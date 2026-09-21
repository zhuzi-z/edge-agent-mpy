"""Socket/tls shims for the MicroPython unix port tests.

The unix port now uses its native ``socket`` (usocket) directly — that is
what the real ``asyncio`` scheduler needs (its select poller requires a
native stream object, which a pure-Python wrapper cannot be).  The only
missing convenience was ``sendall``; app code uses ``app.util.send_all``
instead, which works on both the device and the unix port.

``tls`` (the firmware's name for its mbedTLS module) is provided by
``tests/stubs/tls.py`` and wraps the unix port's native ``ssl`` module.
"""
