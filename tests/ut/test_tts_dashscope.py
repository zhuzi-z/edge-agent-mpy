"""Tests for the DashScope Sambert TTS WebSocket provider.

Uses a local mock WebSocket server simulating the run-task protocol.
"""

import compat  # noqa: F401

import json
import socket
import unittest
import _thread
import time

import app.providers.ws as ws_client
from app.providers.tts_dashscope import DashScopeTTS, TTSWSError
from app.util import send_all
from helpers import (
    find_free_port,
    no_tls,
    ws_server_decode_frame,
    ws_do_handshake,
    ws_send_json,
    ws_server_encode_frame,
)


def synthesize(cfg, text, sink=None, timeout=None):
    return DashScopeTTS().synthesize(cfg, text, sink=sink, timeout=timeout)


def _mock_sambert_server(port, pcm_data, fail_error=None, seen=None):
    """Mock Sambert WebSocket server (one task per connection)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(socket.getaddrinfo("127.0.0.1", port)[0][-1])
    srv.listen(1)
    srv.settimeout(2)

    def loop():
        for _attempt in range(3):
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            try:
                if not ws_do_handshake(conn):
                    conn.close()
                    continue

                # Read run-task.
                opcode, payload = ws_server_decode_frame(conn)
                if opcode is None:
                    conn.close()
                    continue
                msg = json.loads(payload)
                if seen is not None:
                    seen.update(msg)
                if msg.get("header", {}).get("action") != "run-task":
                    conn.close()
                    continue

                ws_send_json(
                    conn,
                    {
                        "header": {
                            "task_id": msg["header"].get("task_id", ""),
                            "event": "task-started",
                            "attributes": {},
                        },
                        "payload": {},
                    },
                )

                if fail_error:
                    ws_send_json(
                        conn,
                        {
                            "header": {
                                "task_id": msg["header"].get("task_id", ""),
                                "event": "task-failed",
                                "error_code": fail_error.get("code", ""),
                                "error_message": fail_error.get("message", ""),
                                "attributes": {},
                            },
                            "payload": {},
                        },
                    )
                    time.sleep(0.02)
                    conn.close()
                    break

                # Audio in 2 binary chunks with an interleaved event.
                mid = len(pcm_data) // 2
                if mid % 2 == 1:
                    mid += 1
                chunks = [pcm_data[:mid], pcm_data[mid:]] if mid > 0 else [pcm_data]
                for i, chunk in enumerate(chunks):
                    if chunk:
                        send_all(conn, ws_server_encode_frame(2, chunk))
                    if i == 0:
                        ws_send_json(
                            conn,
                            {
                                "header": {
                                    "task_id": msg["header"].get("task_id", ""),
                                    "event": "result-generated",
                                    "attributes": {},
                                },
                                "payload": {"usage": {"characters": 4}},
                            },
                        )

                ws_send_json(
                    conn,
                    {
                        "header": {
                            "task_id": msg["header"].get("task_id", ""),
                            "event": "task-finished",
                            "attributes": {},
                        },
                        "payload": {"usage": {"characters": 4}},
                    },
                )
                conn.close()
                break
            except OSError:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
        try:
            srv.close()
        except OSError:
            pass

    _thread.start_new_thread(loop, ())
    time.sleep(0.05)


def cfg_tts(port):
    return {
        "tts_provider": "dashscope",
        # Full endpoint URL: host, port and path all come from tts_ws_host.
        "tts_ws_host": "ws://127.0.0.1:{}/api-ws/v1/inference".format(port),
        "tts_ws_api_key": "sk-test-key",
        "tts_ws_model": "sambert-zhiying-v1",
        "tts_ws_sample_rate": 16000,
    }


class TestDashScopeTTS(unittest.TestCase):
    def setUp(self):
        # Patch the WS transport to use plain sockets (no TLS).
        self._orig_tls = ws_client._wrap_tls
        ws_client._wrap_tls = no_tls

    def tearDown(self):
        ws_client._wrap_tls = self._orig_tls

    def test_parse_ws_endpoint(self):
        parse = ws_client.parse_ws_endpoint
        self.assertEqual(
            parse("wss://h.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"),
            ("h.cn-beijing.maas.aliyuncs.com", 443, "/api-ws/v1/inference"),
        )
        self.assertEqual(parse("ws://127.0.0.1:19100/p"), ("127.0.0.1", 19100, "/p"))
        with self.assertRaises(ws_client.WSError):
            parse("dashscope.aliyuncs.com")

    def test_success(self):
        pcm = b"\x11\x22" * 800
        seen = {}
        port = find_free_port()
        _mock_sambert_server(port, pcm, seen=seen)
        chunks = []
        total = synthesize(cfg_tts(port), "你好", sink=chunks.append, timeout=10)
        self.assertEqual(total, len(pcm))
        self.assertEqual(b"".join(chunks), pcm)
        # run-task carries the full text once, streaming "out", PCM format.
        self.assertEqual(seen["header"]["streaming"], "out")
        self.assertEqual(seen["payload"]["input"]["text"], "你好")
        self.assertEqual(seen["payload"]["parameters"]["format"], "pcm")
        # Without a sink the PCM is returned as bytes.
        port = find_free_port()
        _mock_sambert_server(port, pcm)
        self.assertEqual(synthesize(cfg_tts(port), "你好", timeout=10), pcm)

    def test_task_failed(self):
        port = find_free_port()
        _mock_sambert_server(port, b"", fail_error={"code": "InvalidParameter", "message": "bad"})
        with self.assertRaises(TTSWSError):
            synthesize(cfg_tts(port), "你好", timeout=10)

    def test_prewarmed_connection(self):
        # connect() opens the socket before the text is known and
        # synthesize() reuses it, moving the handshake off the listener's wait.
        pcm = b"\x11\x22" * 800
        seen = {}
        port = find_free_port()
        _mock_sambert_server(port, pcm, seen=seen)
        tts = DashScopeTTS()
        cfg = cfg_tts(port)
        ws = tts.connect(cfg, timeout=10)
        chunks = []
        total = tts.synthesize(cfg, "你好", sink=chunks.append, timeout=10, ws=ws)
        self.assertEqual(total, len(pcm))
        self.assertEqual(b"".join(chunks), pcm)
        self.assertEqual(seen["payload"]["input"]["text"], "你好")
        with self.assertRaises(TTSWSError):
            tts.connect({}, timeout=1)

    def test_missing_config_and_empty_text(self):
        with self.assertRaises(TTSWSError):
            synthesize({}, "你好")
        with self.assertRaises(TTSWSError):
            synthesize({"tts_ws_host": "x"}, "你好")
        # Empty text synthesizes to empty PCM without a connection.
        self.assertEqual(synthesize(cfg_tts(1), ""), b"")
        self.assertEqual(synthesize(cfg_tts(1), "", sink=lambda c: None), 0)


if __name__ == "__main__":
    unittest.main(globals())
