"""Outbound HTTP/1.1 client over TLS (Mbed TLS) for ESP32."""

import socket
import tls
import time
import asyncio
import _thread
import app.config as config
import app.log as log
from app.util import send_all, safe_decode, ms_since


class HttpError(Exception):
    """Transport/protocol failure or oversized response."""


def _wrap_tls(raw_sock, host, ca_path=None):
    """Wrap socket in TLS, verifying only when an explicit CA is configured."""
    ctx = tls.SSLContext(tls.PROTOCOL_TLS_CLIENT)
    if ca_path:
        try:
            with open(ca_path, "rb") as handle:
                ctx.load_verify_locations(handle.read())
            ctx.verify_mode = tls.CERT_REQUIRED
        except Exception as e:  # noqa: BLE001
            raise HttpError("cannot load TLS CA {}: {}".format(ca_path, e))
    else:
        ctx.verify_mode = tls.CERT_NONE
    return ctx.wrap_socket(raw_sock, server_hostname=host)


def _send_body(s, body_bytes, reused=False):
    try:
        send_all(s, body_bytes)
    except OSError as e:
        _fail(reused, "send body failed: {}".format(e))


def _build_request_head(method, path, host, headers, body_bytes, close=True):
    """Build the request line + headers (through the blank line) as bytes."""
    parts = [
        method.encode(),
        b" ",
        path.encode(),
        b" HTTP/1.1\r\n",
        b"Host: ",
        host.encode(),
        b"\r\n",
    ]
    for k, v in headers.items():
        v = v.encode() if isinstance(v, str) else v
        parts.extend([k.encode(), b": ", v, b"\r\n"])
    if body_bytes is not None:
        parts.extend([b"Content-Length: ", str(len(body_bytes)).encode(), b"\r\n"])
    parts.append(b"Connection: close\r\n\r\n" if close else b"Connection: keep-alive\r\n\r\n")
    return b"".join(parts)


def _wants_close(header_blob):
    """True when the server asked us not to reuse the connection."""
    for line in header_blob.split("\r\n")[1:]:
        low = line.lower()
        if low.startswith("connection:") and "close" in low:
            return True
    return False


def _parse_response_head(header_blob):
    """Parse status line and body framing from response headers.

    Returns (status_code, content_length, chunked); content_length is
    None when the header is absent.
    """
    status_line = header_blob.split("\r\n")[0]
    try:
        status_code = int(status_line.split(" ")[1])
    except (IndexError, ValueError):
        raise HttpError("could not parse status line: " + status_line)

    content_length = None
    chunked = False
    for line in header_blob.split("\r\n")[1:]:
        low = line.lower()
        if low.startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif low.startswith("transfer-encoding:") and "chunked" in low:
            chunked = True
    return status_code, content_length, chunked


# ---------------------------------------------------------------------------
# Keep-alive cache (sync client)
#
# One warm connection, single slot on purpose: each cached TLS socket pins
# tens of KB of internal RAM, and on the sync path one endpoint (the LLM)
# dominates. Access is serialised because skill calls can come from more than
# one thread. Opt-in via keep_alive=True so the behaviour of every other
# caller is untouched.
# ---------------------------------------------------------------------------

_conn_lock = _thread.allocate_lock()
_cached_conn = None  # (key, tls_sock, raw_sock, used_ticks)


class _StaleReuse(Exception):
    """A cached connection died before any response byte; retry on a fresh one."""


def _fail(reused, message):
    """Raise the retryable stale error on a reused connection, else HttpError."""
    if reused:
        raise _StaleReuse(message)
    raise HttpError(message)


def _conn_close(entry):
    """Close both ends of a cache entry."""
    for obj in (entry[1], entry[2]):
        try:
            obj.close()
        except Exception:  # noqa: BLE001
            pass


def _conn_alive(sock):
    """Non-blocking peek: False once the peer has closed or desynced."""
    try:
        sock.setblocking(False)
        try:
            data = sock.recv(1)
        finally:
            sock.setblocking(True)
    except OSError:
        # EWOULDBLOCK: nothing pending, which is what a healthy idle
        # connection looks like.
        return True
    return bool(data)


def _conn_take(key):
    """Claim the warm connection for ``key``, or None. Closes what it rejects."""
    global _cached_conn
    if key is None:
        return None
    with _conn_lock:
        entry = _cached_conn
        _cached_conn = None
    if entry is None:
        return None
    dead = (
        entry[0] != key
        or time.ticks_diff(time.ticks_ms(), entry[3]) > config.HTTP_KEEPALIVE_IDLE_MS
        or not _conn_alive(entry[1])
    )
    if dead:
        _conn_close(entry)
        return None
    return entry


def _conn_put(key, sock, raw, reusable):
    """Return the connection to the cache, or close it."""
    global _cached_conn
    if key is None or not reusable:
        _conn_close((key, sock, raw, 0))
        return
    with _conn_lock:
        old = _cached_conn
        _cached_conn = (key, sock, raw, time.ticks_ms())
    if old is not None:
        _conn_close(old)


def close_keepalive():
    """Drop any cached connection (endpoint or network change, tests)."""
    global _cached_conn
    with _conn_lock:
        entry = _cached_conn
        _cached_conn = None
    if entry is not None:
        _conn_close(entry)


def https_request(
    host,
    port,
    method,
    path,
    headers,
    body_bytes,
    timeout=config.LLM_REQUEST_TIMEOUT_SEC,
    max_bytes=config.LLM_MAX_RESPONSE_BYTES,
    ca_path=None,
    keep_alive=False,
):
    """HTTPS request. Returns (status_code, header_str, body_bytes).

    ``keep_alive`` reuses the previous connection to the same endpoint and so
    skips dns+tcp+tls entirely; a cached connection that died while idle is
    replaced transparently, so the caller sees one behaviour either way.
    """
    key = (host, port, ca_path) if keep_alive else None
    entry = _conn_take(key)
    if entry is not None:
        try:
            return _exchange_sync(
                entry,
                host,
                port,
                method,
                path,
                headers,
                body_bytes,
                timeout,
                max_bytes,
                reused=True,
            )
        except _StaleReuse:
            pass  # closed already; fall through to a fresh connection

    t_start = time.ticks_ms()
    addr = socket.getaddrinfo(host, port)[0][-1]
    t_dns = time.ticks_ms()
    raw = socket.socket()
    raw.settimeout(timeout)
    try:
        raw.connect(addr)
    except OSError as e:
        raw.close()
        raise HttpError("connect failed: {}".format(e))
    t_tcp = time.ticks_ms()
    try:
        sock = _wrap_tls(raw, host, ca_path)
    except OSError as e:
        raw.close()
        raise HttpError("TLS handshake failed: {}".format(e))
    t_tls = time.ticks_ms()
    return _exchange_sync(
        (key, sock, raw, t_start),
        host,
        port,
        method,
        path,
        headers,
        body_bytes,
        timeout,
        max_bytes,
        t0=t_start,
        dns_ms=time.ticks_diff(t_dns, t_start),
        tcp_ms=time.ticks_diff(t_tcp, t_dns),
        tls_ms=time.ticks_diff(t_tls, t_tcp),
    )


def _exchange_sync(
    entry,
    host,
    port,
    method,
    path,
    headers,
    body_bytes,
    timeout,
    max_bytes,
    t0=None,
    reused=False,
    dns_ms=0,
    tcp_ms=0,
    tls_ms=0,
):
    """One request/response over an established connection.

    Owns ``entry``: on return the connection is either back in the keep-alive
    cache or closed. Raises _StaleReuse (after closing) when a reused
    connection died before any response byte - the one failure a fresh
    connection can actually fix. A timeout is deliberately *not* treated as
    stale, so a slow server is not waited on twice.
    """
    key, s, raw = entry[0], entry[1], entry[2]
    t_exch = time.ticks_ms()
    if t0 is None:
        t0 = t_exch
    reusable = False
    try:
        raw.settimeout(timeout)
        head = _build_request_head(method, path, host, headers, body_bytes, close=key is None)
        try:
            send_all(s, head)
        except OSError as e:
            _fail(reused, "send headers failed: {}".format(e))

        buf = bytearray()
        if body_bytes:
            _send_body(s, body_bytes, reused)
        t_sent = time.ticks_ms()

        while b"\r\n\r\n" not in buf:
            try:
                chunk = s.recv(config.HTTP_RECV_BUF_SIZE)
            except OSError as e:
                raise HttpError("timeout/closed reading headers: {}".format(e))
            if not chunk:
                _fail(reused, "connection closed reading headers")
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise HttpError("headers exceed {} bytes".format(max_bytes))
        # First byte of the reply: for an LLM call this is the whole generation
        # time, which is why it is reported separately from the transfer.
        t_first = time.ticks_ms()

        hend = buf.find(b"\r\n\r\n")
        header_blob = safe_decode(bytes(buf[:hend]))
        body = bytearray(buf[hend + 4 :])

        status_code, content_length, chunked = _parse_response_head(header_blob)

        # Tolerate recv timeouts: some servers ignore Connection: close
        if chunked:
            body = _read_chunked_stream(s, body, max_bytes)
        elif content_length is not None:
            while len(body) < content_length:
                try:
                    chunk = s.recv(min(config.HTTP_RECV_BUF_SIZE, content_length - len(body)))
                except OSError:
                    break
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise HttpError("body exceeds {} bytes".format(max_bytes))
        else:
            while True:
                try:
                    chunk = s.recv(config.HTTP_RECV_BUF_SIZE)
                except OSError:
                    break
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise HttpError("body exceeds {} bytes".format(max_bytes))

        # Reusable only when the framing is exact: a chunked read stops on the
        # terminator but may have swallowed bytes of the next response, and an
        # EOF-terminated body means the peer is done with the connection.
        reusable = (
            key is not None
            and not chunked
            and content_length is not None
            and len(body) >= content_length
            and not _wants_close(header_blob)
        )

        log.timing(
            "HTTP",
            "{} {} status={} reused={} dns={}ms tcp={}ms tls={}ms send={}ms ttfb={}ms "
            "recv={}ms body={} total={}ms".format(
                method,
                path,
                status_code,
                1 if reused else 0,
                dns_ms,
                tcp_ms,
                tls_ms,
                time.ticks_diff(t_sent, t_exch),
                time.ticks_diff(t_first, t_sent),
                ms_since(t_first),
                len(body),
                ms_since(t0),
            ),
        )
        return status_code, header_blob, bytes(body)
    finally:
        _conn_put(key, s, raw, reusable)


_CHUNK_TERM = b"\r\n0\r\n\r\n"


def _read_chunked_stream(s, initial, max_bytes):
    """Read chunked body until terminator.

    The terminator can straddle two reads, so each pass re-scans only its last
    few bytes plus what just arrived instead of the whole body so far.
    """
    data = bytearray(initial)
    scan_at = 0
    while True:
        if _CHUNK_TERM in data[scan_at:]:
            break
        scan_at = max(0, len(data) - (len(_CHUNK_TERM) - 1))
        try:
            chunk = s.recv(config.HTTP_RECV_BUF_SIZE)
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HttpError("chunked body exceeds {} bytes".format(max_bytes))
    return _decode_chunked(bytes(data), max_bytes)


def _decode_chunked(data, max_bytes):
    """Decode chunked transfer-encoding body."""
    out = bytearray()
    i = 0
    n = len(data)
    while True:
        j = data.find(b"\r\n", i)
        if j == -1:
            break
        try:
            size = int(data[i:j].strip().split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = j + 2
        out.extend(data[start : start + size])
        if len(out) > max_bytes:
            raise HttpError("chunked body exceeds {} bytes".format(max_bytes))
        i = start + size + 2
        if i > n:
            break
    return bytes(out)


def https_post_json(host, port, path, headers, body_bytes, **kw):
    """POST JSON over HTTPS."""
    h = dict(headers)
    h["Content-Type"] = "application/json"
    h["Accept"] = "application/json"
    return https_request(host, port, "POST", path, h, body_bytes, **kw)


def https_get(host, port, path, headers=None, **kw):
    """GET over HTTPS."""
    return https_request(host, port, "GET", path, headers or {}, None, **kw)


# ---------------------------------------------------------------------------
# Async client (asyncio) — used on the event-loop thread so a slow LLM call
# doesn't block other HTTP handlers.
# ---------------------------------------------------------------------------

_async_cached_conn = None
_async_conn_lock = asyncio.Lock()


def _make_tls_context(ca_path):
    """Build an SSLContext for ``asyncio.open_connection(ssl=ctx)``."""
    ctx = tls.SSLContext(tls.PROTOCOL_TLS_CLIENT)
    if ca_path:
        try:
            with open(ca_path, "rb") as handle:
                ctx.load_verify_locations(handle.read())
            ctx.verify_mode = tls.CERT_REQUIRED
        except Exception as e:  # noqa: BLE001
            raise HttpError("cannot load TLS CA {}: {}".format(ca_path, e))
    else:
        ctx.verify_mode = tls.CERT_NONE
    return ctx


async def _async_conn_close(entry):
    if entry is None:
        return
    try:
        entry[2].close()
        await entry[2].wait_closed()
    except Exception:  # noqa: BLE001
        pass


async def _async_conn_take(key):
    """Claim the single async keep-alive connection, if it matches."""
    global _async_cached_conn
    if key is None:
        return None
    await _async_conn_lock.acquire()
    try:
        entry = _async_cached_conn
        _async_cached_conn = None
    finally:
        _async_conn_lock.release()
    if entry is None:
        return None
    if (
        entry[0] != key
        or time.ticks_diff(time.ticks_ms(), entry[3]) > config.HTTP_KEEPALIVE_IDLE_MS
    ):
        await _async_conn_close(entry)
        return None
    return entry


async def _async_conn_put(entry, reusable):
    """Return the connection to the cache, or close it."""
    global _async_cached_conn
    if entry[0] is None or not reusable:
        await _async_conn_close(entry)
        return
    await _async_conn_lock.acquire()
    try:
        old = _async_cached_conn
        _async_cached_conn = (entry[0], entry[1], entry[2], time.ticks_ms())
    finally:
        _async_conn_lock.release()
    await _async_conn_close(old)


async def close_keepalive_async():
    """Drop the cached async connection (tests and endpoint changes)."""
    global _async_cached_conn
    await _async_conn_lock.acquire()
    try:
        entry = _async_cached_conn
        _async_cached_conn = None
    finally:
        _async_conn_lock.release()
    await _async_conn_close(entry)


async def https_request_async(
    host,
    port,
    method,
    path,
    headers,
    body_bytes,
    timeout=None,
    max_bytes=config.LLM_MAX_RESPONSE_BYTES,
    ca_path=None,
    keep_alive=False,
):
    """Async HTTPS request. Returns (status_code, header_blob, body_bytes).

    ``keep_alive`` reuses one TLS connection to the same endpoint.
    A connection that died while idle is retried once on a fresh socket.
    """
    key = (host, port, ca_path) if keep_alive else None
    entry = await _async_conn_take(key)
    if entry is not None:
        try:
            result = await asyncio.wait_for(
                _exchange_https(
                    entry[1],
                    entry[2],
                    host,
                    method,
                    path,
                    headers,
                    body_bytes,
                    max_bytes,
                    key=key,
                    reused=True,
                ),
                timeout,
            )
            await _async_conn_put(entry, result[3])
            return result[0], result[1], result[2]
        except _StaleReuse:
            await _async_conn_close(entry)

    try:
        reader, writer = await asyncio.open_connection(host, port, ssl=_make_tls_context(ca_path))
    except OSError as e:
        raise HttpError("connect failed: {}".format(e))
    entry = (key, reader, writer, time.ticks_ms())
    try:
        result = await asyncio.wait_for(
            _exchange_https(
                reader,
                writer,
                host,
                method,
                path,
                headers,
                body_bytes,
                max_bytes,
                key=key,
                reused=False,
            ),
            timeout,
        )
    except asyncio.TimeoutError:
        raise HttpError("request timed out after {}s".format(timeout))
    except Exception:
        await _async_conn_close(entry)
        raise
    await _async_conn_put(entry, result[3])
    return result[0], result[1], result[2]


async def _exchange_https(
    reader, writer, host, method, path, headers, body_bytes, max_bytes, key=None, reused=False
):
    """Send the request and read the response on an established connection."""
    try:
        writer.write(
            _build_request_head(method, path, host, headers, body_bytes, close=key is None)
        )
        if body_bytes:
            writer.write(body_bytes)
        await writer.drain()

        header_buf = bytearray()
        while b"\r\n\r\n" not in header_buf:
            chunk = await reader.read(config.HTTP_RECV_BUF_SIZE)
            if not chunk:
                if reused:
                    raise _StaleReuse("connection closed reading headers")
                raise HttpError("connection closed reading headers")
            header_buf.extend(chunk)
            if len(header_buf) > max_bytes:
                raise HttpError("headers exceed {} bytes".format(max_bytes))

        hend = header_buf.find(b"\r\n\r\n")
        header_blob = safe_decode(bytes(header_buf[:hend]))
        body = bytearray(header_buf[hend + 4 :])

        status_code, content_length, chunked = _parse_response_head(header_blob)

        if chunked:
            body = await _read_chunked_async(reader, body, max_bytes)
        elif content_length is not None:
            while len(body) < content_length:
                chunk = await reader.read(
                    min(config.HTTP_RECV_BUF_SIZE, content_length - len(body))
                )
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise HttpError("body exceeds {} bytes".format(max_bytes))
        else:
            while True:
                chunk = await reader.read(config.HTTP_RECV_BUF_SIZE)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise HttpError("body exceeds {} bytes".format(max_bytes))

        reusable = (
            key is not None
            and not chunked
            and content_length is not None
            and len(body) >= content_length
            and not _wants_close(header_blob)
        )
        return status_code, header_blob, bytes(body), reusable
    except OSError as e:
        raise HttpError("connection error: {}".format(e))


async def _read_chunked_async(reader, initial, max_bytes):
    data = bytearray(initial)
    scan_at = 0
    while True:
        if _CHUNK_TERM in data[scan_at:]:
            break
        scan_at = max(0, len(data) - (len(_CHUNK_TERM) - 1))
        chunk = await reader.read(config.HTTP_RECV_BUF_SIZE)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HttpError("chunked body exceeds {} bytes".format(max_bytes))
    return _decode_chunked(bytes(data), max_bytes)


async def https_post_json_async(host, port, path, headers, body_bytes, **kw):
    """Async POST JSON over HTTPS."""
    h = dict(headers)
    h["Content-Type"] = "application/json"
    h["Accept"] = "application/json"
    return await https_request_async(host, port, "POST", path, h, body_bytes, **kw)
