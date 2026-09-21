"""PCM analysis, VAD, WAV codec, base64 helpers. Hardware-independent."""

import array
import math
import struct
import binascii


def pcm16_avg_abs(buf):
    """Mean absolute amplitude of LE signed-16-bit PCM.

    Converts the raw bytes to signed shorts with ``array`` so the C
    implementation performs the byte-to-int unpacking; the Python side
    only does the cheap absolute-value pass.
    """
    n = len(buf)
    if n < 2:
        return 0
    if n & 1:
        buf = buf[:-1]
    samples = array.array("h", buf)
    total = 0
    for sample in samples:
        total += abs(sample)
    return total // len(samples)


def pcm16_scale(buf, level):
    """Scale LE signed-16-bit PCM to level percent (0-100). Returns bytes."""
    if level >= 100:
        return buf
    n = len(buf)
    if level <= 0:
        return b"\x00" * n
    trailer = b""
    if n & 1:
        buf = buf[:-1]
        trailer = b"\x00"
    samples = array.array("h", buf)
    for i in range(len(samples)):
        samples[i] = samples[i] * level // 100
    out = bytes(samples)
    if trailer:
        out += trailer
    return out


def make_tone(freq=880, ms=600, rate=16000, amplitude=90):
    """Generate a PCM16-LE sine beep (volume preview). amplitude: 0-100."""
    n = rate * ms // 1000
    fade = rate * 5 // 1000
    amp = 32767 * amplitude // 100
    out = bytearray(n * 2)
    step = 2.0 * math.pi * freq / rate
    for i in range(n):
        env = amp
        if i < fade:
            env = amp * i // fade
        elif n - i <= fade:
            env = amp * (n - i) // fade
        sample = int(env * math.sin(step * i))
        out[2 * i] = sample & 0xFF
        out[2 * i + 1] = (sample >> 8) & 0xFF
    return bytes(out)


class Vad:
    """Energy VAD with adaptive noise-floor estimation and hysteresis.

    Real microphones have very different, bursty ambient noise floors (a
    laptop mic can idle at avg-abs ~500 with bursts to ~900, far above any
    fixed threshold), so the noise floor is tracked continuously as the
    upper quartile of a sliding window of non-speech chunks. Onset/release
    thresholds scale with it (with hysteresis), and two consecutive loud
    chunks are required to extend a segment so isolated noise bursts don't
    hold it open.

    Feed PCM16-LE chunks with now_ms timestamps. Returns "speech_start"
    on silence->sound, "commit" on segment end, else None.
    """

    def __init__(
        self,
        threshold=80,
        min_speech_ms=400,
        post_silence_ms=1500,
        calibrate_chunks=3,
        window=30,
        onset_factor=1.8,
        release_factor=1.6,
        noise_ceiling=1200,
    ):
        self._floor = threshold
        self._min_speech_ms = min_speech_ms
        self._post_silence_ms = post_silence_ms
        self._calibrate_chunks = calibrate_chunks
        self._window = window
        self._onset_factor = onset_factor
        self._release_factor = release_factor
        self._noise_ceiling = noise_ceiling
        self.reset()

    def reset(self):
        """Full reset: forget the noise estimate too (cold start)."""
        self._ambient = []
        self._last_speech_ms = 0
        self.begin_listen()

    def begin_listen(self):
        """Prepare for a new utterance, keeping the learned noise floor.

        With an established floor there is no calibration dead zone:
        detection starts on the very first chunk (important right after
        the wake word, when the user speaks immediately).
        """
        self._had_voice = False
        self._speech_start_ms = 0
        self._last_sound_ms = 0
        self._consec_sound = 0
        have = len(self._ambient)
        if have >= self._calibrate_chunks:
            self._cal_left = 0
        else:
            self._cal_left = self._calibrate_chunks - have

    @property
    def speaking(self):
        """True while a speech segment is open (onset fired, no commit yet)."""
        return self._had_voice

    @property
    def calibrated(self):
        """True once enough ambient was collected to start detecting."""
        return self._cal_left <= 0

    @property
    def last_speech_ms(self):
        """Length of the most recently committed speech segment.

        Measured on the sample clock, from onset to the last chunk that still
        carried speech - so it excludes the trailing silence the detector waits
        through before committing. That gap is what the latency log reports
        separately, and it is a fixed cost however short the utterance is.
        """
        return self._last_speech_ms

    def calibrate(self, chunk):
        """Feed pre-speech audio (e.g. warmup) into the noise estimate."""
        avg = pcm16_avg_abs(chunk)
        if avg > 300 and (not self._ambient or avg > 2 * self._noise()):
            # Loud chunk: the user started talking during warmup. Stop
            # ingesting so speech does not contaminate the floor; start
            # detecting immediately.
            self._cal_left = 0
            return
        self._add_ambient(avg)
        if len(self._ambient) >= self._calibrate_chunks:
            self._cal_left = 0

    @property
    def noise_floor(self):
        """Current noise-floor estimate; None until calibration completes."""
        if self._cal_left > 0 or not self._ambient:
            return None
        return self._noise()

    def _noise(self):
        # Upper quartile: robust to quiet gaps (low outliers) and to a
        # couple of loud wake-word-tail chunks (high outliers).
        vals = sorted(self._ambient)
        noise = vals[len(vals) * 3 // 4]
        return noise if noise < self._noise_ceiling else self._noise_ceiling

    def _thresholds(self):
        """(onset, release) thresholds from one noise-floor pass per chunk."""
        if not self._ambient:
            return self._floor, self._floor
        noise = self._noise()
        onset = int(noise * self._onset_factor)
        release = int(noise * self._release_factor)
        return (
            onset if onset > self._floor else self._floor,
            release if release > self._floor else self._floor,
        )

    def _add_ambient(self, avg):
        self._ambient.append(avg)
        if len(self._ambient) > self._window:
            self._ambient.pop(0)

    def feed(self, chunk, now_ms):
        """Process one chunk. Returns event string or None."""
        avg = pcm16_avg_abs(chunk)

        if self._cal_left > 0:
            if len(self._ambient) >= 6 and avg > 2 * self._noise() and avg > 300:
                # Clear speech onset during calibration: finalize the
                # noise estimate with what we have and start detecting.
                self._cal_left = 0
            else:
                self._cal_left -= 1
                self._add_ambient(avg)
                return None

        onset, release = self._thresholds()

        if not self._had_voice:
            if avg < onset:
                self._add_ambient(avg)
                return None
            self._had_voice = True
            self._consec_sound = 1
            self._speech_start_ms = now_ms
            self._last_sound_ms = now_ms
            return "speech_start"

        if avg > release:
            self._consec_sound += 1
            if self._consec_sound >= 2:
                self._last_sound_ms = now_ms
            return None

        self._consec_sound = 0
        self._add_ambient(avg)
        if now_ms - self._last_sound_ms >= self._post_silence_ms:
            duration_ms = self._last_sound_ms - self._speech_start_ms
            self._last_speech_ms = duration_ms
            self._had_voice = False
            self._speech_start_ms = 0
            self._last_sound_ms = 0
            if duration_ms >= self._min_speech_ms:
                return "commit"
        return None


def parse_wav(data):
    """Parse WAV -> (sample_rate, channels, pcm_bytes). PCM only."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    fmt_rate = 0
    fmt_channels = 0
    fmt_audio_format = 0
    pcm_data = b""
    pos = 12
    n = len(data)
    while pos + 8 <= n:
        chunk_id = data[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16 or body + 16 > n:
                raise ValueError("fmt chunk too small")
            fmt_audio_format = struct.unpack_from("<H", data, body)[0]
            fmt_channels = struct.unpack_from("<H", data, body + 2)[0]
            fmt_rate = struct.unpack_from("<I", data, body + 4)[0]
        elif chunk_id == b"data":
            end = body + chunk_size
            if end > n:
                end = n
            pcm_data = data[body:end]
            break
        pos = body + chunk_size
        if chunk_size % 2 == 1:
            pos += 1
    if fmt_audio_format != 1:
        raise ValueError("not PCM (audio_format={})".format(fmt_audio_format))
    if not pcm_data:
        raise ValueError("no data chunk found")
    return fmt_rate, fmt_channels, pcm_data


def make_wav(pcm, rate=16000, channels=1):
    """Build WAV (44-byte header + PCM16-LE data)."""
    bits_per_sample = 16
    byte_rate = rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm


def b64encode(data):
    return binascii.b2a_base64(data).decode("ascii").strip()


def b64decode(s):
    return binascii.a2b_base64(s)
