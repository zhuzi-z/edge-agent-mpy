"""Wake word detection: base interface + two implementations.

Implementations:
  - EspSrWakeWord: firmware ESP-SR (AFE + WakeNet), owns the I2S mic. With
    capture-capable firmware it also serves as the listening audio source:
    the AFE reports its VAD transitions and buffers the processed PCM of the
    utterance, which is the only way to record while the AFE holds the mic.
  - EnergyWakeWord: simple energy-threshold trigger, does NOT own the mic
    (caller feeds PCM chunks from AudioInput).

Factory ``create_wakeword()`` picks EspSrWakeWord when available,
otherwise falls back to EnergyWakeWord.
"""

import app.config as config
import app.log as log
from app.audio import pcm16_avg_abs

try:
    import esp_sr as _esp_sr
except ImportError:
    _esp_sr = None

# Firmware event codes -> names used inside the app, so callers never depend
# on the numeric values (or on the module being present at all).
_EVENT_NAMES = {}
if _esp_sr is not None:
    _EVENT_NAMES = {
        _esp_sr.EVENT_WAKEUP: "wakeup",
        _esp_sr.EVENT_VAD_ON: "vad_on",
        _esp_sr.EVENT_VAD_OFF: "vad_off",
    }


class BaseWakeWord:
    """Abstract wake word detector interface.

    Subclasses implement start/stop/poll. The voice channel uses
    ``owns_mic`` to decide whether it must manage AudioInput separately.
    """

    @property
    def owns_mic(self):
        """True if this detector acquires the I2S mic internally."""
        raise NotImplementedError

    @property
    def active(self):
        """True if the detector is currently running."""
        raise NotImplementedError

    def start(self):
        """Begin detection."""
        raise NotImplementedError

    def stop(self):
        """Stop detection and release resources."""
        raise NotImplementedError

    def poll(self, pcm=None):
        """Check for wake event. Returns 'wakeup' or None.

        For detectors that own the mic (owns_mic=True), pcm is ignored.
        For detectors that don't own the mic, caller must feed pcm chunks.
        """
        raise NotImplementedError

    @property
    def supports_capture(self):
        """True if the detector can also deliver utterance PCM + VAD events."""
        return False


class EspSrWakeWord(BaseWakeWord):
    """ESP-SR WakeNet detector. Owns the I2S mic while active.

    Also fronts the AFE capture path when the firmware has it, because the AFE
    cannot share the I2S peripheral: the same object that reports the wake word
    then hands the channel the utterance PCM (``read``) and the VAD transitions
    that delimit it (``poll_events``). Without that firmware the caller stops
    the detector and reads the mic itself, as before.
    """

    def __init__(self, sck=None, ws=None, sd=None):
        self._sck = sck if sck is not None else config.I2S_MIC_SCK
        self._ws = ws if ws is not None else config.I2S_MIC_WS
        self._sd = sd if sd is not None else config.I2S_MIC_SD
        self._running = False

    @property
    def owns_mic(self):
        return True

    @property
    def active(self):
        return self._running

    @property
    def supports_capture(self):
        """True when firmware can deliver AFE VAD events and utterance PCM."""
        return _esp_sr is not None and hasattr(_esp_sr, "capture") and hasattr(_esp_sr, "read")

    @property
    def rate(self):
        """Sample rate of the PCM the AFE delivers (16kHz mono)."""
        return config.VOICE_SAMPLE_RATE

    def start(self):
        if self._running:
            return
        kwargs = {"sck": self._sck, "ws": self._ws, "sd": self._sd}
        if self.supports_capture:
            # The AFE VAD is the endpointing signal: its trailing-silence
            # window is how long a pause commits an utterance, and its pre-roll
            # cache is what keeps the first syllable. Both are tunable from
            # here so the firmware does not have to be rebuilt to change them.
            kwargs["vad_min_noise_ms"] = config.VOICE_VAD_POST_SILENCE_MS
            kwargs["vad_delay_ms"] = config.VOICE_AFE_PREROLL_MS
        try:
            _esp_sr.init(**kwargs)
        except RuntimeError:
            # A soft reboot reruns the app but leaves the C module's statics
            # behind, so init() reports "already initialised". Tear the stale
            # instance down and retry once: without this the audio loop
            # crash-loops until somebody hard-resets the board.
            log.warn("WakeWord", "Stale ESP-SR instance, re-initialising")
            _esp_sr.deinit()
            _esp_sr.init(**kwargs)
        self._running = True
        log.info("WakeWord", "ESP-SR detector started")

    def stop(self):
        if not self._running:
            return
        _esp_sr.deinit()
        self._running = False
        log.info("WakeWord", "ESP-SR detector stopped")

    def poll_events(self):
        """Drain the event queue -> list of 'wakeup'/'vad_on'/'vad_off'.

        Everything pending is taken, not just one event: VAD transitions share
        the queue with wakeups and the firmware drops the *newest* event once
        it is full, so leaving VAD chatter behind could swallow a wake word.
        """
        if not self._running or _esp_sr is None:
            return []
        names = []
        while True:
            ev = _esp_sr.poll()
            if ev is None:
                return names
            name = _EVENT_NAMES.get(ev[0])
            if name is not None:
                names.append(name)

    def poll(self, pcm=None):
        return "wakeup" if "wakeup" in self.poll_events() else None

    def capture(self, on):
        """Start/stop AFE utterance buffering. False if firmware can't."""
        if _esp_sr is None or not self.supports_capture:
            return False
        return bool(_esp_sr.capture(on))

    def read(self, nbytes, timeout_ms=None):
        """Captured AFE PCM; b"" if nothing arrived within the timeout."""
        if _esp_sr is None or not self.supports_capture:
            return b""
        if timeout_ms is None:
            timeout_ms = config.VOICE_AFE_READ_TIMEOUT_MS
        return _esp_sr.read(nbytes, timeout_ms)

    def capture_stats(self):
        """(AFE frames seen, bytes dropped) since the last capture(True).

        Frames are counted whether or not the speech gate let them through, so
        zero frames over a whole listen window means the AFE delivered nothing
        at all - a broken capture path, not a silent room.
        """
        if _esp_sr is None or not hasattr(_esp_sr, "capture_stats"):
            return (0, 0)
        return _esp_sr.capture_stats()


class EnergyWakeWord(BaseWakeWord):
    """Energy-threshold wake detection. Does NOT own the mic.

    Caller feeds PCM chunks via poll(pcm). Returns 'wakeup' when
    average amplitude exceeds the threshold (i.e. someone starts talking).
    """

    def __init__(self, threshold=80):
        self._threshold = threshold
        self._running = False
        self._triggered = False

    @property
    def owns_mic(self):
        return False

    @property
    def active(self):
        return self._running

    def start(self):
        self._running = True
        self._triggered = False
        log.info("WakeWord", "Energy detector started")

    def stop(self):
        self._running = False
        self._triggered = False

    def poll(self, pcm=None):
        if not self._running or pcm is None:
            return None
        if self._triggered:
            return None
        avg = pcm16_avg_abs(pcm)
        if avg > self._threshold:
            self._triggered = True
            return "wakeup"
        return None

    def reset(self):
        """Re-arm after a wakeup has been consumed."""
        self._triggered = False


def create_wakeword(sck=None, ws=None, sd=None, threshold=80):
    """Factory: returns EspSrWakeWord if firmware supports it, else EnergyWakeWord."""
    if _esp_sr is not None:
        return EspSrWakeWord(sck=sck, ws=ws, sd=sd)
    return EnergyWakeWord(threshold=threshold)
