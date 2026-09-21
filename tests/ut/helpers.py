"""Shared test utilities."""

import compat  # noqa: F401

import os
import json
import struct
import time
import socket
import asyncio
import _thread

from app.util import send_all


_counter = 0


def _unique_name(prefix="tmp"):
    global _counter
    _counter += 1
    return "/tmp/%s%d_%d" % (prefix, time.ticks_ms(), _counter)


def mkdtemp(prefix="test_"):
    d = _unique_name(prefix)
    os.mkdir(d)
    return d


def rmtree(path):
    try:
        for entry in os.listdir(path):
            p = path + "/" + entry
            try:
                os.remove(p)
            except OSError:
                rmtree(p)
        os.rmdir(path)
    except OSError:
        pass


def path_join(*parts):
    return "/".join(p.rstrip("/") for p in parts)


def path_dirname(p):
    return p.rsplit("/", 1)[0] if "/" in p else ""


def path_isdir(p):
    try:
        os.listdir(p)
        return True
    except OSError:
        return False


def find_free_port(start=18421):
    """Find a free TCP port by bind-probing."""
    for p in range(start, start + 200):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(socket.getaddrinfo("127.0.0.1", p)[0][-1])
            s.close()
            return p
        except OSError:
            try:
                s.close()
            except OSError:
                pass
    raise RuntimeError("no free port in [%d, %d)" % (start, start + 200))


def raw_request(port, method, path, body=None, host="127.0.0.1", timeout=5, headers=None):
    """Minimal HTTP/1.1 request. Returns (status, head_str, body_bytes)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(socket.getaddrinfo(host, port)[0][-1])
    b = b""
    if body is not None:
        b = body.encode("utf-8") if isinstance(body, str) else body
    req = "%s %s HTTP/1.1\r\nHost: %s\r\n" % (method, path, host)
    if body is not None:
        req += "Content-Length: %d\r\n" % len(b)
    for name, value in (headers or {}).items():
        req += "%s: %s\r\n" % (name, value)
    req += "Connection: close\r\n\r\n"
    send_all(s, req.encode("utf-8") + b)
    data = b""
    while True:
        try:
            chunk = s.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    s.close()
    hend = data.find(b"\r\n\r\n")
    if hend < 0:
        return 0, "", data
    head = data[:hend].decode("utf-8")
    try:
        status = int(head.split("\r\n")[0].split(" ")[1])
    except (IndexError, ValueError):
        status = 0
    return status, head, data[hend + 4 :]


class ServerHarness:
    """Base for integration tests: real HTTPServer on a free port."""

    def setUp(self):
        self.port = find_free_port()
        self._stopped = False
        self._tmp_dirs = []
        self._tmp_files = []
        self.server = self._build_server()

    def tearDown(self):
        self._stopped = True
        time.sleep(0.1)  # let the background event loop stop the server
        for p in self._tmp_files:
            try:
                os.remove(p)
            except OSError:
                pass
        for d in self._tmp_dirs:
            rmtree(d)

    def _build_server(self):
        raise NotImplementedError

    def _start(self):
        _thread.start_new_thread(self._run_server, ())
        time.sleep(0.15)

    def _run_server(self):
        async def serve():
            await self.server.start()
            while not self._stopped:
                await asyncio.sleep(0.02)
            await self.server.stop()

        asyncio.run(serve())

    def _request(self, method, path, body=None):
        status, head, rbody = raw_request(self.port, method, path, body=body)
        return status, rbody.decode("utf-8")

    def _get(self, path):
        return self._request("GET", path)

    def _post(self, path, body):
        return self._request("POST", path, body)

    def _post_json(self, path, obj):
        status, resp = self._post(path, json.dumps(obj))
        return status, json.loads(resp)

    def _make_temp_file(self, suffix=".json"):
        path = _unique_name("file_") + suffix
        f = open(path, "w")
        f.close()
        self._tmp_files.append(path)
        return path

    def _make_temp_dir(self, prefix="test_"):
        d = mkdtemp(prefix)
        self._tmp_dirs.append(d)
        return d


class FakeAgent:
    """Minimal agent stub for bus/channel tests."""

    def __init__(self, reply_text="ok", error=None):
        self._reply = reply_text
        self._error = error
        self.calls = []

    def reply(self, user_text, history=None, channel=None):
        self.calls.append(
            {"text": user_text, "history_len": len(history or []), "channel": channel}
        )
        msg = {"role": "assistant", "content": self._reply}
        return self._reply, self._error, [msg]

    async def reply_async(self, user_text, history=None, channel=None):
        return self.reply(user_text, history, channel)

    def make_llm_fn(self):
        return None


def no_tls(sock, host, ca_path=None):
    """TLS bypass: return socket unwrapped for plain-HTTP tests."""
    return sock


def read_http_request(conn):
    """Read one full HTTP request (headers + Content-Length body) from ``conn``.

    Fake servers must drain the entire request before closing; closing with
    unread data in the receive buffer makes the kernel send a RST, which the
    client sees as ECONNRESET while reading the response.
    """
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    header_end = data.find(b"\r\n\r\n") + 4
    content_length = 0
    for line in bytes(data[:header_end]).split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                pass
            break
    while len(data) - header_end < content_length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def start_raw_server(handler, port):
    """Start a one-shot TCP server on a background thread."""
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(socket.getaddrinfo("127.0.0.1", port)[0][-1])
    srv.listen(1)
    srv.settimeout(5)

    def loop():
        try:
            conn, _ = srv.accept()
            try:
                handler(conn)
            finally:
                conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    _thread.start_new_thread(loop, ())
    time.sleep(0.15)


def s16_chunk(samples):
    """Encode signed integers into little-endian PCM16 bytes."""
    out = bytearray()
    for s in samples:
        if s < 0:
            s += 0x10000
        out.append(s & 0xFF)
        out.append((s >> 8) & 0xFF)
    return bytes(out)


# --- Mock WebSocket server codec (shared by test_asr_ws / test_tts_ws) ---


def ws_server_decode_frame(conn):
    """Decode a masked WebSocket frame from a client connection."""
    header = b""
    while len(header) < 2:
        chunk = conn.recv(2 - len(header))
        if not chunk:
            return None, None
        header += chunk
    byte1, byte2 = header[0], header[1]
    opcode = byte1 & 0x0F
    masked = bool(byte2 & 0x80)
    length = byte2 & 0x7F
    if length == 126:
        ext = b""
        while len(ext) < 2:
            ext += conn.recv(2 - len(ext))
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = b""
        while len(ext) < 8:
            ext += conn.recv(8 - len(ext))
        length = struct.unpack("!Q", ext)[0]
    mask_key = b""
    if masked:
        while len(mask_key) < 4:
            mask_key += conn.recv(4 - len(mask_key))
    payload = b""
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def ws_server_encode_frame(opcode, payload):
    """Encode an unmasked WebSocket frame (server -> client)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)
    byte1 = 0x80 | opcode
    byte2 = 0  # no mask from server
    if length < 126:
        byte2 |= length
        header = struct.pack("!BB", byte1, byte2)
    elif length < (1 << 16):
        byte2 |= 126
        header = struct.pack("!BBH", byte1, byte2, length)
    else:
        byte2 |= 127
        header = struct.pack("!BBQ", byte1, byte2, length)
    return header + payload


def ws_do_handshake(conn):
    """Read an HTTP upgrade request and answer 101 Switching Protocols."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: fake\r\n"
        "\r\n"
    )
    send_all(conn, resp.encode("utf-8"))
    return True


def ws_send_json(conn, obj):
    """Send a JSON dict as an unmasked text frame (server -> client)."""
    send_all(conn, ws_server_encode_frame(1, json.dumps(obj)))  # 1 = TEXT opcode


def tar_entry(name, data, kind=b"0", prefix=""):
    """One ustar member: a 512 byte header, the data, and its block padding.

    Hand-built because there is no tarfile on MicroPython - the device reads
    exactly this layout, so the tests write exactly what `make ota` writes.
    `kind` is the type flag (b"0" a file, b"5" a directory) and `prefix` is
    ustar's second half of a name too long for its own field.
    """
    head = bytearray(512)
    head[0 : len(name)] = name.encode()
    head[100:108] = b"0000644\0"
    head[124:136] = b"%011o\0" % len(data)
    head[136:148] = b"00000000000\0"
    head[148:156] = b"        "  # the checksum is computed over these spaces
    head[156:157] = kind
    head[257:263] = b"ustar\0"
    head[263:265] = b"00"
    if prefix:
        head[345 : 345 + len(prefix)] = prefix.encode()
    head[148:155] = b"%06o\0" % sum(head)
    return bytes(head) + data + b"\0" * (-len(data) % 512)
