"""Minimal synchronous WebSocket client (RFC 6455 subset).

Shared transport for WebSocket-based providers (DashScope ASR/TTS).
Client frames are always masked; server frames are assumed unmasked.
"""

import json
import struct
import socket
import time
import binascii
import app.log as log
from app.httpclient import _wrap_tls
from app.providers.base import ProviderError
from app.util import send_all, ms_since


class WSError(ProviderError):
    """WebSocket transport failure."""


_WS_TEXT = 1
_WS_BINARY = 2
_WS_CLOSE = 8
_WS_PING = 9
_WS_PONG = 10


def _ws_key():
    """Generate a random Sec-WebSocket-Key."""
    import urandom

    raw = bytes(urandom.getrandbits(8) for _ in range(16))
    return binascii.b2a_base64(raw).decode("ascii").strip()


def parse_ws_endpoint(value):
    """Split a full endpoint URL into (host, port, path).

    Expects "wss://host[:port]/path"; the path is mandatory and the
    port defaults to 443. Raises WSError on anything less specific.
    """
    value = value.strip()
    for scheme in ("wss://", "ws://", "https://", "http://"):
        if value.startswith(scheme):
            value = value[len(scheme) :]
            break
    host_port, _, path = value.partition("/")
    if not host_port or not path:
        raise WSError("endpoint must be a full URL with path: {}".format(value))
    host, _, port_str = host_port.partition(":")
    try:
        port = int(port_str) if port_str else 443
    except ValueError:
        raise WSError("invalid port in endpoint: {}".format(value))
    return host.strip(), port, "/" + path


def _mask_payload(payload, mask):
    """XOR ``payload`` with the repeating 4-byte ``mask``.

    A per-byte Python loop costs ~50ms per 3200-byte frame on the ESP32, which
    made the ASR audio upload CPU-bound (1.6s for 3s of PCM, ~60KB/s "apparent
    throughput"). Treating both operands as one big integer moves the loop into
    C: same result, ~12x faster on the unix port and far more on device.
    Peak extra heap is a few times the frame size, bounded by the caller's
    chunk size (ASR sends 100ms frames).
    """
    n = len(payload)
    if not n:
        return payload
    reps = (n + 3) // 4
    key = int.from_bytes(mask * reps, "big") >> ((reps * 4 - n) * 8)
    return (int.from_bytes(payload, "big") ^ key).to_bytes(n, "big")


def _encode_frame(opcode, payload):
    """Encode a WebSocket frame with masking (client -> server)."""
    import urandom

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)
    byte1 = 0x80 | opcode
    byte2 = 0x80  # MASK bit set
    if length < 126:
        byte2 |= length
        header = struct.pack("!BB", byte1, byte2)
    elif length < (1 << 16):
        byte2 |= 126
        header = struct.pack("!BBH", byte1, byte2, length)
    else:
        byte2 |= 127
        header = struct.pack("!BBQ", byte1, byte2, length)
    mask = struct.pack("!I", urandom.getrandbits(32))
    return header + mask + _mask_payload(payload, mask)


def _recv_exact(sock, n):
    """Read exactly n bytes from socket, normalising transport errors."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError as e:
            raise WSError("socket read failed: {}".format(e))
        if not chunk:
            raise WSError("connection closed")
        buf.extend(chunk)
    return bytes(buf)


def _decode_frame(sock):
    """Decode one WebSocket frame from server.

    Returns (fin, opcode, payload). Transport failures raise WSError.
    """
    header = _recv_exact(sock, 2)
    byte1, byte2 = header[0], header[1]
    fin = bool(byte1 & 0x80)
    opcode = byte1 & 0x0F
    masked = bool(byte2 & 0x80)
    length = byte2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask_key = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length > 0 else b""
    if mask_key:
        payload = _mask_payload(payload, mask_key)
    return fin, opcode, payload


class _BufferedSock:
    """Socket wrapper draining a pushback buffer before real reads.

    The 101 handshake response may share a segment with the first
    WebSocket frame; bytes read past the header terminator are held
    here instead of being dropped. ``send`` passes through so the
    reader can also answer PINGs in :func:`_read_message`.
    """

    def __init__(self, sock, initial=b""):
        self._sock = sock
        self._buf = initial
        self._off = 0

    def recv(self, n):
        if self._off < len(self._buf):
            data = self._buf[self._off : self._off + n]
            self._off += len(data)
            return data
        return self._sock.recv(n)

    def send(self, data):
        return self._sock.send(data)


def _read_message(sock):
    """Read and reassemble one complete WebSocket message.

    Handles fragmentation (data frames until FIN), answers PING with PONG,
    and raises WSError on CLOSE or transport failure. Returns
    (opcode, payload) where payload is str for TEXT frames, bytes otherwise.
    """
    opcode = None
    payload = bytearray()
    while True:
        fin, fop, data = _decode_frame(sock)
        if fop == _WS_CLOSE:
            try:
                send_all(sock, _encode_frame(_WS_CLOSE, b""))
            except OSError:
                pass
            raise WSError("server closed connection")
        if fop == _WS_PING:
            try:
                send_all(sock, _encode_frame(_WS_PONG, data))
            except OSError as e:
                raise WSError("ping reply failed: {}".format(e))
            continue
        if fop == _WS_PONG:
            continue
        if fop == 0:  # continuation
            if opcode is None:
                raise WSError("continuation frame without a start frame")
        elif fop in (_WS_TEXT, _WS_BINARY):
            if opcode is not None:
                raise WSError("new data frame before previous message finished")
            opcode = fop
        else:
            raise WSError("unexpected opcode {}".format(fop))
        payload.extend(data)
        if not fin:
            continue
        if opcode == _WS_TEXT:
            try:
                return _WS_TEXT, bytes(payload).decode("utf-8")
            except UnicodeError as e:
                raise WSError("invalid UTF-8 in text frame: {}".format(e))
        return opcode, bytes(payload)


class WSConnection:
    """Minimal synchronous WebSocket connection."""

    def __init__(self, sock, initial=b""):
        self._sock = sock
        self._reader = _BufferedSock(sock, initial)
        self._closed = False

    def _send(self, frame):
        try:
            send_all(self._sock, frame)
        except OSError as e:
            raise WSError("socket write failed: {}".format(e))

    def send_text(self, text):
        self._send(_encode_frame(_WS_TEXT, text))

    def send_binary(self, data):
        self._send(_encode_frame(_WS_BINARY, data))

    def recv(self):
        """Receive one message. Returns (opcode, data)."""
        return _read_message(self._reader)

    def recv_json(self):
        """Receive and parse a JSON text message."""
        opcode, data = self.recv()
        if opcode != _WS_TEXT:
            raise WSError("expected text frame, got opcode {}".format(opcode))
        return json.loads(data)

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                send_all(self._sock, _encode_frame(_WS_CLOSE, b""))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass


def make_task_id():
    """Generate a UUID-like task/event ID."""
    import urandom

    parts = []
    for n in (4, 2, 2, 2, 6):
        seg = "".join("{:02x}".format(urandom.getrandbits(8)) for _ in range(n))
        parts.append(seg)
    return "-".join(parts)


def ws_connect(host, port, path, headers, timeout=30, ca_path=None):
    """Establish a WebSocket connection. Returns WSConnection."""
    t_start = time.ticks_ms()
    raw = None
    try:
        try:
            addr = socket.getaddrinfo(host, port)[0][-1]
        except OSError as e:
            raise WSError("DNS lookup failed for '{}': {}".format(host, e))
        t_dns = time.ticks_ms()
        raw = socket.socket()
        raw.settimeout(timeout)
        raw.connect(addr)
        t_tcp = time.ticks_ms()
        sock = _wrap_tls(raw, host, ca_path)
        t_tls = time.ticks_ms()
    except OSError as e:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        raise WSError("connect failed: {}".format(e))

    try:
        key = _ws_key()
        req_lines = [
            "GET {} HTTP/1.1".format(path),
            "Host: {}".format(host),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: {}".format(key),
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in headers.items():
            req_lines.append("{}: {}".format(k, v))
        req = "\r\n".join(req_lines) + "\r\n\r\n"
        send_all(sock, req.encode("utf-8"))

        # Read HTTP response headers
        resp_buf = bytearray()
        while b"\r\n\r\n" not in resp_buf:
            try:
                chunk = sock.recv(1024)
            except OSError as e:
                raise WSError("handshake read failed: {}".format(e))
            if not chunk:
                raise WSError("connection closed during handshake")
            resp_buf.extend(chunk)

        hend = resp_buf.find(b"\r\n\r\n")
        status_line = resp_buf[:hend].split(b"\r\n")[0].decode("utf-8")
        if "101" not in status_line:
            raise WSError("handshake failed: {}".format(status_line))
        leftover = bytes(resp_buf[hend + 4 :])
    except Exception:
        # Close the socket on any handshake failure so the fd is not leaked.
        try:
            sock.close()
        except OSError:
            pass
        raise

    # The split matters: tls is mbedTLS CPU on the ESP32, dns/tcp/upgrade are
    # network round-trips. Which one dominates decides whether a voice turn
    # needs a pre-warmed connection or a faster cipher suite.
    log.timing(
        "WS",
        "{}:{} dns={}ms tcp={}ms tls={}ms upgrade={}ms total={}ms".format(
            host,
            port,
            time.ticks_diff(t_dns, t_start),
            time.ticks_diff(t_tcp, t_dns),
            time.ticks_diff(t_tls, t_tcp),
            ms_since(t_tls),
            ms_since(t_start),
        ),
    )
    return WSConnection(sock, leftover)
