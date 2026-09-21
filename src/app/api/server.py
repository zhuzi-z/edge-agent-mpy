"""HTTP server with route-based request handling (asyncio)."""

import os
import json
import gc
import sys
import binascii
import asyncio
import app.config as config
import app.log as log
from app.controllers import read_gpio_states
from app.util import parse_http_request_head, safe_decode, uptime_seconds


_index_cache = None  # (etag, body_bytes); the file only changes on redeploy


def _index_cached():
    """(etag, body_bytes) for the WebUI, read and digested once per process.

    Serving from the cached bytes keeps every GET / from re-encoding the whole
    page, and the ETag from re-running crc32 over it.
    """
    global _index_cache
    if _index_cache is None:
        try:
            with open(config.WEB_UI_PATH, "rb") as handle:
                body = handle.read()
        except OSError:
            return None
        _index_cache = ('"{:x}"'.format(binascii.crc32(body)), body)
    return _index_cache


def route_index_etag():
    """Strong ETag for the process-lifetime-cached WebUI."""
    cached = _index_cached()
    return cached[0] if cached is not None else None


def _request_header(head, name):
    """Return one request header, or None, from the decoded request head."""
    wanted = name.lower()
    for line in safe_decode(head).split("\r\n")[1:]:
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == wanted:
            return value.strip()
    return None


class BodyTooLargeError(Exception):
    """Request body larger than ``config.HTTP_MAX_BODY_SIZE``."""


async def route_index(server, method, path, body):
    """GET / -- serve web UI from flash (cached for the process lifetime:
    the file only changes on redeploy, which resets the device)."""
    cached = _index_cached()
    if cached is None:
        return (404, "Not Found", "text/plain", "web UI not found on device")
    return (200, "OK", "text/html; charset=utf-8", cached[1])


def _heap_regions():
    """System heap (PSRAM/SRAM) usage totals. ESP32-only; empty elsewhere.

    IDF heap regions are merged into one PSRAM and one SRAM entry: the largest
    region (>=1MiB) counts as PSRAM, all others as SRAM.
    """
    try:
        import esp32
    except ImportError:
        return []
    try:
        raw = esp32.idf_heap_info(esp32.HEAP_DATA)
    except Exception:
        return []
    regions = []
    for total, free, largest, min_free in raw:
        if total <= 0:
            continue
        regions.append((total, free))
    regions.sort(key=lambda r: r[0], reverse=True)
    merged = {}
    for i, (total, free) in enumerate(regions):
        name = "PSRAM" if i == 0 and total >= 1048576 else "SRAM"
        entry = merged.get(name)
        if entry is None:
            merged[name] = {
                "name": name,
                "total_bytes": total,
                "free_bytes": free,
                "used_bytes": total - free,
            }
        else:
            entry["total_bytes"] += total
            entry["free_bytes"] += free
            entry["used_bytes"] += total - free
    return [merged[name] for name in ("PSRAM", "SRAM") if name in merged]


async def route_info(server, method, path, body):
    """GET /info -- device info as JSON."""
    try:
        stat = os.statvfs("/")
        disk_total = stat[2] * stat[1]
        disk_free = stat[3] * stat[1]
        disk_used = disk_total - disk_free
    except OSError:
        disk_total = 0
        disk_free = 0
        disk_used = 0

    # Live heap readings, deliberately without a gc.collect() first: the WebUI
    # polls this every few seconds, and a full collection pause lands right in
    # the middle of the audio thread's chunk processing.
    mem_free = gc.mem_free()
    mem_alloc = gc.mem_alloc()
    mem_total = mem_free + mem_alloc

    data = {
        "name": "Edge Agent",
        "version": "1.0.0",
        "chip": "ESP32-S3",
        "micropython": "{}.{}.{}".format(*sys.implementation.version[:3]),
        "uptime_seconds": uptime_seconds(),
        "wifi_connected": "true" if server._wifi.is_connected() else "false",
        "memory": {"total_bytes": mem_total, "free_bytes": mem_free, "used_bytes": mem_alloc},
        "disk": {"total_bytes": disk_total, "free_bytes": disk_free, "used_bytes": disk_used},
        "heap": _heap_regions(),
        "gpio": read_gpio_states(
            server._control.gpio_status_pins()
            if server._control is not None
            else config.GPIO_STATUS_PINS
        ),
    }
    return (200, "OK", "application/json", json.dumps(data))


class HTTPServer:
    """Route-based HTTP server backed by ``asyncio.start_server``.

    Each connection runs as an asyncio task, so a slow client no longer
    blocks the whole server.  Handlers are async and return the same
    ``(status, text, content_type, body)`` tuple as before.

    A stream route is the exception: it is handed the socket reader instead of
    a body, because its body is bigger than the device can hold (an OTA bundle)
    and is binary, which the text every other route gets would corrupt.
    """

    def __init__(self, wifi, port=config.HTTP_PORT, skills=None, control=None):
        self._server = None
        self._wifi = wifi
        self._control = control
        self._port = port
        self._routes = {}
        self._stream_routes = {}
        self._skills = skills

    def register_route(self, method, path, handler):
        self._routes[(method.upper(), path)] = handler

    def register_stream_route(self, method, path, handler):
        """Register ``handler(server, reader, content_length, extra)``."""
        self._stream_routes[(method.upper(), path)] = handler

    async def start(self):
        """Bind and listen."""
        if self._server is not None:
            return
        try:
            self._server = await asyncio.start_server(
                self._handle_client, "0.0.0.0", self._port, backlog=config.HTTP_SERVER_BACKLOG
            )
            log.info("Server", "Listening on :{}".format(self._port))
        except OSError as e:
            log.error("Server", "start failed: {}".format(e))
            self._server = None

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            log.info("Server", "Stopped")

    def is_running(self):
        return self._server is not None

    async def _handle_client(self, reader, writer):
        try:
            # Bounded: a client that connects and then goes quiet would
            # otherwise pin its task and socket forever, and lwIP has only
            # CONFIG_LWIP_MAX_SOCKETS (10) of them to go round.
            try:
                head, extra = await asyncio.wait_for(
                    self._read_head(reader), config.HTTP_CLIENT_READ_TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                log.warn(
                    "HTTP",
                    "no request within {}s, dropping".format(config.HTTP_CLIENT_READ_TIMEOUT_SEC),
                )
                return
            if head is None:
                return
            request_head = head[:-4]  # drop the blank line
            parsed = parse_http_request_head(request_head)
            if parsed is None:
                return
            method, path, content_length = parsed

            log.info("HTTP", "{} {}".format(method, path))

            if method == "OPTIONS":
                # CORS preflight: lets browsers POST/DELETE cross-origin,
                # e.g. the WebUI opened from a local file:// copy.
                await self._send_preflight(writer)
                return

            index_etag = None
            if method == "GET" and path in ("/", "/index.html"):
                index_etag = route_index_etag()
                if (
                    index_etag is not None
                    and _request_header(request_head, "If-None-Match") == index_etag
                ):
                    await self._send_not_modified(writer, index_etag)
                    return

            stream = self._stream_routes.get((method, path))
            if stream is not None:
                status_code, status_text, content_type, resp_body = await stream(
                    self, reader, content_length, extra
                )
                await self._send_response(
                    writer, status_code, status_text, content_type, resp_body
                )
                return

            try:
                body = await asyncio.wait_for(
                    self._read_body(reader, content_length, extra),
                    config.HTTP_CLIENT_READ_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                log.warn(
                    "HTTP",
                    "no body within {}s, dropping".format(config.HTTP_CLIENT_READ_TIMEOUT_SEC),
                )
                return
            except BodyTooLargeError as e:
                # JSON like every other error, so the WebUI can show why an
                # upload was refused instead of failing to parse the reply.
                await self._send_response(
                    writer,
                    413,
                    "Payload Too Large",
                    "application/json",
                    json.dumps({"error": str(e)}),
                )
                return

            handler = self._routes.get((method, path))
            if handler is None and self._skills is not None:
                handler = self._skills.match_endpoint(method, path)
            if handler:
                status_code, status_text, content_type, resp_body = await handler(
                    self, method, path, body
                )
                extra_headers = None
                if index_etag is not None:
                    extra_headers = {
                        "ETag": index_etag,
                        "Cache-Control": "no-cache, must-revalidate",
                    }
                await self._send_response(
                    writer, status_code, status_text, content_type, resp_body, extra_headers
                )
            else:
                await self._send_response(writer, 404, "Not Found", "text/plain", "404 Not Found")
        except Exception as e:
            log.error("HTTP", "handle request: {}".format(e))
        finally:
            writer.close()
            await writer.wait_closed()

    async def _read_head(self, reader):
        """Read up to and including the blank line that ends the request head.

        Returns ``(head, extra)``, where ``extra`` is the body bytes the reads
        already pulled in past it, or ``(None, None)`` when the peer closed
        first or sent more head than ``config.HTTP_MAX_HEADER_SIZE``.
        """
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = await reader.read(config.HTTP_RECV_BUF_SIZE)
            if not chunk:
                return (None, None)
            request.extend(chunk)
            if len(request) > config.HTTP_MAX_HEADER_SIZE:
                return (None, None)
        hend = request.find(b"\r\n\r\n") + 4
        return (bytes(request[:hend]), bytes(request[hend:]))

    async def _read_body(self, reader, content_length, extra):
        """The rest of an ordinary request, as the text its handler expects."""
        if content_length > config.HTTP_MAX_BODY_SIZE:
            raise BodyTooLargeError(
                "body of {} bytes exceeds the {} byte limit".format(
                    content_length, config.HTTP_MAX_BODY_SIZE
                )
            )
        if content_length <= 0:
            return ""
        body = bytearray(extra[:content_length])
        if len(body) < content_length:
            body.extend(await reader.readexactly(content_length - len(body)))
        return safe_decode(bytes(body))

    async def _send_preflight(self, writer):
        """Answer a CORS preflight (OPTIONS) allowing any origin."""
        header = (
            "HTTP/1.1 204 No Content\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Private-Network: true\r\n"
            "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type\r\n"
            "Access-Control-Max-Age: 600\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n"
        )
        try:
            writer.write(header.encode("utf-8"))
            await writer.drain()
        except OSError:
            pass

    async def _send_response(
        self, writer, status_code, status_text, content_type, body, extra_headers=None
    ):
        return await self._send_response_with_headers(
            writer, status_code, status_text, content_type, body, extra_headers
        )

    async def _send_response_with_headers(
        self, writer, status_code, status_text, content_type, body, extra_headers=None
    ):
        try:
            body_bytes = body.encode("utf-8") if isinstance(body, str) else body
            header = "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store, no-cache, must-revalidate\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Private-Network: true\r\n".format(
                status_code, status_text, content_type, len(body_bytes)
            )
            if extra_headers:
                for name, value in extra_headers.items():
                    header += "{}: {}\r\n".format(name, value)
            header += "\r\n"
            writer.write(header.encode("utf-8") + body_bytes)
            await writer.drain()
        except OSError as e:
            log.error("HTTP", "send response: {}".format(e))

    async def _send_not_modified(self, writer, etag):
        try:
            header = (
                "HTTP/1.1 304 Not Modified\r\n"
                "ETag: {}\r\n"
                "Cache-Control: no-cache, must-revalidate\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            ).format(etag)
            writer.write(header.encode("utf-8"))
            await writer.drain()
        except OSError as e:
            log.error("HTTP", "send 304: {}".format(e))
