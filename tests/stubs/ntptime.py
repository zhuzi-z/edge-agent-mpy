"""``ntptime`` module shim for the MicroPython unix port.

On the ESP32 firmware ``ntptime.settime()`` syncs the RTC (and notably fixes
the broken ``time.time()`` -- see the mijia skill notes). On the unix port
``time.time()`` is already the correct host epoch, so there is nothing to sync.
"""

host = "pool.ntp.org"


def settime():
    pass


def time():
    import time

    return time.time()
