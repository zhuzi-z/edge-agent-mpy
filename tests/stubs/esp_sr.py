"""esp_sr module stub for testing on the unix port.

Provides the same API surface as the firmware C module so that
``app.audio.wakeword`` can be exercised without real hardware.

For app-flow testing, call ``start_trigger_server(port)`` to accept
TCP connections that inject wake word events (simulating "Hi ESP").

The utterance-capture API (``capture``/``read``/``capture_stats``) is
modelled too: ``_vad(on, preroll)`` simulates an AFE VAD transition and
``_feed(pcm)`` an AFE frame, so the voice channel's AFE listening path can be
exercised without hardware.
"""

import _thread

EVENT_WAKEUP = 1
EVENT_VAD_ON = 2
EVENT_VAD_OFF = 3

_events = []
_initialized = False
_init_kwargs = {}
_trigger_port = None
_capture_on = False
_capture_ready = True
_capture = bytearray()
_capture_dropped = 0
_capture_frames = 0
_vad_speech = False


def init(**kwargs):
    global _initialized, _init_kwargs
    if _initialized:
        # The firmware C module refuses a second init() until deinit(); the
        # app relies on that to notice a stale instance left by a soft reboot.
        raise RuntimeError("esp_sr already initialised")
    _initialized = True
    _init_kwargs = kwargs


def deinit():
    global _initialized
    _initialized = False


def poll():
    if _events:
        return _events.pop(0)
    return None


def on_event(cb):
    pass


def running():
    return _initialized


def capture(on):
    """Start/stop utterance buffering. False when capture is unavailable."""
    global _capture, _capture_on, _capture_dropped, _capture_frames, _vad_speech
    _capture_on = bool(on)
    if not on:
        return True
    if not _capture_ready or not _initialized:
        _capture_on = False
        return False
    _capture = bytearray()
    _capture_dropped = 0
    _capture_frames = 0
    # Arming restarts the VAD, exactly as the firmware does: the segment the
    # wake word or a playback tail left open is dropped rather than announced,
    # so only a real onset opens the next one.
    _vad_speech = False
    return True


def read(nbytes=1024, timeout_ms=200):
    """Captured PCM; b"" when the buffer is empty (never waits, unlike C)."""
    global _capture
    if not _capture:
        return b""
    n = min(nbytes, len(_capture))
    out = bytes(_capture[:n])
    _capture = _capture[n:]
    return out


def capture_stats():
    """(frames seen, bytes dropped) since capture(True), as the firmware reports."""
    return (_capture_frames, _capture_dropped)


# -- Test helpers (unit tests) -----------------------------------------------


def _inject(event, value=0):
    """Queue a fake event for poll() to return."""
    _events.append((event, value))


def _vad(on, preroll=b""):
    """Simulate an AFE VAD transition, buffering audio as the firmware does."""
    global _vad_speech
    if on == _vad_speech:
        return
    _vad_speech = on
    if on:
        if _capture_on:
            _capture.extend(preroll)
        _inject(EVENT_VAD_ON, 0)
    else:
        _inject(EVENT_VAD_OFF, 0)


def _feed(pcm):
    """Simulate one AFE frame: counted always, buffered only during speech."""
    global _capture_frames
    if not _capture_on:
        return
    _capture_frames += 1
    if _vad_speech:
        _capture.extend(pcm)


def _reset():
    """Clear all state."""
    global _initialized, _init_kwargs, _capture, _capture_on, _capture_ready
    global _capture_dropped, _capture_frames, _vad_speech
    _events.clear()
    _initialized = False
    _init_kwargs = {}
    _capture = bytearray()
    _capture_on = False
    _capture_ready = True
    _capture_dropped = 0
    _capture_frames = 0
    _vad_speech = False


# -- App-flow trigger server --------------------------------------------------


def start_trigger_server(port):
    """Start a TCP server that injects EVENT_WAKEUP on connection.

    The external CPython orchestrator connects and sends the wake word
    text (e.g. "Hi ESP"). We inject the wakeup event regardless of the
    text content (real WakeNet matching is hardware-level).
    """
    global _trigger_port
    _trigger_port = port

    import usocket as socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(socket.getaddrinfo("0.0.0.0", port)[0][-1])
    srv.listen(4)
    srv.settimeout(1.0)
    print("[esp_sr stub] Trigger server on port {}".format(port))

    def _loop():
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                continue
            try:
                data = conn.recv(128)
                word = data.decode("utf-8").strip() if data else "?"
                print("[esp_sr stub] Wake word received: '{}'".format(word))
                _inject(EVENT_WAKEUP, 1)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    _thread.start_new_thread(_loop, ())
