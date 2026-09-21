#!/usr/bin/env python3
"""Fake LLM/ASR/TTS server for app-level integration testing.

All connections are TLS (self-signed cert, generated at startup).
Provides:
  HTTPS POST /v1/chat/completions     — echo-based LLM inference
  WSS    /api-ws/v1/inference         — DashScope run-task: Fun-ASR + Sambert TTS
  HTTPS GET  /__ping                  — readiness check
  HTTPS GET  /__stop                  — graceful shutdown

Usage:
    python3 tests/fake_llm_server.py <port> [asr_text]
"""

import base64
import hashlib
import json
import math
import os
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import socket

SAMPLE_RATE = 16000
DEFAULT_ASR_TEXT = "who are you?"
IDENTITY_REPLY = "I'm an edge agent running with MicroPython"
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-5AB5-11CE-85AA-3085F01D"

asr_text = DEFAULT_ASR_TEXT
# One-shot ASR override armed via POST /__asr_next: the next ASR session
# returns this text instead of running whisper (deterministic scenarios).
_asr_next = None
_shutdown_event = threading.Event()

# ---------------------------------------------------------------------------
# Local model loading (lazy, thread-safe)
# ---------------------------------------------------------------------------

_model_lock = threading.Lock()
_whisper_model = None
_piper_voice = None

WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "tiny")
PIPER_MODEL_PATH = os.environ.get(
    "PIPER_MODEL",
    os.path.join(os.path.expanduser("~"), ".local", "share", "piper", "en_US-lessac-low.onnx"),
)


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        with _model_lock:
            if _whisper_model is None:
                os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
                from faster_whisper import WhisperModel

                print("[models] Loading whisper {} ...".format(WHISPER_MODEL_SIZE), flush=True)
                _whisper_model = WhisperModel(
                    WHISPER_MODEL_SIZE, device="cpu", compute_type="int8"
                )
                print("[models] Whisper ready", flush=True)
    return _whisper_model


def _get_piper():
    global _piper_voice
    if _piper_voice is None:
        with _model_lock:
            if _piper_voice is None:
                from piper import PiperVoice

                print("[models] Loading piper {} ...".format(PIPER_MODEL_PATH), flush=True)
                _piper_voice = PiperVoice.load(PIPER_MODEL_PATH)
                print(
                    "[models] Piper ready (rate={})".format(_piper_voice.config.sample_rate),
                    flush=True,
                )
    return _piper_voice


def _transcribe_pcm(pcm_bytes):
    """Run faster-whisper on raw PCM16-LE bytes. Returns recognized text."""
    import numpy as np
    import tempfile

    model = _get_whisper()
    tmp = tempfile.NamedTemporaryFile(suffix=".raw", delete=False)
    import wave as _wave

    tmp.close()
    wav_path = tmp.name + ".wav"
    with _wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    try:
        segments, _info = model.transcribe(wav_path, language="zh")
        text = "".join(s.text for s in segments)
    finally:
        os.unlink(wav_path)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return text.strip()


def _synthesize_pcm(text):
    """Run piper-tts on text. Returns (pcm_bytes, sample_rate)."""
    import io
    import wave as _wave

    voice = _get_piper()
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        voice.synthesize_wav(text, wf)
    buf.seek(0)
    with _wave.open(buf, "rb") as wf:
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    return pcm, rate


# ---------------------------------------------------------------------------
# TLS cert generation
# ---------------------------------------------------------------------------


def _generate_cert():
    """Generate a self-signed cert. Returns (cert_path, key_path)."""
    tmp_dir = tempfile.mkdtemp(prefix="fake_llm_")
    cert_path = os.path.join(tmp_dir, "cert.pem")
    key_path = os.path.join(tmp_dir, "key.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            key_path,
            "-out",
            cert_path,
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
        ],
        capture_output=True,
        check=True,
    )
    return cert_path, key_path


# ---------------------------------------------------------------------------
# WebSocket helpers (RFC 6455 server side)
# ---------------------------------------------------------------------------


def _ws_accept_key(client_key):
    combined = client_key + "258EAFA5-E914-47DA-95CA-5AB5CF4655B4"
    sha1 = hashlib.sha1(combined.encode()).digest()
    return base64.b64encode(sha1).decode()


def _ws_encode_frame(opcode, payload):
    """Encode a server frame (unmasked)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    frame = bytearray()
    frame.append(0x80 | opcode)
    length = len(payload)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(127)
        frame.extend(struct.pack(">Q", length))
    frame.extend(payload)
    return bytes(frame)


def _ws_decode_frame(conn):
    """Decode one client frame. Returns (opcode, payload) or (None, None)."""
    header = _recv_exact(conn, 2)
    if header is None:
        return None, None
    opcode = header[0] & 0x0F
    masked = (header[1] & 0x80) != 0
    length = header[1] & 0x7F
    if length == 126:
        ext = _recv_exact(conn, 2)
        if ext is None:
            return None, None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = _recv_exact(conn, 8)
        if ext is None:
            return None, None
        length = struct.unpack(">Q", ext)[0]
    mask = b""
    if masked:
        mask = _recv_exact(conn, 4)
        if mask is None:
            return None, None
    payload = _recv_exact(conn, length) if length > 0 else b""
    if payload is None:
        return None, None
    if masked and mask:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload


def _recv_exact(conn, n):
    """Receive exactly n bytes."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except (OSError, ssl.SSLError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _ws_send_text(conn, obj):
    """Send a JSON text frame."""
    data = json.dumps(obj) if isinstance(obj, dict) else obj
    conn.sendall(_ws_encode_frame(1, data))


# ---------------------------------------------------------------------------
# Protocol handlers
# ---------------------------------------------------------------------------


def _handle_inference_ws(conn):
    """DashScope /api-ws/v1/inference: dispatch run-task by payload.task."""
    try:
        opcode, payload = _ws_decode_frame(conn)
        if opcode is None:
            return
        msg = json.loads(payload.decode("utf-8"))
        task_id = msg.get("header", {}).get("task_id", "t1")
        if msg.get("payload", {}).get("task") == "tts":
            _handle_tts_task(conn, task_id, msg)
        else:
            _handle_asr_task(conn, task_id)
    except (OSError, ssl.SSLError, json.JSONDecodeError):
        pass


def _handle_asr_task(conn, task_id):
    """Fun-ASR protocol after run-task: audio → finish → result.

    Collects binary PCM frames and runs faster-whisper for real ASR.
    Falls back to the hardcoded asr_text if the model is unavailable.
    """
    try:
        _ws_send_text(
            conn,
            {
                "header": {"task_id": task_id, "event": "task-started", "attributes": {}},
                "payload": {},
            },
        )

        audio_buf = bytearray()
        while True:
            opcode, payload = _ws_decode_frame(conn)
            if opcode is None:
                return
            if opcode == 2:
                audio_buf.extend(payload)
            elif opcode == 1:
                m = json.loads(payload.decode("utf-8"))
                if m.get("header", {}).get("action") == "finish-task":
                    break

        global _asr_next
        override = _asr_next
        _asr_next = None
        if override:
            recognized = override
            print("[ASR] override: {}".format(recognized), flush=True)
        else:
            recognized = asr_text
            if audio_buf:
                try:
                    recognized = _transcribe_pcm(bytes(audio_buf))
                    print("[ASR] recognized: {}".format(recognized), flush=True)
                except Exception as e:
                    print("[ASR] model failed, fallback: {}".format(e), flush=True)

        _ws_send_text(
            conn,
            {
                "header": {"task_id": task_id, "event": "result-generated", "attributes": {}},
                "payload": {"output": {"sentence": {"text": recognized, "sentence_end": True}}},
            },
        )
        _ws_send_text(
            conn,
            {
                "header": {"task_id": task_id, "event": "task-finished", "attributes": {}},
                "payload": {},
            },
        )
    except (OSError, ssl.SSLError, json.JSONDecodeError):
        pass


def _handle_tts_task(conn, task_id, msg):
    """Sambert protocol after run-task: task-started → PCM frames → finished.

    Uses piper-tts for real speech synthesis.
    Falls back to sine wave if the model is unavailable.
    """
    try:
        params = msg.get("payload", {}).get("parameters", {})
        try:
            rate = int(params.get("sample_rate") or 16000)
        except (TypeError, ValueError):
            rate = 16000
        text = msg.get("payload", {}).get("input", {}).get("text", "")
        _ws_send_text(
            conn,
            {
                "header": {"task_id": task_id, "event": "task-started", "attributes": {}},
                "payload": {},
            },
        )

        pcm = None
        out_rate = rate
        if text:
            try:
                pcm, out_rate = _synthesize_pcm(text)
                print(
                    "[TTS] synthesized {} bytes for: {}".format(len(pcm), text[:40]),
                    flush=True,
                )
            except Exception as e:
                print("[TTS] model failed, fallback: {}".format(e), flush=True)
        if pcm is None:
            pcm = _sine_pcm(freq=440, duration_sec=0.5, rate=rate)

        chunk_size = max(out_rate * 2 // 10, 2)
        for i in range(0, len(pcm), chunk_size):
            conn.sendall(_ws_encode_frame(2, pcm[i : i + chunk_size]))
        _ws_send_text(
            conn,
            {
                "header": {"task_id": task_id, "event": "task-finished", "attributes": {}},
                "payload": {},
            },
        )
    except (OSError, ssl.SSLError, json.JSONDecodeError):
        pass


def _sine_pcm(freq=440, duration_sec=0.5, amplitude=6000, rate=SAMPLE_RATE):
    n = int(rate * duration_sec)
    out = bytearray()
    for i in range(n):
        val = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        out += struct.pack("<h", val)
    return bytes(out)


# ---------------------------------------------------------------------------
# HTTP handling
# ---------------------------------------------------------------------------


def _handle_http(conn, method, path, body):
    """Handle regular HTTP request. Returns True if handled."""
    if method == "GET" and path == "/__ping":
        _send_json(conn, 200, {"status": "ok"})
        return True
    if method == "GET" and path == "/__stop":
        _send_json(conn, 200, {"status": "stopping"})
        _shutdown_event.set()
        return True
    if method == "POST" and path == "/__asr_next":
        global _asr_next
        try:
            _asr_next = json.loads(body).get("text", "")
        except (json.JSONDecodeError, ValueError):
            _send_json(conn, 400, {"error": "bad json"})
            return True
        _send_json(conn, 200, {"status": "armed", "text": _asr_next})
        return True
    if method == "POST" and path == "/v1/chat/completions":
        _handle_chat(conn, body)
        return True
    _send_json(conn, 404, {"error": "not found"})
    return True


def _handle_chat(conn, body):
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        _send_json(conn, 400, {"error": "bad json"})
        return

    messages = data.get("messages", [])
    tools = data.get("tools")
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m.get("content", "")
            break

    # Stateless check: only a request ending with a voice_control tool
    # result is the post-tool round (older history may contain stale
    # calls). Covers both actions: exit (ack spoken) and volume (the
    # "OK" confirmation is spoken at the new volume).
    last = messages[-1] if messages else {}
    tool_done = last.get("role") == "tool" and last.get("name") == "voice_control"
    if tool_done:
        resp = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
    elif tools and "stand down" in user_msg.lower():
        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_exit",
                                "type": "function",
                                "function": {
                                    "name": "voice_control",
                                    "arguments": '{"action": "exit", "ack": "Sure, talk to you later"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
    elif tools and "volume" in user_msg.lower():
        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_vol",
                                "type": "function",
                                "function": {
                                    "name": "voice_control",
                                    "arguments": '{"action": "volume", "level": 30}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
    elif tools and "gpio" in user_msg.lower():
        tool_name = tools[0]["function"]["name"]
        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_001",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": '{"pin": 2, "action": "on"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
    elif "who are you" in user_msg.lower():
        reply = IDENTITY_REPLY
        resp = {"choices": [{"message": {"role": "assistant", "content": reply}}]}
    else:
        reply = "Echo: {}".format(user_msg) if user_msg else "Hello!"
        resp = {"choices": [{"message": {"role": "assistant", "content": reply}}]}
    _send_json(conn, 200, resp)


def _send_json(conn, code, obj):
    data = json.dumps(obj).encode("utf-8")
    status_text = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(code, "Error")
    header = (
        "HTTP/1.1 {} {}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: {}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).format(code, status_text, len(data))
    conn.sendall(header.encode("utf-8") + data)


# ---------------------------------------------------------------------------
# Connection dispatcher
# ---------------------------------------------------------------------------


def _handle_connection(conn):
    """Dispatch a TLS connection: WebSocket upgrade or plain HTTP."""
    try:
        conn.settimeout(30)
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                return
            request.extend(chunk)

        header_text = request.decode("utf-8", errors="replace")
        lines = header_text.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1].split("?")[0]

        is_upgrade = any(
            l.lower().startswith("upgrade:") and "websocket" in l.lower() for l in lines[1:]
        )

        if is_upgrade:
            ws_key = ""
            for l in lines[1:]:
                if l.lower().startswith("sec-websocket-key:"):
                    ws_key = l.split(":", 1)[1].strip()
                    break
            accept = _ws_accept_key(ws_key)
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: {}\r\n"
                "\r\n"
            ).format(accept)
            conn.sendall(resp.encode("utf-8"))

            if path.rstrip("/") == "/api-ws/v1/inference":
                _handle_inference_ws(conn)
        else:
            content_length = 0
            for l in lines[1:]:
                if l.lower().startswith("content-length:"):
                    content_length = int(l.split(":", 1)[1].strip())
                    break
            header_end = request.find(b"\r\n\r\n")
            body_received = request[header_end + 4 :]
            while len(body_received) < content_length:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                body_received.extend(chunk)
            body = body_received[:content_length].decode("utf-8", errors="replace")
            _handle_http(conn, method, path, body)
    except (OSError, ssl.SSLError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18900
    global asr_text
    if len(sys.argv) > 2:
        asr_text = sys.argv[2]

    cert_path, key_path = _generate_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    srv.settimeout(1.0)

    # Preload local models so first request isn't delayed
    if os.environ.get("PRELOAD_MODELS", "1") != "0":
        try:
            _get_whisper()
            _get_piper()
        except Exception as e:
            print("[models] preload warning: {}".format(e), flush=True)

    print("READY {}".format(port), flush=True)

    while not _shutdown_event.is_set():
        try:
            raw_conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            tls_conn = ctx.wrap_socket(raw_conn, server_side=True)
        except (ssl.SSLError, OSError):
            try:
                raw_conn.close()
            except OSError:
                pass
            continue
        t = threading.Thread(target=_handle_connection, args=(tls_conn,), daemon=True)
        t.start()

    srv.close()
    try:
        os.remove(cert_path)
        os.remove(key_path)
        os.rmdir(os.path.dirname(cert_path))
    except OSError:
        pass
    print("STOPPED", flush=True)


if __name__ == "__main__":
    main()
