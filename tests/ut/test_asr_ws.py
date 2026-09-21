"""Tests for WebSocket ASR client (asr_ws module).

Uses a local mock WebSocket server simulating the Fun-ASR protocol.
"""

import compat  # noqa: F401

import json
import struct
import socket
import unittest
import _thread
import time

import app.providers.ws as ws_client
from app.providers.asr_dashscope import (
    ASRWSError,
    DashScopeASR,
    _run_task_msg,
    _finish_task_msg,
)
from app.providers.ws import _encode_frame, _WS_TEXT, _WS_BINARY
from app.util import send_all
from helpers import (
    find_free_port,
    no_tls,
    ws_server_decode_frame,
    ws_server_encode_frame,
    ws_do_handshake,
)


def transcribe_ws(cfg, pcm, timeout=None):
    return DashScopeASR().transcribe(cfg, pcm, timeout=timeout)


# ---------------------------------------------------------------------------
# Mock WebSocket server helpers
# ---------------------------------------------------------------------------


def _mock_asr_server(port, result_text, fail_event=None):
    """Start a mock Fun-ASR WebSocket server on a background thread.

    Simulates: task-started -> (receive audio) -> result-generated -> task-finished.
    If fail_event is set, sends task-failed instead.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(socket.getaddrinfo("127.0.0.1", port)[0][-1])
    srv.listen(1)
    srv.settimeout(10)

    def loop():
        try:
            conn, _ = srv.accept()
        except OSError:
            srv.close()
            return
        try:
            if not ws_do_handshake(conn):
                return

            # Read run-task
            opcode, payload = ws_server_decode_frame(conn)
            if opcode is None:
                return
            msg = json.loads(payload.decode("utf-8"))
            task_id = msg["header"]["task_id"]

            # Send task-started
            started = json.dumps(
                {
                    "header": {
                        "task_id": task_id,
                        "event": "task-started",
                        "attributes": {},
                    },
                    "payload": {},
                }
            )
            send_all(conn, ws_server_encode_frame(_WS_TEXT, started))

            # Read audio frames until finish-task
            while True:
                opcode, payload = ws_server_decode_frame(conn)
                if opcode is None:
                    return
                if opcode == _WS_TEXT:
                    msg = json.loads(payload.decode("utf-8"))
                    if msg["header"]["action"] == "finish-task":
                        break
                # binary audio frames are consumed silently

            if fail_event:
                failed = json.dumps(
                    {
                        "header": {
                            "task_id": task_id,
                            "event": "task-failed",
                            "error_code": fail_event.get("code", "ERR"),
                            "error_message": fail_event.get("message", "mock error"),
                        },
                        "payload": {},
                    }
                )
                send_all(conn, ws_server_encode_frame(_WS_TEXT, failed))
            else:
                # Send result-generated with sentence_end=true
                result = json.dumps(
                    {
                        "header": {
                            "task_id": task_id,
                            "event": "result-generated",
                            "attributes": {},
                        },
                        "payload": {
                            "output": {
                                "sentence": {
                                    "begin_time": 0,
                                    "end_time": 1000,
                                    "text": result_text,
                                    "heartbeat": False,
                                    "sentence_end": True,
                                    "sentence_id": 1,
                                },
                            },
                            "usage": {"duration": 1},
                        },
                    }
                )
                send_all(conn, ws_server_encode_frame(_WS_TEXT, result))

                # Send task-finished
                finished = json.dumps(
                    {
                        "header": {
                            "task_id": task_id,
                            "event": "task-finished",
                            "attributes": {},
                        },
                        "payload": {"output": {}, "usage": None},
                    }
                )
                send_all(conn, ws_server_encode_frame(_WS_TEXT, finished))

            time.sleep(0.1)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            srv.close()

    _thread.start_new_thread(loop, ())
    time.sleep(0.15)


def _mock_handshake_fail_server(port, status_line="HTTP/1.1 401 Unauthorized"):
    """Server that rejects the WebSocket handshake."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(socket.getaddrinfo("127.0.0.1", port)[0][-1])
    srv.listen(1)
    srv.settimeout(10)

    def loop():
        try:
            conn, _ = srv.accept()
        except OSError:
            srv.close()
            return
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            resp = status_line + "\r\nContent-Length: 0\r\n\r\n"
            send_all(conn, resp.encode("utf-8"))
            time.sleep(0.1)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            srv.close()

    _thread.start_new_thread(loop, ())
    time.sleep(0.15)


def cfg_for_port(port):
    """Config dict pointing at local mock server (plain HTTP, no TLS)."""
    return {
        "asr_ws_host": "ws://127.0.0.1:{}/api-ws/v1/inference".format(port),
        "asr_ws_api_key": "sk-test-key",
        "asr_ws_model": "fun-asr-realtime",
        "asr_ws_format": "pcm",
        "asr_ws_sample_rate": 16000,
    }


# ---------------------------------------------------------------------------
# Unit tests: frame encoding/decoding
# ---------------------------------------------------------------------------


class TestWSFrames(unittest.TestCase):
    def test_frame_roundtrip(self):
        frame = _encode_frame(_WS_TEXT, "hello")
        # Frame should be masked (client->server), verify structure
        self.assertTrue(frame[1] & 0x80)  # MASK bit
        opcode = frame[0] & 0x0F
        self.assertEqual(opcode, _WS_TEXT)
        data = b"\x01\x02\x03\x04"
        frame = _encode_frame(_WS_BINARY, data)
        opcode = frame[0] & 0x0F
        self.assertEqual(opcode, _WS_BINARY)
        length = frame[1] & 0x7F
        self.assertEqual(length, 4)
        # Masking is a big-integer XOR rather than a per-byte loop; unmask
        # with the naive reference at the lengths that matter: empty (close
        # frame), short text, >=126 (2-byte extended length) and a full 100ms
        # ASR audio chunk.
        payloads = (b"", b"\x01", b"abc", b"\xa5" * 5, b"\x00" * 300, bytes(3200))
        for payload in payloads:
            frame = _encode_frame(_WS_BINARY, payload)
            self.assertTrue(frame[1] & 0x80)  # MASK bit
            length_field = frame[1] & 0x7F
            off = 2
            if length_field == 126:
                self.assertGreaterEqual(len(payload), 126)
                self.assertEqual(struct.unpack("!H", frame[2:4])[0], len(payload))
                off = 4
            else:
                self.assertEqual(length_field, len(payload))
            mask_key = frame[off : off + 4]
            body = frame[off + 4 :]
            self.assertEqual(len(body), len(payload))
            self.assertEqual(bytes(b ^ mask_key[i % 4] for i, b in enumerate(body)), payload)


# ---------------------------------------------------------------------------
# Unit tests: protocol messages
# ---------------------------------------------------------------------------


class TestProtocolMessages(unittest.TestCase):
    def test_protocol_messages(self):
        msg = json.loads(_run_task_msg("tid-123", "fun-asr-realtime", 16000))
        self.assertEqual(msg["header"]["action"], "run-task")
        self.assertEqual(msg["header"]["task_id"], "tid-123")
        self.assertEqual(msg["header"]["streaming"], "duplex")
        self.assertEqual(msg["payload"]["model"], "fun-asr-realtime")
        self.assertEqual(msg["payload"]["parameters"]["format"], "pcm")
        self.assertEqual(msg["payload"]["parameters"]["sample_rate"], 16000)
        msg = json.loads(_finish_task_msg("tid-456"))
        self.assertEqual(msg["header"]["action"], "finish-task")
        self.assertEqual(msg["header"]["task_id"], "tid-456")


# ---------------------------------------------------------------------------
# Integration tests: transcribe_ws with mock server
# ---------------------------------------------------------------------------


class TestTranscribeWS(unittest.TestCase):
    def setUp(self):
        # Patch the WS transport to use plain sockets (no TLS).
        self._orig_tls = ws_client._wrap_tls
        ws_client._wrap_tls = no_tls

    def tearDown(self):
        ws_client._wrap_tls = self._orig_tls

    def test_success(self):
        port = find_free_port()
        _mock_asr_server(port, "hello world")
        pcm = b"\x00\x01" * 1600  # 3200 bytes of audio
        result = transcribe_ws(cfg_for_port(port), pcm, timeout=5)
        self.assertEqual(result, "hello world")

    def test_failure_modes(self):
        # Service reports task-failed.
        port = find_free_port()
        _mock_asr_server(port, "", fail_event={"code": "InvalidParameter", "message": "bad audio"})
        with self.assertRaises(ASRWSError):
            transcribe_ws(cfg_for_port(port), b"\x00" * 320, timeout=5)

        # HTTP upgrade rejected.
        port = find_free_port()
        _mock_handshake_fail_server(port)
        with self.assertRaises(ASRWSError):
            transcribe_ws(cfg_for_port(port), b"\x00" * 320, timeout=5)

        # Missing configuration.
        with self.assertRaises(ASRWSError):
            transcribe_ws({}, b"\x00" * 100)
        with self.assertRaises(ASRWSError):
            transcribe_ws({"asr_ws_host": "x"}, b"\x00" * 100)

    def test_stream_live_and_dead(self):
        # A live session takes chunks as they are captured and finish()
        # collects the transcript; close() after finish() is a no-op.
        port = find_free_port()
        _mock_asr_server(port, "streamed text")
        stream = DashScopeASR().stream(cfg_for_port(port), timeout=5)
        self.assertFalse(stream.dead)
        for _ in range(4):
            stream.feed(b"\x00\x01" * 1600)
        self.assertFalse(stream.dead)
        # Sub-frame reads (what the AFE delivers) are accumulated instead of
        # sent as short frames, and finish() flushes the remainder upstream.
        stream.feed(b"\x00\x01" * 512)
        self.assertEqual(stream._sent, 4 * 3200)
        self.assertEqual(stream.finish(), "streamed text")
        self.assertEqual(stream._sent, 4 * 3200 + 1024)
        stream.close()

        # A session whose transport dies mid-utterance reports failure (and is
        # closed) so the caller can retry from its buffered PCM.
        port = find_free_port()
        _mock_asr_server(port, "unused")
        stream = DashScopeASR().stream(cfg_for_port(port), timeout=5)
        stream._ws.close()
        # A full frame: short reads are accumulated before they hit the socket.
        stream.feed(b"\x00" * 3200)
        self.assertTrue(stream.dead)
        with self.assertRaises(ASRWSError):
            stream.finish()


if __name__ == "__main__":
    unittest.main(globals())
