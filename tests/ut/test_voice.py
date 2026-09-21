"""Voice logic tests: audio, VAD, WAV, channel state machine."""

import compat  # noqa: F401

import time
import unittest

import app.config as config

from helpers import s16_chunk
from app.audio import (
    pcm16_avg_abs,
    pcm16_scale,
    make_tone,
    Vad,
    parse_wav,
    make_wav,
    b64encode,
    b64decode,
)
from app.channels.voice import VoiceChannel, STATE_IDLE, STATE_LISTENING


LOUD = s16_chunk([10000, -10000] * 256)
AMBIENT = s16_chunk([100, -100] * 256)
SILENCE = bytes(1024)


class TestAudio(unittest.TestCase):
    def test_pcm16_helpers(self):
        self.assertEqual(pcm16_avg_abs(SILENCE), 0)
        self.assertEqual(pcm16_avg_abs(s16_chunk([1000, -1000, 1000, -1000])), 1000)
        self.assertEqual(pcm16_avg_abs(b""), 0)
        pcm = s16_chunk([4000, -4000, 32767, -32768])
        self.assertEqual(pcm16_scale(pcm, 50), s16_chunk([2000, -2000, 16383, -16384]))
        self.assertEqual(pcm16_scale(pcm, 100), pcm)
        self.assertEqual(pcm16_scale(pcm, 0), bytes(8))
        tone = make_tone(freq=880, ms=100, rate=16000, amplitude=90)
        self.assertEqual(len(tone), 3200)
        self.assertEqual(tone[:2], b"\x00\x00")  # sin(0) == 0
        self.assertTrue(17000 < pcm16_avg_abs(tone) < 20000)  # ~0.637 * 29490
        self.assertEqual(make_tone(ms=100, amplitude=0), bytes(3200))

    def test_vad_adaptive_lifecycle(self):
        # Ambient noise above the absolute floor (80) must calibrate as
        # noise, not trigger speech; loud speech then commits on silence.
        v = Vad(min_speech_ms=200, post_silence_ms=500)
        for i in range(10):
            self.assertIsNone(v.feed(AMBIENT, i * 100))
        self.assertEqual(v.noise_floor, 100)
        self.assertIsNone(v.feed(AMBIENT, 500))
        self.assertEqual(v.feed(LOUD, 600), "speech_start")
        self.assertIsNone(v.feed(LOUD, 900))
        self.assertIsNone(v.feed(SILENCE, 1000))
        self.assertEqual(v.feed(SILENCE, 1500), "commit")
        # The committed segment excludes the trailing silence the detector waits
        # through, which the latency log reports separately as the gap.
        self.assertEqual(v.last_speech_ms, 300)

    def test_vad_calibration_robustness(self):
        # A loud wake-word tail chunk during calibration must not raise
        # the noise floor (robust to the outlier).
        v = Vad(min_speech_ms=200, post_silence_ms=500)
        self.assertIsNone(v.feed(LOUD, 0))
        for i in range(9):
            self.assertIsNone(v.feed(AMBIENT, 100 + i * 100))
        self.assertEqual(v.noise_floor, 100)
        self.assertEqual(v.feed(LOUD, 600), "speech_start")
        # An established floor survives into the next listen: detection
        # starts on the first chunk (no post-wake dead zone).
        v.begin_listen()
        self.assertEqual(v.noise_floor, 100)
        self.assertEqual(v.feed(LOUD, 0), "speech_start")
        # Speech during warmup must not contaminate the floor.
        v2 = Vad()
        v2.calibrate(AMBIENT)
        v2.calibrate(AMBIENT)
        v2.calibrate(LOUD)
        self.assertEqual(v2.noise_floor, 100)
        # Full reset forgets the floor (cold start).
        v.reset()
        self.assertIsNone(v.noise_floor)
        # The channel narrows the class default: 1500ms of trailing silence
        # delayed every commit and padded the PCM sent to ASR.
        self.assertEqual(
            VoiceChannel(None, lambda: {})._vad._post_silence_ms,
            config.VOICE_VAD_POST_SILENCE_MS,
        )

    def test_wav_and_b64_roundtrip(self):
        pcm = s16_chunk([100, -200, 300])
        wav = make_wav(pcm, rate=16000, channels=1)
        rate, ch, data = parse_wav(wav)
        self.assertEqual((rate, ch), (16000, 1))
        self.assertEqual(data, pcm)
        self.assertEqual(b64decode(b64encode(b"\x01\x02\x03")), b"\x01\x02\x03")

    def test_prepare_for_voice(self):
        from app.channels.voice import prepare_for_voice

        reply = "🌤️ **Shenzhen Weather** 🌤️\n\n| Item | Info |\n|------|------|\n| Temp | 25°C |\n"
        out = prepare_for_voice(reply, max_chars=300)
        self.assertNotIn("|", out)
        self.assertNotIn("*", out)
        self.assertIn("Shenzhen Weather", out)
        self.assertIn("Temp 25°C", out)
        self.assertEqual(prepare_for_voice("x" * 500, max_chars=300), "x" * 300)


class TestMicGain(unittest.TestCase):
    def test_gain_and_clipping(self):
        from app.audio.io import AudioInput

        ai = AudioInput(gain=4)

        class FakeI2S:
            def readinto(self, buf):
                src = s16_chunk([100, -100, 20000, -20000])
                buf[: len(src)] = src
                return len(src)

        ai._i2s = FakeI2S()
        pcm = ai.read(8)
        got = []
        for i in range(0, len(pcm), 2):
            s = pcm[i] | (pcm[i + 1] << 8)
            if s & 0x8000:
                s -= 0x10000
            got.append(s)
        self.assertEqual(got, [400, -400, 32767, -32768])


class FakeWakeWord:
    """Wake detector stub returning scripted events."""

    owns_mic = True

    def __init__(self, events):
        self.events = list(events)
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def poll(self, pcm=None):
        return self.events.pop(0) if self.events else None


class TestPlaybackTailMute(unittest.TestCase):
    def test_wake_gated_during_playback_tail(self):
        """After TTS, wake events are dropped until the tail expires."""
        ch = VoiceChannel(None, lambda: {})
        ch._wakeword = FakeWakeWord(["wakeup", "wakeup"])
        ch._mute_until_ms = time.ticks_add(time.ticks_ms(), 500)
        ch._loop_idle(320)
        self.assertEqual(ch._state, STATE_IDLE)
        ch._mute_until_ms = time.ticks_add(time.ticks_ms(), -1)
        ch._loop_idle(320)
        self.assertEqual(ch._state, STATE_LISTENING)


class FakeAudioOut:
    rate = 16000

    def __init__(self):
        self.writes = 0
        self.ended = False
        self.chunks = []

    def write(self, pcm):
        self.writes += 1
        self.chunks.append(pcm)

    def end(self):
        self.ended = True


class FakeBus:
    def dispatch_sync(self, message):
        from app.channel import Reply

        return Reply(content="reply text")

    async def dispatch(self, message):
        return self.dispatch_sync(message)


class FakeAudioIn:
    rate = 16000

    def read(self, nbytes):
        return b""

    def deinit(self):
        pass


class TestFollowUp(unittest.TestCase):
    def setUp(self):
        import app.channels.voice as vc

        self.vc = vc
        self._orig_asr = vc.do_asr
        self._orig_tts = vc.do_tts_stream
        vc.do_asr = lambda cfg, wav: "hello"

        self.tts_chunk = b"xx"

        def fake_tts(cfg, text, sink=None, ws=None):
            for _ in range(5):
                sink(self.tts_chunk)
            return 10

        vc.do_tts_stream = fake_tts
        self.cfg = {"base_url": "http://x/v1", "api_key": "k", "tts_ws_sample_rate": 16000}

    def tearDown(self):
        self.vc.do_asr = self._orig_asr
        self.vc.do_tts_stream = self._orig_tts

    def _channel(self):
        ch = VoiceChannel(FakeBus(), lambda: self.cfg)
        ch._wakeword = FakeWakeWord([])
        ch._audio_out = FakeAudioOut()
        ch._audio_in = FakeAudioIn()
        return ch

    def test_followup_window_lifecycle(self):
        ch = self._channel()
        self.assertTrue(ch._process_speech(b"\x00" * 640, 16000))
        self.assertGreater(ch._mute_until_ms, 0)
        ch._enter_followup()
        self.assertEqual(ch._state, STATE_LISTENING)
        self.assertGreater(ch._followup_until_ms, 0)
        # Window open: stays listening; expired silently: back to wake stage.
        ch._loop_listening(320)  # window open: stays listening
        self.assertEqual(ch._state, STATE_LISTENING)
        ch._followup_until_ms = time.ticks_add(time.ticks_ms(), -1)
        ch._loop_listening(320)  # expired silently: back to wake stage
        self.assertEqual(ch._state, STATE_IDLE)

    def test_exit_voice_metadata_skips_followup(self):
        from app.channel import Reply

        spoken = []
        fake_tts = self.vc.do_tts_stream

        def cap_tts(cfg, text, sink=None, ws=None):
            spoken.append(text)
            return fake_tts(cfg, text, sink=sink, ws=ws)

        self.vc.do_tts_stream = cap_tts

        class BusWithMeta:
            def __init__(self, reply):
                self._reply = reply

            def dispatch_sync(self, message):
                return self._reply

            async def dispatch(self, message):
                return self._reply

        # Exit with ack: the skill's farewell is spoken, returns False
        # (wake stage, no follow-up).
        ch = self._channel()
        meta = {"exit_voice": True, "exit_ack": "Sure, talk to you later"}
        ch._bus = BusWithMeta(Reply(content="OK", metadata=meta))
        self.assertFalse(ch._process_speech(b"\x00" * 640, 16000))
        self.assertEqual(spoken, ["Sure, talk to you later"])
        self.assertGreater(ch._mute_until_ms, 0)
        # No ack: falls back to the reply content.
        spoken.clear()
        ch2 = self._channel()
        ch2._bus = BusWithMeta(Reply(content="That is it for now", metadata={"exit_voice": True}))
        self.assertFalse(ch2._process_speech(b"\x00" * 640, 16000))
        self.assertEqual(spoken, ["That is it for now"])
        # No ack, no content: fixed local ack.
        spoken.clear()
        ch3 = self._channel()
        ch3._bus = BusWithMeta(Reply(content="", metadata={"exit_voice": True}))
        self.assertFalse(ch3._process_speech(b"\x00" * 640, 16000))
        self.assertEqual(spoken, [config.VOICE_EXIT_ACK])

    def test_volume_scaling(self):
        self.cfg["volume"] = 50
        self.tts_chunk = s16_chunk([4000, -4000])
        ch = self._channel()
        out = ch._audio_out
        self.assertTrue(ch._process_speech(b"\x00" * 640, 16000))
        self.assertEqual(b"".join(out.chunks), s16_chunk([2000, -2000]) * 5)
        # Test tone obeys the same volume and is refused mid-utterance.
        ch2 = self._channel()
        out2 = ch2._audio_out
        self.assertTrue(ch2.play_test_tone())
        self.assertEqual(len(out2.chunks), 1)
        full_avg = pcm16_avg_abs(make_tone(rate=16000))
        ratio = pcm16_avg_abs(out2.chunks[0]) * 100 // full_avg
        self.assertTrue(45 <= ratio <= 55)
        ch2._busy = True
        self.assertFalse(ch2.play_test_tone())

    def test_tts_prewarm_is_passed_and_closed(self):
        """The socket opened during the LLM call reaches TTS; unused ones close."""
        from app.channel import Reply

        seen = []
        closed = []
        orig_tts = self.vc.do_tts_stream
        orig_prewarm = VoiceChannel._prewarm_tts

        class FakeWs:
            def close(self):
                closed.append(True)

        def cap_tts(cfg, text, sink=None, ws=None):
            seen.append(ws)
            return orig_tts(cfg, text, sink=sink, ws=ws)

        class FailingBus:
            def dispatch_sync(self, message):
                return Reply(error="boom")

            async def dispatch(self, message):
                return self.dispatch_sync(message)

        ws = FakeWs()
        self.vc.do_tts_stream = cap_tts
        VoiceChannel._prewarm_tts = lambda self, cfg: ws
        try:
            ch = self._channel()
            self.assertTrue(ch._process_speech(b"\x00" * 640, 16000))
            self.assertEqual(seen, [ws])
            self.assertEqual(closed, [])
            # A failed reply never reaches _speak, so the socket is dropped.
            ch2 = self._channel()
            ch2._bus = FailingBus()
            self.assertFalse(ch2._process_speech(b"\x00" * 640, 16000))
            self.assertEqual(closed, [True])
        finally:
            self.vc.do_tts_stream = orig_tts
            VoiceChannel._prewarm_tts = orig_prewarm

    def test_listening_detects_immediately_when_calibrated(self):
        class LoudIn:
            rate = 16000

            def read(self, nbytes):
                return LOUD[:nbytes]

            def deinit(self):
                pass

        ch = self._channel()
        for _ in range(6):
            ch._vad.calibrate(AMBIENT)
        ch._audio_in = LoudIn()
        ch._enter_listening()
        ch._loop_listening(320)
        self.assertTrue(ch._vad.speaking)
        self.assertEqual(len(ch._pcm_buf), 320)
        # Cold channel: first loud chunk finalizes calibration, no false trigger yet.
        ch2 = self._channel()
        ch2._audio_in = LoudIn()
        ch2._enter_listening()
        ch2._loop_listening(320)
        self.assertFalse(ch2._vad.speaking)


class TestStreamingAsr(unittest.TestCase):
    """The ASR session opens at speech_start, is fed live, and has a fallback."""

    def setUp(self):
        import app.channels.voice as vc

        self.vc = vc
        self._orig_asr = vc.do_asr
        vc.do_asr = lambda cfg, wav: "buffered"
        self.cfg = {
            "base_url": "http://x/v1",
            "api_key": "k",
            "asr_ws_host": "wss://example.test/api-ws/v1/inference",
        }

    def tearDown(self):
        self.vc.do_asr = self._orig_asr

    def test_open_feed_finish_and_fallback(self):
        from app.providers import ASRWSError

        class FakeStream:
            def __init__(self, text):
                self.text = text
                self.fed = []
                self.closed = False

            def feed(self, pcm):
                self.fed.append(pcm)

            def finish(self):
                try:
                    if self.text is None:
                        raise ASRWSError("transport died")
                    return self.text
                finally:
                    self.close()

            def close(self):
                self.closed = True

        class FakeProvider:
            def __init__(self, stream):
                self._stream = stream

            def stream(self, cfg, timeout=None):
                return self._stream

        class LoudIn:
            rate = 16000

            def read(self, nbytes):
                return LOUD[:nbytes]

            def deinit(self):
                pass

        stream = FakeStream("streamed")
        orig_get_asr = self.vc.get_asr
        self.vc.get_asr = lambda cfg: FakeProvider(stream)
        try:
            ch = VoiceChannel(FakeBus(), lambda: self.cfg)
            ch._wakeword = FakeWakeWord([])
            ch._audio_out = FakeAudioOut()
            ch._audio_in = LoudIn()
            for _ in range(6):
                ch._vad.calibrate(AMBIENT)
            ch._enter_listening()
            ch._loop_listening(320)
            # speech_start opened the session and pushed the triggering chunk.
            self.assertIs(ch._asr_stream, stream)
            self.assertEqual(stream.fed, [LOUD[:320]])
            # finish() supplies the transcript with no second round trip.
            self.assertEqual(ch._finish_asr(self.cfg, b""), "streamed")
            self.assertIsNone(ch._asr_stream)
            # A dead session falls back to the buffered PCM.
            dead = FakeStream(None)
            ch._asr_stream = dead
            self.assertEqual(ch._finish_asr(self.cfg, b""), "buffered")
            self.assertTrue(dead.closed)
            # No endpoint configured: nothing is pre-opened.
            ch._voice_cfg_fn = lambda: {"base_url": "http://x/v1", "api_key": "k"}
            ch._open_asr_stream()
            self.assertIsNone(ch._asr_stream)
            # An unused session is dropped on the way back to the wake stage.
            spare = FakeStream("x")
            ch._asr_stream = spare
            ch._enter_idle()
            self.assertTrue(spare.closed)
            self.assertIsNone(ch._asr_stream)
        finally:
            self.vc.get_asr = orig_get_asr


class TestAudioThreadRecovery(unittest.TestCase):
    """A crashed audio thread is restarted by supervise(); a deliberate stop
    is not, and restarts are rate-limited."""

    def test_supervise_restarts_only_crashes(self):
        import app.channels.voice as vc

        started = []

        class FakeThread:
            @staticmethod
            def stack_size(n):
                pass

            @staticmethod
            def start_new_thread(fn, args):
                started.append(fn)

        ch = VoiceChannel(FakeBus(), lambda: {"voice_enabled": True})
        ch._wakeword = FakeWakeWord([])
        ch._audio_out = FakeAudioOut()
        ch._audio_in = FakeAudioIn()
        ch._running = True
        orig_thread = vc._thread
        vc._thread = FakeThread
        try:
            ch._start_audio()
            self.assertEqual(len(started), 1)

            def crash():
                # What _audio_loop's except/finally leave behind.
                ch._thread_alive = False
                ch._thread_done = True
                ch._crashed = True

            ch._state = STATE_LISTENING
            crash()
            self.assertTrue(ch.supervise())
            self.assertEqual(len(started), 2)
            # Back at the wake stage rather than resuming a stale utterance.
            self.assertEqual(ch._state, STATE_IDLE)
            self.assertTrue(ch._thread_alive and not ch._crashed)
            # A crash inside the cooldown window waits it out.
            crash()
            self.assertFalse(ch.supervise())
            self.assertEqual(len(started), 2)
            ch._restart_at_ms = 0
            self.assertTrue(ch.supervise())
            self.assertEqual(len(started), 3)
            # Stopping on purpose clears the crash flag, so nothing restarts.
            ch._stop_audio()
            self.assertFalse(ch._crashed)
            self.assertFalse(ch.supervise())
            self.assertEqual(len(started), 3)
        finally:
            vc._thread = orig_thread


if __name__ == "__main__":
    unittest.main(globals())
