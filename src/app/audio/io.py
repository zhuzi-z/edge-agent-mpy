"""I2S audio hardware: microphone input and speaker output.

ESP32-S3 wiring (xiaozhi bread-compact-wifi compatible):
  Mic (INMP441):  SCK=5, WS=4, SD=6
  Speaker (MAX98357A): BCLK=15, LRC=16, DIN=7

The INMP441 puts 24-bit samples left-justified in 32-bit I2S slots.
MicroPython I2S RX returns the top 16 bits of each slot (a >>16 shift),
which is 4x quieter than the firmware ESP-SR path (>>14); AudioInput
compensates with I2S_MIC_GAIN so VAD thresholds and ASR levels match.
"""

from machine import I2S, Pin
import array
import app.config as config


def _gain_shift(gain):
    shift = 0
    while gain > 1:
        gain >>= 1
        shift += 1
    return shift


def _apply_gain(buf, n, shift):
    """Shift every 16-bit sample left with saturation. Returns the samples.

    ``array`` does the byte-to-int conversion in C; the Python loop then only
    shifts and clips. The byte-indexing version this replaced cost ~1600 slow
    iterations per 100ms chunk for the whole listening session.
    """
    samples = array.array("h", buf[:n] if n < len(buf) else buf)
    for i in range(len(samples)):
        s = samples[i] << shift
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        samples[i] = s
    return samples


class AudioInput:
    """I2S microphone: blocking PCM16-LE reads with software gain."""

    def __init__(self, sck=None, ws=None, sd=None, rate=None, gain=None):
        self._rate = rate or config.VOICE_SAMPLE_RATE
        self._shift = _gain_shift(config.I2S_MIC_GAIN if gain is None else gain)
        self._i2s = I2S(
            0,
            sck=Pin(sck or config.I2S_MIC_SCK),
            ws=Pin(ws or config.I2S_MIC_WS),
            sd=Pin(sd or config.I2S_MIC_SD),
            mode=I2S.RX,
            bits=16,
            format=I2S.MONO,
            rate=self._rate,
            ibuf=config.I2S_BUF_SIZE,
        )

    @property
    def rate(self):
        return self._rate

    def read(self, nbytes):
        """Read up to nbytes of PCM. Blocks until data available."""
        buf = bytearray(nbytes)
        n = self._i2s.readinto(buf)
        if n <= 0:
            return b""
        if self._shift:
            return bytes(_apply_gain(buf, n, self._shift))
        # A full read needs no copy: the next read allocates a fresh buffer,
        # so handing out this one aliases nothing.
        if n == nbytes:
            return buf
        return bytes(buf[:n])

    def deinit(self):
        self._i2s.deinit()


class AudioOutput:
    """I2S speaker: blocking PCM16-LE writes."""

    def __init__(self, sck=None, ws=None, sd=None, rate=None):
        self._rate = rate or config.VOICE_SAMPLE_RATE
        self._i2s = I2S(
            1,
            sck=Pin(sck or config.I2S_SPK_SCK),
            ws=Pin(ws or config.I2S_SPK_WS),
            sd=Pin(sd or config.I2S_SPK_SD),
            mode=I2S.TX,
            bits=16,
            format=I2S.MONO,
            rate=self._rate,
            ibuf=config.I2S_SPK_BUF_SIZE,
        )

    @property
    def rate(self):
        return self._rate

    def write(self, pcm):
        """Write PCM bytes to speaker. Blocks until buffered."""
        self._i2s.write(pcm)

    def end(self):
        """Finish an utterance. No-op for I2S (writes flush on their own)."""

    def deinit(self):
        self._i2s.deinit()
