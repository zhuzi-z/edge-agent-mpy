"""Wake word detector tests: EspSr, Energy, factory, voice channel states."""

import compat  # noqa: F401

import time
import unittest
import esp_sr
import app.config as config
from app.audio.wakeword import (
    EspSrWakeWord,
    EnergyWakeWord,
    create_wakeword,
)
from app.audio import Vad
from helpers import s16_chunk


LOUD = s16_chunk([10000, -10000] * 256)
SILENCE = bytes(1024)


class TestEspSrWakeWord(unittest.TestCase):
    def setUp(self):
        esp_sr._reset()

    def test_lifecycle_and_poll(self):
        det = EspSrWakeWord(sck=41, ws=42, sd=2)
        self.assertTrue(det.owns_mic)
        self.assertFalse(det.active)
        # stopped → poll returns None
        esp_sr._inject(esp_sr.EVENT_WAKEUP, 1)
        self.assertIsNone(det.poll())
        # start
        det.start()
        self.assertTrue(det.active)
        self.assertTrue(esp_sr.running())
        self.assertEqual(esp_sr._init_kwargs["sck"], 41)
        # drains the stale event injected before start
        esp_sr._events.clear()
        # start() hands the AFE VAD its two timing knobs
        self.assertEqual(esp_sr._init_kwargs["vad_min_noise_ms"], config.VOICE_VAD_POST_SILENCE_MS)
        self.assertEqual(esp_sr._init_kwargs["vad_delay_ms"], config.VOICE_AFE_PREROLL_MS)
        # One call drains the whole queue and ignores VAD transitions: they
        # share the queue with wakeups, which a full queue would drop.
        esp_sr._inject(esp_sr.EVENT_VAD_ON, 0)
        esp_sr._inject(esp_sr.EVENT_VAD_OFF, 0)
        esp_sr._inject(esp_sr.EVENT_WAKEUP, 1)
        self.assertEqual(det.poll(), "wakeup")
        self.assertIsNone(det.poll())
        # stop
        det.stop()
        self.assertFalse(det.active)
        self.assertFalse(esp_sr.running())

    def test_start_recovers_stale_instance(self):
        # A soft reboot reruns the app while the C module stays initialised, so
        # start() has to tear the stale instance down instead of crash-looping.
        esp_sr.init(sck=1, ws=2, sd=3)
        det = EspSrWakeWord(sck=41, ws=42, sd=2)
        det.start()
        self.assertTrue(det.active)
        self.assertTrue(esp_sr.running())
        self.assertEqual(esp_sr._init_kwargs["sck"], 41)
        self.assertEqual(esp_sr._init_kwargs["vad_delay_ms"], config.VOICE_AFE_PREROLL_MS)
        det.stop()
        self.assertFalse(esp_sr.running())


class TestEspSrCapture(unittest.TestCase):
    """AFE utterance capture: VAD events plus the PCM they delimit."""

    def setUp(self):
        esp_sr._reset()

    def test_capture_and_read(self):
        det = EspSrWakeWord()
        self.assertTrue(det.supports_capture)
        self.assertEqual(det.rate, config.VOICE_SAMPLE_RATE)
        det.start()
        self.assertTrue(det.capture(True))
        # Silence is counted (the pipeline is alive) but buffers nothing;
        # onset writes the pre-roll, then the frames that follow it.
        esp_sr._feed(b"\x09\x09" * 8)
        self.assertEqual(det.read(64), b"")
        esp_sr._vad(True, preroll=b"\x01\x02" * 8)
        esp_sr._feed(b"\x03\x04" * 16)
        self.assertEqual(det.read(16), b"\x01\x02" * 8)
        self.assertEqual(det.read(1024), b"\x03\x04" * 16)
        self.assertEqual(det.read(1024), b"")
        # Offset closes the segment: later frames are not buffered.
        esp_sr._vad(False)
        esp_sr._feed(b"\x05\x06" * 8)
        self.assertEqual(det.poll_events(), ["vad_on", "vad_off"])
        self.assertEqual(det.read(64), b"")
        self.assertEqual(det.capture_stats(), (3, 0))
        # Re-arming inside an open segment flushes the ring and restarts the
        # VAD, which is how a wake word and a playback tail are left behind:
        # neither is re-announced as the utterance to recognise.
        esp_sr._vad(True)
        self.assertEqual(det.poll_events(), ["vad_on"])
        esp_sr._feed(b"\x07\x08" * 8)
        det.capture(False)
        self.assertTrue(det.capture(True))
        self.assertEqual(det.read(64), b"")
        self.assertEqual(det.poll_events(), [])
        # Only a fresh onset opens the next segment.
        esp_sr._vad(True)
        self.assertEqual(det.poll_events(), ["vad_on"])
        self.assertTrue(det.capture(False))
        det.stop()

    def test_channel_afe_listening(self):
        from app.channels.voice import STATE_LISTENING, VoiceChannel

        class FakeBus:
            pass

        ch = VoiceChannel(FakeBus(), voice_cfg_fn=lambda: {})
        committed = []

        def fake_process(pcm, rate):
            committed.append((len(pcm), rate))
            return True

        ch._process_speech = fake_process
        ch._wakeword = EspSrWakeWord()
        ch._wakeword.start()
        ch._enter_listening()
        # The AFE keeps the mic and stays armed: no teardown per turn.
        self.assertTrue(ch._afe_mode)
        self.assertTrue(ch._wakeword.active)
        self.assertIsNone(ch._temp_audio_in)
        ch._loop_listening(3200)
        self.assertFalse(ch._afe_speech)

        # Playback tail: echo is drained, and the segment the echo opened is
        # dropped rather than captured.
        ch._mute_until_ms = time.ticks_add(time.ticks_ms(), 300)
        esp_sr._vad(True, preroll=b"\x01\x02" * 16)
        esp_sr._feed(b"\x03\x04" * 1600)
        ch._loop_listening(3200)
        self.assertFalse(ch._afe_speech)
        self.assertEqual(len(ch._pcm_buf), 0)

        # Tail over: the first iteration re-arms the capture (flushing the echo
        # and restarting the VAD), so whoever is still talking has to onset
        # again before the next iteration opens the segment.
        ch._mute_until_ms = 0
        esp_sr._feed(b"\x03\x04" * 1600)
        ch._loop_listening(3200)
        self.assertFalse(ch._afe_speech)
        esp_sr._vad(True, preroll=b"\x03\x04" * 1600)
        ch._loop_listening(3200)
        self.assertTrue(ch._afe_speech)
        self.assertEqual(len(ch._pcm_buf), 3200)
        # The segment's tail is still in the ring when vad_off arrives: the
        # commit drains it instead of clipping the end of the utterance.
        esp_sr._feed(b"\x05\x06" * 3200)
        esp_sr._vad(False)
        ch._loop_listening(3200)
        self.assertEqual(committed, [(3200 + 6400, 16000)])
        self.assertEqual(ch._state, STATE_LISTENING)
        self.assertTrue(ch._followup_until_ms)
        self.assertTrue(ch._afe_mode)
        self.assertEqual(len(ch._pcm_buf), 0)

        # Follow-up window expiring silently returns to the wake stage.
        ch._followup_until_ms = time.ticks_add(time.ticks_ms(), -1)
        ch._loop_listening(3200)
        self.assertFalse(ch._afe_mode)
        self.assertTrue(ch._wakeword.active)

    def test_falls_back_without_capture(self):
        from app.channels.voice import VoiceChannel

        class FakeBus:
            pass

        class AudioIn:
            rate = 16000

            def read(self, n):
                return b"\x00" * n

            def deinit(self):
                pass

        # Firmware without the capture API: detector stops, energy VAD path.
        esp_sr._capture_ready = False
        ch = VoiceChannel(FakeBus(), voice_cfg_fn=lambda: {})
        ch._wakeword = EspSrWakeWord()
        ch._wakeword.start()
        ch._enter_listening()
        self.assertFalse(ch._afe_mode)
        self.assertFalse(ch._wakeword.active)
        # An injected audio input (app-flow tests) also keeps the AFE off.
        esp_sr._reset()
        ch2 = VoiceChannel(FakeBus(), voice_cfg_fn=lambda: {}, audio_in=AudioIn())
        ch2._wakeword = EspSrWakeWord()
        ch2._wakeword.start()
        ch2._enter_listening()
        self.assertFalse(ch2._afe_mode)
        self.assertFalse(ch2._wakeword.active)

        # A capture path that never delivers a frame latches itself off, so a
        # firmware without a working AFE costs one turn instead of deafness.
        esp_sr._reset()
        ch3 = VoiceChannel(FakeBus(), voice_cfg_fn=lambda: {})
        ch3._wakeword = EspSrWakeWord()
        ch3._wakeword.start()
        ch3._enter_listening()
        self.assertTrue(ch3._afe_mode)
        ch3._listen_start_ms = time.ticks_add(time.ticks_ms(), -config.VOICE_LISTEN_TIMEOUT_MS - 1)
        ch3._loop_listening(3200)
        self.assertTrue(ch3._afe_off)
        ch3._enter_listening()
        self.assertFalse(ch3._afe_mode)
        self.assertFalse(ch3._wakeword.active)


class TestEnergyWakeWord(unittest.TestCase):
    def test_trigger_and_reset(self):
        det = EnergyWakeWord(threshold=80)
        self.assertFalse(det.owns_mic)
        self.assertIsNone(det.poll(LOUD))
        det.start()
        self.assertIsNone(det.poll(None))
        self.assertIsNone(det.poll(SILENCE))
        self.assertEqual(det.poll(LOUD), "wakeup")
        self.assertIsNone(det.poll(LOUD))
        det.reset()
        self.assertEqual(det.poll(LOUD), "wakeup")
        det.stop()


class TestFactory(unittest.TestCase):
    def setUp(self):
        esp_sr._reset()

    def test_prefers_espsr_and_interface(self):
        det = create_wakeword()
        self.assertIsInstance(det, EspSrWakeWord)


class TestVoiceChannelStates(unittest.TestCase):
    def setUp(self):
        esp_sr._reset()

    def _make_channel(self, **kw):
        from app.channels.voice import VoiceChannel

        class FakeBus:
            pass

        return VoiceChannel(FakeBus(), voice_cfg_fn=lambda: {}, **kw)

    def test_channel_state_transitions(self):
        from app.channels.voice import STATE_IDLE

        ch = self._make_channel()
        self.assertEqual(ch._state, STATE_IDLE)
        self.assertFalse(ch._recording)
        ch._recording = True
        ch._pcm_buf = bytearray(b"\x01\x02")
        ch._enter_idle()
        self.assertEqual(ch._state, STATE_IDLE)
        self.assertFalse(ch._recording)
        self.assertEqual(len(ch._pcm_buf), 0)

        from app.channels.voice import STATE_LISTENING

        class FakeAudioIn:
            rate = 16000

            def read(self, n):
                return b"\x00" * n

            def deinit(self):
                pass

        # owns_mic=True without the capture API: stops detector so the channel
        # can open the mic itself (the AFE path is TestEspSrCapture's subject).
        esp_sr._capture_ready = False
        ch = self._make_channel()
        ch._wakeword = EspSrWakeWord()
        ch._wakeword.start()
        ch._enter_listening()
        self.assertEqual(ch._state, STATE_LISTENING)
        self.assertFalse(ch._wakeword.active)
        ch._release_temp_audio()
        # owns_mic=False: keeps detector active
        ch2 = self._make_channel(audio_in=FakeAudioIn())
        ch2._wakeword = EnergyWakeWord()
        ch2._wakeword.start()
        ch2._enter_listening()
        self.assertEqual(ch2._state, STATE_LISTENING)
        self.assertTrue(ch2._wakeword.active)

        # Quiet warmup calibrates the floor without triggering; sustained
        # loud input right after warmup is detected promptly.
        class AudioIn:
            rate = 16000

            def __init__(self, pcm):
                self._pcm = pcm

            def read(self, n):
                return (self._pcm * 4)[:n]

            def deinit(self):
                pass

        ch = self._make_channel(audio_in=AudioIn(SILENCE))
        ch._wakeword = EnergyWakeWord()
        ch._wakeword.start()
        ch._enter_listening()
        chunk = 3200
        for _ in range(3):
            ch._loop_listening(chunk)
        self.assertFalse(ch._vad._had_voice)
        self.assertEqual(len(ch._pcm_buf), 0)
        self.assertEqual(ch._vad.noise_floor, 0)
        ch._audio_in = AudioIn(LOUD)
        ch._loop_listening(chunk)
        self.assertTrue(ch._vad._had_voice)
        self.assertGreater(len(ch._pcm_buf), 0)


if __name__ == "__main__":
    unittest.main(globals())
