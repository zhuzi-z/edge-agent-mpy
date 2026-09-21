"""machine module shim (shared pin state, reserved-pin skip, counted reboots)."""

_PIN_STATES = {}
_RESERVED_PINS = set(range(22, 38))
# machine.reset() would reboot the device; here it is counted so a test can
# assert that an OTA install (or a failed trial boot) asked for one.
_RESETS = 0


def reset_pin_states():
    _PIN_STATES.clear()


def reset():
    global _RESETS
    _RESETS += 1


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 1
    PULL_DOWN = 2

    def __init__(self, pin_id, mode=-1, pull=-1, *, value=None):
        if pin_id in _RESERVED_PINS:
            raise ValueError("invalid pin: {}".format(pin_id))
        self._n = pin_id
        _PIN_STATES.setdefault(pin_id, 0)
        if mode == Pin.OUT and value is not None:
            _PIN_STATES[pin_id] = 1 if value else 0

    def init(self, mode=-1, pull=-1, *, value=None):
        if value is not None:
            _PIN_STATES[self._n] = 1 if value else 0

    def value(self, x=None):
        if x is None:
            return _PIN_STATES.get(self._n, 0)
        _PIN_STATES[self._n] = 1 if x else 0

    def on(self):
        _PIN_STATES[self._n] = 1

    def off(self):
        _PIN_STATES[self._n] = 0

    def __repr__(self):
        return "Pin({})".format(self._n)


class PWM:
    def __init__(self, pin, freq=5000, duty=0):
        self._pin = pin
        self._freq = freq
        self._duty = duty

    def freq(self, f=None):
        if f is None:
            return self._freq
        self._freq = f

    def duty(self, d=None):
        if d is None:
            return self._duty
        self._duty = d

    def deinit(self):
        pass


class RTC:
    def datetime(self, dt=None):
        import time

        t = time.localtime()
        return (t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0)


def unique_id():
    return b"\x98\x88\xe0\x11\xf4\x80"


def freq():
    return 240000000


class WDT:
    """Watchdog stub: records feeds without resetting the unix process."""

    def __init__(self, timeout=30000):
        self.timeout = timeout
        self.feeds = 0

    def feed(self):
        self.feeds += 1


class I2S:
    """I2S stub for unix port (no real hardware)."""

    RX = 0
    TX = 1
    MONO = 0
    STEREO = 1

    def __init__(self, port, **kwargs):
        self._port = port

    def readinto(self, buf):
        return 0

    def write(self, data):
        return len(data)

    def deinit(self):
        pass
