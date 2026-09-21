"""Stub: manage a CPython fake LLM/ASR/TTS server for integration tests.

Launches tests/fake_llm_server.py (TLS + WebSocket) as a background
subprocess and provides helpers to wait for readiness and shut it down.
"""

import os
import time
import socket
import tls
import _thread

from app.util import send_all

_SERVER_SCRIPT = None


def _find_server():
    global _SERVER_SCRIPT
    if _SERVER_SCRIPT:
        return _SERVER_SCRIPT
    base = __file__.rsplit("/", 1)[0]
    _SERVER_SCRIPT = base + "/../fake_llm_server.py"
    return _SERVER_SCRIPT


def _find_python():
    base = __file__.rsplit("/", 1)[0]
    venv_py = base + "/../../.venv/bin/python3"
    try:
        os.stat(venv_py)
        return venv_py
    except OSError:
        return "python3"


def start(port, asr_text=None):
    """Launch the fake server on the given port. Blocks until ready."""
    script = _find_server()
    python = _find_python()
    cmd = "{} {} {}".format(python, script, port)
    if asr_text:
        cmd += " '{}'".format(asr_text)
    cmd += " &"

    _thread.start_new_thread(os.system, (cmd,))

    for _ in range(80):
        if _ping(port):
            return True
        time.sleep(0.15)
    raise RuntimeError("fake LLM server did not start on port {}".format(port))


def stop(port):
    """Request graceful shutdown of the fake server."""
    try:
        s = _tls_connect(port)
        send_all(s, b"GET /__stop HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        time.sleep(0.3)
        s.close()
    except OSError:
        pass


def _ping(port):
    """Check if the server is responding over TLS."""
    try:
        s = _tls_connect(port)
        send_all(s, b"GET /__ping HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        data = s.recv(4096)
        s.close()
        return b"200" in data
    except OSError:
        return False


def _tls_connect(port):
    """Create a TLS connection to the server (no cert verification)."""
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(5)
    raw.connect(socket.getaddrinfo("127.0.0.1", port)[0][-1])
    ctx = tls.SSLContext(tls.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = tls.CERT_NONE
    return ctx.wrap_socket(raw, server_hostname="127.0.0.1")
