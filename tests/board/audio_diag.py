"""On-board audio diagnostic tool for ESP32-S3.

Isolates hardware vs software issues by testing each layer independently.

Usage (from REPL or mpremote):
    import audio_diag
    audio_diag.run()              # all tests in sequence

    # Or step by step:
    audio_diag.tone_test()        # Step 1: I2S + amp + speaker hardware
    audio_diag.sweep_test()       # Step 2: frequency response check
    audio_diag.tts_test()         # Step 3: TTS synthesis (no playback)
    audio_diag.playback_test()    # Step 4: TTS -> speaker (full path)
    audio_diag.loopback_test()    # Step 5: mic -> speaker loopback

Deploy:
    make diag                     # copies file + runs all tests
    # or manually:
    mpremote cp tests/board/audio_diag.py :audio_diag.py
"""

import math
import struct
import time

# I2S pins (xiaozhi bread-compact-wifi compatible)
SPK_SCK = 15
SPK_WS = 16
SPK_SD = 7
MIC_SCK = 5
MIC_WS = 4
MIC_SD = 6
RATE = 16000
BUF_SIZE = 8000
AMPLITUDE = 16000  # ~50% of full scale


def _pcm_stats(pcm):
    """Return (n_samples, avg_abs, peak) for PCM16-LE bytes."""
    n = len(pcm) // 2
    if n == 0:
        return 0, 0, 0
    total = 0
    peak = 0
    for i in range(0, len(pcm) - 1, 2):
        s = pcm[i] | (pcm[i + 1] << 8)
        if s & 0x8000:
            s -= 0x10000
        a = s if s >= 0 else -s
        total += a
        if a > peak:
            peak = a
    return n, total // n, peak


def _make_sine_chunk(freq, rate=RATE, amplitude=AMPLITUDE, chunk_ms=100):
    """Generate one chunk of sine wave PCM16-LE."""
    n_samples = rate * chunk_ms // 1000
    buf = bytearray(n_samples * 2)
    for i in range(n_samples):
        val = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        struct.pack_into("<h", buf, i * 2, val)
    return bytes(buf)


def _open_speaker(rate=RATE):
    from machine import I2S, Pin

    return I2S(
        1,
        sck=Pin(SPK_SCK),
        ws=Pin(SPK_WS),
        sd=Pin(SPK_SD),
        mode=I2S.TX,
        bits=16,
        format=I2S.MONO,
        rate=rate,
        ibuf=BUF_SIZE,
    )


def _open_mic(rate=RATE):
    from machine import I2S, Pin

    return I2S(
        0,
        sck=Pin(MIC_SCK),
        ws=Pin(MIC_WS),
        sd=Pin(MIC_SD),
        mode=I2S.RX,
        bits=16,
        format=I2S.MONO,
        rate=rate,
        ibuf=BUF_SIZE,
    )


# ---------------------------------------------------------------------------
# Step 1: Tone test (pure hardware check)
# ---------------------------------------------------------------------------


def tone_test(freq=1000, duration_ms=2000, rate=RATE):
    """Play a sine tone. If audible, I2S wiring + amp + speaker are OK."""
    print("[DIAG] Tone test: {}Hz for {}ms at {}Hz sample rate".format(freq, duration_ms, rate))
    i2s = _open_speaker(rate)
    chunk = _make_sine_chunk(freq, rate, chunk_ms=100)
    total_bytes = rate * duration_ms // 1000 * 2
    written = 0
    try:
        while written < total_bytes:
            n = i2s.write(chunk)
            written += n
    finally:
        i2s.deinit()
    print("[DIAG] Tone done ({} bytes written)".format(written))
    print("[DIAG] => Can you hear a {}Hz beep? YES=hardware OK, NO=hardware issue".format(freq))
    return written > 0


# ---------------------------------------------------------------------------
# Step 2: Frequency sweep
# ---------------------------------------------------------------------------


def sweep_test(freqs=(200, 500, 1000, 2000, 4000, 8000), step_ms=500, rate=RATE):
    """Play a sequence of tones to check frequency response."""
    print("[DIAG] Sweep test: {} freqs, {}ms each".format(len(freqs), step_ms))
    i2s = _open_speaker(rate)
    try:
        for freq in freqs:
            print("[DIAG]   {}Hz ...".format(freq))
            chunk = _make_sine_chunk(freq, rate, chunk_ms=100)
            total = rate * step_ms // 1000 * 2
            written = 0
            while written < total:
                written += i2s.write(chunk)
            time.sleep_ms(100)
    finally:
        i2s.deinit()
    print("[DIAG] Sweep done. All freqs audible = speaker bandwidth OK")


# ---------------------------------------------------------------------------
# Step 3: TTS synthesis test (no playback)
# ---------------------------------------------------------------------------


def _load_agent_cfg():
    import json

    try:
        with open("/config/agent.json", "r") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print("[DIAG] Cannot load /config/agent.json: {}".format(e))
        return None


def tts_test(text="Hello, this is a diagnostic test."):
    """Call TTS and report PCM stats without playing. Checks network + TTS service."""
    print("[DIAG] TTS synthesis test (no playback)")
    cfg = _load_agent_cfg()
    if not cfg:
        print("[DIAG] SKIP: no config")
        return None

    from app.channels.voice import do_tts_stream, tts_rate

    t0 = time.ticks_ms()
    try:
        chunks = []
        do_tts_stream(cfg, text, chunks.append)
        pcm = b"".join(chunks)
        rate = tts_rate(cfg)
    except Exception as e:
        print("[DIAG] TTS FAILED: {}".format(e))
        return None
    elapsed = time.ticks_diff(time.ticks_ms(), t0)

    n_samples, avg, peak = _pcm_stats(pcm)
    duration_ms = n_samples * 1000 // rate if rate else 0
    print("[DIAG] TTS result:")
    print("[DIAG]   bytes={}, sample_rate={}, duration={}ms".format(len(pcm), rate, duration_ms))
    print("[DIAG]   avg_abs={}, peak={}, elapsed={}ms".format(avg, peak, elapsed))
    if avg < 10:
        print("[DIAG]   WARNING: PCM is nearly silent! TTS returned empty/quiet audio")
    else:
        print("[DIAG]   PCM looks good (non-silent)")
    return pcm, rate


# ---------------------------------------------------------------------------
# Step 4: TTS playback test (full software path)
# ---------------------------------------------------------------------------


def playback_test(text="Hello, this is a playback diagnostic test.", rate_override=None):
    """TTS -> speaker. Tests the full software output path."""
    print("[DIAG] Playback test: TTS -> speaker")
    result = tts_test(text)
    if result is None:
        print("[DIAG] SKIP: TTS failed, cannot test playback")
        return False
    pcm, tts_rate = result
    play_rate = rate_override or tts_rate
    if play_rate != RATE:
        print(
            "[DIAG] NOTE: TTS rate={} vs I2S default={}. Playing at TTS rate.".format(
                play_rate, RATE
            )
        )
    print("[DIAG] Playing {} bytes at {}Hz ...".format(len(pcm), play_rate))
    i2s = _open_speaker(play_rate)
    try:
        off = 0
        chunk_size = 4096
        while off < len(pcm):
            end = min(off + chunk_size, len(pcm))
            i2s.write(pcm[off:end])
            off = end
    finally:
        i2s.deinit()
    print("[DIAG] Playback done. Can you hear speech?")
    return True


# ---------------------------------------------------------------------------
# Step 5: Mic -> Speaker loopback
# ---------------------------------------------------------------------------


def loopback_test(duration_ms=3000, rate=RATE):
    """Record from mic and play back through speaker in real-time."""
    print("[DIAG] Loopback test: speak now ({}ms)".format(duration_ms))
    mic = _open_mic(rate)
    spk = _open_speaker(rate)
    chunk_bytes = rate * 2 * 100 // 1000  # 100ms
    total_bytes = rate * 2 * duration_ms // 1000
    read_total = 0
    try:
        while read_total < total_bytes:
            buf = bytearray(chunk_bytes)
            n = mic.readinto(buf)
            if n and n > 0:
                spk.write(bytes(buf[:n]))
                read_total += n
    finally:
        mic.deinit()
        spk.deinit()
    print("[DIAG] Loopback done ({} bytes). Did you hear your voice?".format(read_total))
    return read_total > 0


# ---------------------------------------------------------------------------
# Full diagnostic sequence
# ---------------------------------------------------------------------------


def run():
    """Run all diagnostic steps with pauses between them."""
    print("=" * 50)
    print("[DIAG] Audio diagnostic starting")
    print(
        "[DIAG] Pins: SPK(BCLK={}, LRC={}, DIN={}) MIC(SCK={}, WS={}, SD={})".format(
            SPK_SCK, SPK_WS, SPK_SD, MIC_SCK, MIC_WS, MIC_SD
        )
    )
    print("=" * 50)

    print("\n--- Step 1/5: Hardware tone ---")
    tone_test()
    time.sleep_ms(1000)

    print("\n--- Step 2/5: Frequency sweep ---")
    sweep_test()
    time.sleep_ms(1000)

    print("\n--- Step 3/5: TTS synthesis (no audio) ---")
    tts_test()
    time.sleep_ms(1000)

    print("\n--- Step 4/5: TTS playback ---")
    playback_test()
    time.sleep_ms(1000)

    print("\n--- Step 5/5: Mic loopback ---")
    loopback_test()

    print("\n" + "=" * 50)
    print("[DIAG] Diagnostic complete.")
    print("[DIAG] Interpretation:")
    print("[DIAG]   Step 1 silent  => hardware issue (wiring/amp/speaker)")
    print("[DIAG]   Step 1 OK, Step 3 fails => network/TTS config issue")
    print("[DIAG]   Step 3 OK, Step 4 silent => I2S rate mismatch or SW bug")
    print("[DIAG]   Step 5 silent => mic hardware issue")
    print("=" * 50)
