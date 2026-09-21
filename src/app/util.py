"""Shared utilities: HTTP responses, dir creation, socket send."""

import json
import os
import time
import asyncio
import _thread

import app.config as config

# Uptime accumulator, see uptime_seconds().
_uptime_ms = 0
_last_ticks = time.ticks_ms()


def json_response(status_code, status_text, obj):
    return (status_code, status_text, "application/json", json.dumps(obj))


def uptime_seconds():
    """Seconds since boot, robust to ``time.ticks_ms()`` wraparound.

    ``ticks_ms()`` is a wrapping counter (about 12.4 days on ESP32), so the
    obvious ``ticks_ms() // 1000`` silently restarts from zero on a device left
    running. Accumulating ``ticks_diff()`` between calls keeps counting; it
    stays exact as long as this is called at least once per half-wrap, which the
    main loop's maintenance pass guarantees.
    """
    global _uptime_ms, _last_ticks
    now = time.ticks_ms()
    _uptime_ms += time.ticks_diff(now, _last_ticks)
    _last_ticks = now
    return _uptime_ms // 1000


def ms_since(ticks):
    """Milliseconds elapsed since ``ticks`` (a ``time.ticks_ms()`` reading).

    Wraparound-safe, so it is also what the latency logging uses.
    """
    return time.ticks_diff(time.ticks_ms(), ticks)


def latin1_decode(data):
    """Byte-wise latin-1 decode.

    MicroPython only ships utf-8/ascii codecs (and from v1.29 rejects any
    other encoding name), so map each byte to its code point manually.
    """
    return "".join(chr(b) for b in data)


def safe_decode(data, fallback=""):
    """Decode bytes via UTF-8, then latin-1, then ``fallback``."""
    try:
        return data.decode("utf-8")
    except (UnicodeError, LookupError):
        return latin1_decode(data) if fallback == "" else fallback


def parse_http_request_head(data):
    """Parse an HTTP request head (bytes before the blank line).

    Returns (method, path, content_length), or None when the request
    line is malformed. The path has its query string stripped.
    """
    lines = safe_decode(data).split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None
    method = parts[0]
    path = parts[1].split("?")[0]
    content_length = 0
    for line in lines[1:]:
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":")[1].strip())
            except ValueError:
                pass
            break
    return method, path, content_length


def send_all(sock, data):
    """Write all of ``data`` to ``sock`` (MicroPython usocket has no ``sendall``).

    Mirrors CPython's ``socket.sendall``: loops ``send`` until every byte is
    dispatched, raising OSError if the peer closes or a send fails.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    view = memoryview(data)
    off = 0
    while off < len(view):
        sent = sock.send(view[off:])
        if not sent:
            raise OSError("socket send failed (peer closed?)")
        off += sent


async def run_sync(fn, *args, **kwargs):
    """Run a blocking callable in a short-lived worker thread.

    The event loop polls a completion flag instead of waiting on an event set
    from the worker: MicroPython's asyncio primitives are not thread-safe. The
    GIL makes the flag handoff safe, while the periodic sleep keeps HTTP and
    voice maintenance responsive during provider and skill calls.
    """
    stack_size = kwargs.pop("stack_size", config.BACKGROUND_THREAD_STACK_SIZE)
    poll_sec = kwargs.pop("poll_sec", 0.05)
    state = {"done": False, "value": None, "error": None}

    def worker():
        try:
            state["value"] = fn(*args, **kwargs)
        except BaseException as exc:
            state["error"] = exc
        finally:
            state["done"] = True

    if stack_size:
        _thread.stack_size(stack_size)
    _thread.start_new_thread(worker, ())
    while not state["done"]:
        await asyncio.sleep(poll_sec)
    if state["error"] is not None:
        raise state["error"]
    return state["value"]


def ensure_dir(path):
    """Create a directory and any missing parents (``mkdir -p``).

    MicroPython's ``os.mkdir`` only creates a single level and raises
    when a parent is missing, so this walks the path and creates each
    level best-effort (e.g. first boot before /config exists).
    Handles absolute and relative paths.
    """
    path = path.rstrip("/")
    if not path:
        return
    parts = [p for p in path.split("/") if p]
    cur = "/" if path.startswith("/") else ""
    for part in parts:
        if cur and not cur.endswith("/"):
            cur += "/"
        cur += part
        try:
            os.mkdir(cur)
        except OSError:
            pass


def ensure_parent_dir(file_path):
    """Ensure the parent directory of a file path exists."""
    parts = file_path.rsplit("/", 1)
    if len(parts) == 2 and parts[0]:
        ensure_dir(parts[0])
