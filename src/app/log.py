"""Minimal timestamped logging for MicroPython.

MicroPython has no stdlib logging (micropython-lib ships one, but it is
not frozen into the firmware). This project keeps its own tiny version
to stay dependency-free on device.

Usage:
    import app.log as log
    log.info("Voice", "Wake word detected")
    -> [2026-08-08 21:03:44.123] [Voice] Wake word detected

Before NTP sync the timestamp degrades to seconds since boot.
"""

import time

import app.config as config

# localtime() year before NTP sync (ESP32 boots at its epoch).
_SYNC_MIN_YEAR = 2023
# How often the wall clock is re-read so an NTP correction is picked up; in
# between, the timestamp advances on ticks_ms() alone.
_WALL_RESYNC_MS = 30000

_wall_base_ms = None
_wall_ticks = None


def _wall_ms():
    """Milliseconds since the epoch, derived from one monotonic reading.

    ``localtime()`` only resolves whole seconds, so pairing its seconds with a
    separately read ``ticks_ms() % 1000`` lets a timestamp go backwards across
    a second boundary (a real log showed ``16.967`` followed by ``16.352``).
    Anchoring the wall clock to ``ticks_ms()`` and deriving both parts from the
    same tick count keeps the sequence monotonic.
    """
    global _wall_base_ms, _wall_ticks
    ticks = time.ticks_ms()
    if _wall_base_ms is None or time.ticks_diff(ticks, _wall_ticks) >= _WALL_RESYNC_MS:
        # int(float) on the unix port, int on device; *1000 keeps the
        # sub-second phase where time.time() reports it.
        _wall_base_ms = int(time.time() * 1000)
        _wall_ticks = ticks
    return _wall_base_ms + time.ticks_diff(ticks, _wall_ticks)


def _ts():
    total_ms = _wall_ms()
    sec = total_ms // 1000
    ms = total_ms % 1000
    t = time.localtime(sec)
    if t[0] < _SYNC_MIN_YEAR:
        return "+{}.{:03d}s".format(sec, ms)
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}.{:03d}".format(
        t[0], t[1], t[2], t[3], t[4], t[5], ms
    )


def info(tag, msg):
    print("[{}] [{}] {}".format(_ts(), tag, msg))


def warn(tag, msg):
    print("[{}] [{}] WARN {}".format(_ts(), tag, msg))


def error(tag, msg):
    print("[{}] [{}] ERROR {}".format(_ts(), tag, msg))


def timing(tag, detail):
    """Log one latency-breakdown line, e.g. ``timing: dns=12ms tls=880ms``.

    Every instrumented stage of a voice turn goes through here so the lines
    share one greppable prefix and one switch (``config.TIMING_LOG``). The
    caller formats ``detail``; the microseconds that costs while logging is off
    are noise next to the network calls being measured.
    """
    if config.TIMING_LOG:
        print("[{}] [{}] timing: {}".format(_ts(), tag, detail))
