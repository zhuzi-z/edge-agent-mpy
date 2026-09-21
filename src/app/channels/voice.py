"""Voice channel: wake word → I2S mic → VAD → ASR → Bus → TTS → speaker.

State machine
─────────────
  IDLE ──(wakeup)──► LISTENING ──(commit)──► PROCESSING ──► IDLE
                         │ (timeout)
                         └──────────────────────────────────► IDLE

PROCESSING normally speaks the reply and reopens follow-up listening;
when the agent invoked the voice_control skill's exit action the reply
is spoken and the channel returns straight to the wake stage (no
follow-up window). The skill's volume action changes the speaker volume
mid-conversation (the value is re-read before every playback).

Wake word detection is polymorphic (see ``app.audio.wakeword``).
The channel interacts only with the BaseWakeWord interface and uses
``owns_mic`` to manage AudioInput lifecycle:
  - owns_mic=True:  detector holds mic in IDLE; channel opens a temporary
    AudioInput only during LISTENING.
  - owns_mic=False: channel opens AudioInput at start and keeps it open;
    PCM is fed to the detector (IDLE) and VAD (LISTENING).

With capture-capable ESP-SR firmware (``supports_capture``) LISTENING skips
the AudioInput entirely: the AFE keeps the mic, buffers the utterance itself
and reports its VAD transitions. That is cheaper - no detector teardown and
I2S re-open per turn - and endpoints on the AFE's spectral VAD instead of an
amplitude threshold. ``VOICE_AFE_VAD`` and an injected ``audio_in`` decide
which path runs.
"""

import time
import _thread
import app.config as config
import app.log as log
from app.providers import ASRWSError, ProviderError, TTSWSError, get_asr, get_tts
from app.channel import BaseChannel, Message
from app.audio import Vad, pcm16_scale, make_tone
from app.audio.wakeword import create_wakeword
from app.util import ms_since

STATE_IDLE = 0
STATE_LISTENING = 1
STATE_PROCESSING = 2


def do_asr(cfg, pcm):
    """ASR via the configured provider. Returns recognized text."""
    return get_asr(cfg).transcribe(cfg, pcm)


def tts_rate(cfg):
    """Output sample rate of the configured TTS provider."""
    return get_tts(cfg).sample_rate(cfg)


def do_tts_stream(cfg, text, sink, ws=None):
    """TTS via the configured provider, streaming PCM to sink(chunk).

    Chunks are handed to ``sink`` as they arrive so memory stays bounded
    regardless of reply length. ``ws`` is an optional connection pre-opened
    while the LLM was still thinking. Returns total PCM bytes sent.
    """
    return get_tts(cfg).synthesize(cfg, text, sink=sink, ws=ws)


class _BufferedSink:
    """Collects PCM until a threshold before feeding the speaker.

    Streaming TTS chunks arrive with network jitter; playback starts only
    after the pre-buffer is filled so gaps don't underrun the speaker.
    """

    def __init__(self, write, prebuf_bytes):
        self._write = write
        self._pending = []
        self._need = prebuf_bytes

    def __call__(self, chunk):
        if self._need <= 0:
            self._write(chunk)
            return
        self._pending.append(chunk)
        self._need -= len(chunk)
        if self._need <= 0:
            self._write(b"".join(self._pending))
            self._pending = []

    def flush(self):
        if self._pending:
            self._write(b"".join(self._pending))
            self._pending = []
        self._need = 0


_MD_CHARS = "*#|`>~_"


def prepare_for_voice(text, max_chars=None):
    """Make an LLM reply speakable: drop markdown/emoji, cap length."""
    if max_chars is None:
        max_chars = config.VOICE_TTS_MAX_CHARS
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip table separator rows (---, |---|---|, :---: ...).
        if not line.replace("|", "").replace("-", "").replace(":", "").replace(" ", ""):
            continue
        cleaned = []
        for ch in line:
            o = ord(ch)
            if ch in _MD_CHARS:
                cleaned.append(" ")
            elif (
                o < 0x2500
                or 0x3000 <= o <= 0x303F
                or 0x4E00 <= o <= 0x9FFF
                or 0xFF00 <= o <= 0xFFEF
            ):
                cleaned.append(ch)
            else:
                cleaned.append(" ")
        lines.append("".join(cleaned))
    out = " ".join(lines)
    # One split/join pass collapses every whitespace run; the while-replace
    # loop this replaces rescanned the whole string per collapse.
    out = " ".join(out.split())
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


class VoiceChannel(BaseChannel):
    """Voice conversation channel with wake word + I2S hardware audio loop."""

    name = "voice"

    def __init__(self, bus, voice_cfg_fn, audio_in=None, audio_out=None):
        super().__init__(bus)
        self._voice_cfg_fn = voice_cfg_fn
        self._audio_in = audio_in
        self._audio_out = audio_out
        self._owns_audio_out = False
        self._vad = Vad(post_silence_ms=config.VOICE_VAD_POST_SILENCE_MS)
        self._wakeword = create_wakeword()
        self._state = STATE_IDLE
        self._recording = False
        self._pcm_buf = bytearray()
        self._asr_stream = None
        self._busy = False
        self._thread_alive = False
        self._thread_done = True
        self._crashed = False
        self._restart_at_ms = 0
        self._restarts = 0
        self._listen_start_ms = 0
        self._t_wake_ms = 0
        self._temp_audio_in = None
        self._noise_logged = False
        self._mute_until_ms = 0
        self._followup_until_ms = 0
        self._afe_mode = False
        self._afe_speech = False
        self._afe_muted = False
        self._afe_off = False
        self._last_audio_ms = 0

    async def start(self):
        self._running = True
        cfg = self._voice_cfg_fn()
        if cfg.get("voice_enabled", True):
            self._start_audio()

    async def stop(self):
        self._running = False
        self._stop_audio()
        self._wakeword.stop()
        self._release_temp_audio()
        if self._audio_in:
            try:
                self._audio_in.deinit()
            except Exception:
                pass
        if self._audio_out:
            try:
                self._audio_out.deinit()
            except Exception:
                pass

    def set_enabled(self, enabled):
        if enabled:
            self._start_audio()
        else:
            self._stop_audio()

    def supervise(self):
        """Restart the audio thread if it died unexpectedly.

        ``_audio_loop`` swallows its own exceptions so a single failure cannot
        take the process down - but that also left the device deaf until a
        reboot. The main loop calls this periodically; only an exit through the
        exception path is recovered, never a deliberate stop (WebUI voice
        toggle, channel shutdown). Restarts are rate-limited so a failure that
        recurs at once retries instead of spinning up threads back to back.
        Returns True when a restart happened.
        """
        if not self._crashed or not self._running or self._thread_alive:
            return False
        now = time.ticks_ms()
        if self._restart_at_ms and time.ticks_diff(self._restart_at_ms, now) > 0:
            return False
        self._crashed = False
        self._restart_at_ms = time.ticks_add(now, config.VOICE_THREAD_RESTART_COOLDOWN_MS)
        self._restarts += 1
        log.warn("Voice", "restarting audio loop after crash (#{})".format(self._restarts))
        # Back to the wake stage first: the crash may have landed mid-utterance,
        # and a resumed LISTENING state would carry a stale buffer and clock.
        # Plain field writes only - touching the wake word or I2S here would do
        # it from the event-loop thread instead of the audio thread.
        self._state = STATE_IDLE
        self._recording = False
        self._busy = False
        self._pcm_buf = bytearray()
        self._followup_until_ms = 0
        self._afe_mode = False
        self._afe_speech = False
        self._start_audio()
        return True

    def play_test_tone(self):
        """Play a short beep at the configured volume (WebUI volume test).

        Returns True when the tone was written to the speaker.
        """
        if self._busy:
            return False
        cfg = self._voice_cfg_fn()
        volume = cfg.get("volume", config.SPEAKER_VOLUME_DEFAULT)
        self._ensure_audio_out()
        if self._audio_out is None:
            return False
        tone = make_tone(rate=self._audio_out.rate)
        if volume != 100:
            tone = pcm16_scale(tone, volume)
        try:
            self._audio_out.write(tone)
            self._audio_out.end()
        except OSError as e:
            log.error("Voice", "test tone failed: {}".format(e))
            return False
        return True

    # -- Mic lifecycle --------------------------------------------------------

    def _start_audio(self):
        self._crashed = False
        self._ensure_audio_out()
        if self._audio_out is None and self._audio_in is None and not self._wakeword.owns_mic:
            return
        if not self._thread_alive:
            self._wait_thread_done()
            self._thread_alive = True
            self._thread_done = False
            _thread.stack_size(config.VOICE_THREAD_STACK_SIZE)
            _thread.start_new_thread(self._audio_loop, ())
            log.info("Voice", "Audio loop started ({})".format(type(self._wakeword).__name__))

    def _stop_audio(self):
        # A deliberate stop is not a crash: clear the flag so supervise() does
        # not fight the WebUI's voice toggle.
        self._crashed = False
        if self._thread_alive:
            self._thread_alive = False
            log.info("Voice", "Audio loop stopping")

    def _wait_thread_done(self, timeout_ms=2000):
        """Spin-wait until the previous thread has fully exited."""
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while not self._thread_done:
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                log.warn("Voice", "old thread did not exit in time")
                break
            time.sleep(0.01)

    def _ensure_audio_out(self):
        """Lazily create AudioOutput for TTS playback."""
        if self._audio_out is None:
            from app.audio.io import AudioOutput

            self._audio_out = AudioOutput()
            self._owns_audio_out = True

    def _release_audio_out(self):
        """Deinit AudioOutput if we own it."""
        if self._owns_audio_out and self._audio_out is not None:
            try:
                self._audio_out.deinit()
            except Exception:
                pass
            self._audio_out = None
            self._owns_audio_out = False

    def _ensure_audio_in(self):
        """Get or create the AudioInput for the current phase."""
        if self._temp_audio_in is not None:
            return self._temp_audio_in
        if self._audio_in is not None:
            return self._audio_in
        from app.audio.io import AudioInput

        self._temp_audio_in = AudioInput()
        return self._temp_audio_in

    def _release_temp_audio(self):
        """Release temporary AudioInput (only when wakeword owns mic)."""
        if self._wakeword.owns_mic and self._temp_audio_in is not None:
            try:
                self._temp_audio_in.deinit()
            except Exception:
                pass
            self._temp_audio_in = None

    # -- State transitions ----------------------------------------------------

    def _enter_idle(self):
        self._state = STATE_IDLE
        self._recording = False
        self._pcm_buf = bytearray()
        self._close_asr_stream()
        self._vad.begin_listen()
        self._stop_afe_capture()
        self._release_temp_audio()
        self._wakeword.start()
        if hasattr(self._wakeword, "reset"):
            self._wakeword.reset()

    def _enter_listening(self):
        self._state = STATE_LISTENING
        self._recording = True
        self._pcm_buf = bytearray()
        self._vad.begin_listen()
        self._listen_start_ms = time.ticks_ms()
        self._t_wake_ms = self._listen_start_ms
        self._samples_fed = 0
        self._noise_logged = False
        self._afe_speech = False
        self._afe_muted = False
        self._last_audio_ms = self._listen_start_ms
        self._afe_mode = self._afe_capture_available() and self._wakeword.capture(True)
        if self._afe_mode:
            # The AFE keeps the mic and the wake word stays armed, so nothing
            # is torn down here and the next turn re-opens nothing.
            log.info("Voice", "Listening... (AFE VAD)")
            return
        if self._wakeword.owns_mic:
            self._wakeword.stop()
        log.info("Voice", "Listening...")

    def _afe_capture_available(self):
        """Whether this listen can take PCM and VAD events from the AFE.

        Off when an audio input is injected (app-flow tests stream PCM over a
        socket) and when the firmware predates the capture API; both fall back
        to a temporary AudioInput plus the energy VAD.
        """
        return (
            config.VOICE_AFE_VAD
            and not self._afe_off
            and self._audio_in is None
            and self._wakeword.owns_mic
            and getattr(self._wakeword, "supports_capture", False)
        )

    def _stop_afe_capture(self):
        """Stop AFE utterance buffering (no-op unless this listen used it)."""
        if self._afe_mode:
            self._afe_mode = False
            try:
                self._wakeword.capture(False)
            except OSError as e:
                log.warn("Voice", "AFE capture stop: {}".format(e))

    def _enter_followup(self):
        """After TTS playback: keep listening for a follow-up utterance.

        No wake word needed; if nothing is said within
        VOICE_FOLLOWUP_TIMEOUT_MS the channel returns to the wake stage.
        """
        self._followup_until_ms = time.ticks_add(time.ticks_ms(), config.VOICE_FOLLOWUP_TIMEOUT_MS)
        self._enter_listening()
        log.info("Voice", "Follow-up listening ({}ms)".format(config.VOICE_FOLLOWUP_TIMEOUT_MS))

    # -- Main audio loop ------------------------------------------------------

    def _audio_loop(self):
        """Background thread: unified state machine."""
        try:
            self._wakeword.start()

            if not self._wakeword.owns_mic and self._audio_in is None:
                from app.audio.io import AudioInput

                self._audio_in = AudioInput()

            audio_in = self._audio_in
            if audio_in is None and not self._wakeword.owns_mic:
                log.warn("Voice", "No audio input, loop exiting")
                return

            rate = audio_in.rate if audio_in else config.VOICE_SAMPLE_RATE
            chunk_bytes = rate * 2 * config.VOICE_CHUNK_MS // 1000

            while self._running and self._thread_alive:
                if self._state == STATE_IDLE:
                    self._loop_idle(chunk_bytes)
                elif self._state == STATE_LISTENING:
                    self._loop_listening(chunk_bytes)
                else:
                    time.sleep(0.01)
        except Exception as e:
            log.error("Voice", "Audio loop crashed: {}".format(e))
            # Marks the channel for recovery; supervise() (main loop) restarts
            # the thread, so one unexpected error does not leave the device
            # permanently deaf until someone reboots it.
            self._crashed = True
        finally:
            self._stop_afe_capture()
            self._wakeword.stop()
            self._close_asr_stream()
            self._release_temp_audio()
            self._release_audio_out()
            self._thread_alive = False
            self._thread_done = True
            log.info("Voice", "Audio loop exited")

    def _loop_idle(self, chunk_bytes):
        """IDLE: wait for wake word event."""
        muted = time.ticks_diff(self._mute_until_ms, time.ticks_ms()) > 0
        if self._wakeword.owns_mic:
            ev = self._wakeword.poll()
            if ev == "wakeup":
                if muted:
                    log.info("Voice", "Wake ignored (TTS playback tail)")
                    return
                log.info("Voice", "Wake word detected!")
                self._enter_listening()
            else:
                time.sleep(0.05)
        else:
            if muted:
                time.sleep(0.01)
                return
            audio_in = self._ensure_audio_in()
            try:
                pcm = audio_in.read(chunk_bytes)
            except OSError:
                time.sleep(0.01)
                return
            if not pcm:
                time.sleep(0.01)
                return
            ev = self._wakeword.poll(pcm)
            if ev == "wakeup":
                log.info("Voice", "Energy trigger detected!")
                self._enter_listening()
                self._pcm_buf = bytearray(pcm)

    def _loop_listening(self, chunk_bytes):
        """LISTENING: capture speech via VAD until commit or timeout."""
        if self._afe_mode:
            self._loop_listening_afe(chunk_bytes)
            return
        # Follow-up window: back to the wake stage when it expires silently.
        if self._followup_until_ms and not self._vad.speaking:
            if time.ticks_diff(time.ticks_ms(), self._followup_until_ms) > 0:
                log.info("Voice", "Follow-up timeout, back to wake word")
                self._followup_until_ms = 0
                self._enter_idle()
                return
        # Playback tail: drop mic input so TTS echo doesn't feed the VAD.
        if time.ticks_diff(self._mute_until_ms, time.ticks_ms()) > 0:
            time.sleep(0.01)
            return

        audio_in = self._ensure_audio_in()
        rate = audio_in.rate

        try:
            pcm = audio_in.read(chunk_bytes)
        except OSError:
            pcm = b""
        if not pcm:
            pcm = b"\x00" * chunk_bytes
            time.sleep(0.01)

        self._samples_fed += len(pcm) // 2
        elapsed_ms = self._samples_fed * 1000 // rate
        # Warmup/calibration only until the VAD has a noise floor; later
        # listens (and follow-ups) detect from the very first chunk.
        if elapsed_ms <= config.VOICE_WARMUP_MS and not self._vad.calibrated:
            self._vad.calibrate(pcm)
            return
        now_ms = self._listen_start_ms + elapsed_ms
        event = self._vad.feed(pcm, now_ms)
        if not self._noise_logged and self._vad.noise_floor is not None:
            self._noise_logged = True
            log.info("Voice", "Noise floor {}".format(self._vad.noise_floor))

        if event == "speech_start":
            self._followup_until_ms = 0
            # The local buffer stays the source of truth: it is the fallback
            # when the pre-opened session dies mid-utterance.
            self._pcm_buf = bytearray(pcm)
            self._open_asr_stream()
            self._feed_asr(pcm)
            log.info("Voice", "Speech started")
        elif self._recording:
            self._pcm_buf.extend(pcm)
            self._feed_asr(pcm)

        if event == "commit":
            self._commit_speech(rate, self._vad.last_speech_ms)
            return

        # Wall-clock timeout: with socket-mocked audio, reads can stall or
        # return instantly, so sample-based time may diverge from real time.
        # Never cut the user off mid-utterance: the timeout only applies
        # while no speech segment is open.
        if (
            not self._vad.speaking
            and time.ticks_diff(time.ticks_ms(), self._listen_start_ms)
            > config.VOICE_LISTEN_TIMEOUT_MS
        ):
            log.info("Voice", "Listen timeout, back to idle")
            self._enter_idle()

    def _loop_listening_afe(self, chunk_bytes):
        """LISTENING with the AFE holding the mic and driving the endpoint.

        The firmware buffers only utterance audio - its VAD pre-roll first,
        then every frame until its trailing-silence window expires - and the
        channel delimits the segment with the same events: ``vad_on`` opens it,
        ``vad_off`` commits it. Audio read outside a segment is dropped, which
        is what keeps the speaker echo of a playback tail out of the ASR (a
        single-mic board gives the AFE no echo reference). No warmup, no noise
        floor, no amplitude threshold for a noise burst to fool.
        """
        now_ms = time.ticks_ms()
        # Follow-up window: back to the wake stage when it expires silently.
        if self._followup_until_ms and not self._afe_speech:
            if time.ticks_diff(now_ms, self._followup_until_ms) > 0:
                log.info("Voice", "Follow-up timeout, back to wake word")
                self._followup_until_ms = 0
                self._enter_idle()
                return

        try:
            pcm = self._wakeword.read(chunk_bytes, config.VOICE_AFE_READ_TIMEOUT_MS)
        except OSError as e:
            log.error("Voice", "AFE read: {}".format(e))
            pcm = b""
        events = self._wakeword.poll_events()

        if time.ticks_diff(self._mute_until_ms, time.ticks_ms()) > 0:
            self._afe_muted = True
            return
        if self._afe_muted:
            # The tail just ended: re-arm, which flushes the echo the AFE
            # buffered and re-announces a still-open segment (someone talking
            # over the reply) as the vad_on that opens it.
            self._afe_muted = False
            self._wakeword.capture(False)
            if not self._wakeword.capture(True):
                log.warn("Voice", "AFE re-arm failed, back to idle")
                self._enter_idle()
            return

        if "vad_on" in events and not self._afe_speech:
            self._afe_speech = True
            self._recording = True
            self._followup_until_ms = 0
            self._last_audio_ms = time.ticks_ms()
            # The local buffer is the source of truth: it is the fallback when
            # the pre-opened session dies mid-utterance.
            self._pcm_buf = bytearray(pcm)
            # Opened before the first chunk goes out, so the handshake overlaps
            # the utterance instead of following it.
            self._open_asr_stream()
            self._feed_asr(pcm)
            log.info("Voice", "Speech started")
        elif self._afe_speech and pcm:
            self._pcm_buf.extend(pcm)
            self._feed_asr(pcm)
            self._last_audio_ms = time.ticks_ms()

        rate = self._wakeword.rate
        if "vad_off" in events and self._afe_speech:
            # Drain the rest of the segment first: the ring still holds the
            # frames captured since the previous read, which are the last ones
            # of the utterance. Non-blocking, so this costs one extra pass.
            while True:
                tail = self._wakeword.read(chunk_bytes, 0)
                if not tail:
                    break
                self._pcm_buf.extend(tail)
                self._feed_asr(tail)
            self._commit_speech(rate)
            return

        # Nothing said inside the listen window. A segment the AFE never closes
        # (its VAD held open by sustained noise) is committed instead of left
        # to hang in LISTENING forever.
        now_ms = time.ticks_ms()
        if not self._afe_speech:
            if time.ticks_diff(now_ms, self._listen_start_ms) > config.VOICE_LISTEN_TIMEOUT_MS:
                frames = self._wakeword.capture_stats()[0]
                if not frames and not self._afe_off:
                    # A whole listen window without one AFE frame: the capture
                    # path does not work on this firmware. Stop using it rather
                    # than staying deaf - the next listen takes the mic itself.
                    self._afe_off = True
                    log.warn("Voice", "AFE capture silent, falling back to energy VAD")
                log.info("Voice", "Listen timeout, back to idle")
                self._enter_idle()
        elif time.ticks_diff(now_ms, self._last_audio_ms) > config.VOICE_AFE_MAX_SEGMENT_MS:
            log.warn("Voice", "AFE segment limit, committing")
            self._commit_speech(rate)

    def _commit_speech(self, rate, speech_ms=None):
        """Close the utterance, then run ASR -> agent -> TTS on this thread.

        ``speech_ms`` is the utterance length on the sample clock; whatever
        else the listen window spent is what the timing line reports as the
        gap. ``None`` derives it from the captured PCM, which is what the AFE
        path does: its buffer holds speech plus the trailing-silence window.
        """
        speech_pcm = bytes(self._pcm_buf)
        duration_ms = len(speech_pcm) * 1000 // (rate * 2)
        if speech_ms is None:
            speech_ms = duration_ms - config.VOICE_VAD_POST_SILENCE_MS
            if speech_ms < 0:
                speech_ms = 0
        log.info("Voice", "Speech committed ({}ms)".format(duration_ms))
        # gap is the dead time between the wake word and the commit that no
        # amount of streaming can remove: the pause before the user starts
        # talking, the VAD's trailing-silence window, and mic read stalls.
        # It is a fixed cost, so on a short utterance it outweighs the
        # transfer of the audio itself.
        wall_ms = ms_since(self._t_wake_ms)
        detail = "listen wall={}ms pcm={}ms speech={}ms gap={}ms vad={}".format(
            wall_ms,
            duration_ms,
            speech_ms,
            wall_ms - speech_ms,
            "afe" if self._afe_mode else "energy",
        )
        if self._afe_mode:
            # Non-zero means the capture ring overflowed: the reader stalled
            # for longer than it covers and audio went missing.
            dropped = self._wakeword.capture_stats()[1]
            if dropped:
                detail += " dropped={}B".format(dropped)
        log.timing("Voice", detail)
        self._state = STATE_PROCESSING
        self._recording = False
        self._afe_speech = False
        self._stop_afe_capture()
        self._release_temp_audio()
        self._busy = True
        try:
            played = self._process_speech(speech_pcm, rate)
        except Exception as e:
            log.error("Voice", "process speech: {}".format(e))
            self._beep(self._voice_cfg_fn(), 440, 160)
            played = False
        finally:
            self._busy = False
        if played:
            self._enter_followup()
        else:
            self._enter_idle()

    # -- Speech processing pipeline -------------------------------------------

    def _beep(self, cfg, freq, ms):
        """Short feedback tone so pipeline failures aren't silent.

        No end() call (real I2S end() is a no-op, but the e2e socket stub
        closes the connection on end()). Sets the playback-tail mute so
        the tone's echo can't retrigger wake/VAD.
        """
        if self._audio_out is None:
            self._ensure_audio_out()
        if self._audio_out is None:
            return
        try:
            volume = cfg.get("volume", config.SPEAKER_VOLUME_DEFAULT)
            tone = make_tone(freq=freq, ms=ms, rate=self._audio_out.rate)
            if volume != 100:
                tone = pcm16_scale(tone, volume)
            self._audio_out.write(tone)
            self._mute_until_ms = time.ticks_add(time.ticks_ms(), config.VOICE_PLAYBACK_TAIL_MS)
        except OSError as e:
            log.error("Voice", "feedback tone failed: {}".format(e))

    # -- Streaming ASR --------------------------------------------------------

    def _open_asr_stream(self):
        """Open the ASR session the moment the VAD hears speech.

        The handshake (500-740ms measured) and the audio upload (620-950ms)
        used to run back to back *after* the commit, while the listener waited
        in silence. Both now overlap with the utterance, leaving only the
        finish-task round-trip on the critical path.

        Best effort on purpose: the handshake stalls the capture loop, so the
        mic ring buffer (I2S_BUF_SIZE) has to be big enough to hold it, and if
        the session can't be opened the buffered path at commit still works.
        """
        self._close_asr_stream()
        cfg = self._voice_cfg_fn()
        if not (cfg.get("asr_ws_host") or "").strip():
            return
        try:
            self._asr_stream = get_asr(cfg).stream(cfg)
        except (ProviderError, OSError, NotImplementedError) as e:
            log.warn("Voice", "ASR prewarm failed: {}".format(e))
            self._asr_stream = None

    def _feed_asr(self, pcm):
        """Push one captured chunk upstream; never blocks the state machine."""
        if self._asr_stream is not None:
            self._asr_stream.feed(pcm)

    def _close_asr_stream(self):
        """Drop an unused session (listen timeout, new utterance, shutdown)."""
        if self._asr_stream is not None:
            stream = self._asr_stream
            self._asr_stream = None
            stream.close()

    def _prewarm_tts(self, cfg):
        """Open the TTS socket while the LLM is still thinking.

        The handshake costs 590-630ms on device and used to run *after* the
        reply was in hand, entirely inside the listener's wait. The LLM
        round-trip (2.3-2.5s) hides it completely. Best effort: on failure
        ``_speak`` connects the old way.
        """
        if not (cfg.get("tts_ws_host") or "").strip():
            return None
        try:
            return get_tts(cfg).connect(cfg)
        except (ProviderError, OSError, NotImplementedError) as e:
            log.warn("Voice", "TTS prewarm failed: {}".format(e))
            return None

    @staticmethod
    def _close_tts(ws):
        """Drop a prewarmed TTS socket that never reached _speak."""
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _finish_asr(self, cfg, pcm):
        """Transcript from the pre-opened session, else the buffered path."""
        stream = self._asr_stream
        self._asr_stream = None
        if stream is None:
            return do_asr(cfg, pcm)
        try:
            return stream.finish()
        except ASRWSError as e:
            log.warn("Voice", "ASR stream failed ({}), retrying buffered".format(e))
            return do_asr(cfg, pcm)

    def _process_speech(self, pcm, rate):
        """Full pipeline: PCM → ASR → Bus → TTS → speaker.

        Returns True when TTS was played (caller opens follow-up
        listening), else False (caller returns to the wake stage).
        When the agent invoked the voice_control skill's exit action,
        the farewell from the skill's "ack" argument is spoken and False
        is returned so the channel goes back to the wake stage.
        """
        cfg = self._voice_cfg_fn()
        if not cfg.get("base_url") or not cfg.get("api_key"):
            log.warn("Voice", "LLM not configured, skipping")
            self._beep(cfg, 440, 160)
            return False

        try:
            transcript = self._finish_asr(cfg, pcm)
        except ASRWSError as e:
            log.error("Voice", "ASR failed: {}".format(e))
            self._beep(cfg, 440, 160)
            return False
        if not transcript or not transcript.strip():
            # No tone here: an empty transcript is what a cough, a noise burst
            # or a clipped utterance produces, and a beep straight after the
            # wake word reads as a fault. The genuine failures below still
            # announce themselves.
            self._beep(cfg, 440, 160)
            log.info("Voice", "ASR empty, back to idle")
            return False

        log.info("Voice", "ASR: {}".format(transcript))
        message = Message(channel=self.name, content=transcript)
        log.info("Voice", "Thinking...")
        tts_ws = self._prewarm_tts(cfg)
        reply = self.dispatch_sync(message)
        if not reply.ok:
            log.error("Voice", "Agent error: {}".format(reply.error))
            self._close_tts(tts_ws)
            self._beep(cfg, 440, 160)
            return False

        exit_voice = reply.metadata.get("exit_voice")
        log.info("Voice", "Reply: {}".format(reply.content[:80]))
        speak = prepare_for_voice(reply.content)
        if exit_voice:
            # Farewell priority: skill ack argument → reply text → fixed
            # local ack.
            speak = prepare_for_voice(reply.metadata.get("exit_ack") or "") or speak
            if not speak:
                speak = config.VOICE_EXIT_ACK
        played = self._speak(cfg, speak, tts_ws)
        if exit_voice:
            log.info("Voice", "Exit requested, back to wake stage")
            return False
        return played

    def _speak(self, cfg, text, ws=None):
        """Synthesize text and play it on the speaker.

        Returns True when audio was played. Sets the playback-tail mute
        so the device's own voice doesn't retrigger wake/VAD. ``ws`` is an
        optional TTS connection pre-opened during the LLM call; it is always
        consumed (or closed) here.
        """
        if not (text and self._audio_out):
            self._close_tts(ws)
            return False
        try:
            log.info("Voice", "TTS synthesizing...")
            out_rate = tts_rate(cfg)
            if out_rate != self._audio_out.rate:
                self._release_audio_out()
                from app.audio.io import AudioOutput

                self._audio_out = AudioOutput(rate=out_rate)
                self._owns_audio_out = True
            volume = cfg.get("volume", config.SPEAKER_VOLUME_DEFAULT)
            t_speak = time.ticks_ms()
            first_out = [0]

            def write(chunk):
                if volume != 100:
                    chunk = pcm16_scale(chunk, volume)
                if not first_out[0]:
                    # Sound reaching the speaker: everything the listener
                    # waited through is behind this point.
                    first_out[0] = time.ticks_ms()
                self._audio_out.write(chunk)

            prebuf = out_rate * 2 * config.VOICE_TTS_PREBUFFER_MS // 1000
            sink = _BufferedSink(write, prebuf)
            try:
                sent = do_tts_stream(cfg, text, sink, ws=ws)
            except TTSWSError as e:
                # A pre-opened socket can go stale while the LLM thinks
                # (the gateway drops idle sessions). Nothing has reached
                # the speaker yet in that case, so a fresh connection is a
                # clean retry; after playback started it would stutter.
                if ws is None or first_out[0]:
                    raise
                log.warn("Voice", "TTS prewarm stale ({}), reconnecting".format(e))
                sink = _BufferedSink(write, prebuf)
                sent = do_tts_stream(cfg, text, sink)
            sink.flush()
            self._audio_out.end()
            log.info("Voice", "TTS played ({} bytes)".format(sent))
            if sent:
                if first_out[0]:
                    # wake_to_speaker is the headline number; -1 means the
                    # playback was not started by a wake event.
                    log.timing(
                        "Voice",
                        "turn wake_to_speaker={}ms to_first_audio={}ms play={}ms".format(
                            (
                                time.ticks_diff(first_out[0], self._t_wake_ms)
                                if self._t_wake_ms
                                else -1
                            ),
                            time.ticks_diff(first_out[0], t_speak),
                            ms_since(first_out[0]),
                        ),
                    )
                self._mute_until_ms = time.ticks_add(
                    time.ticks_ms(), config.VOICE_PLAYBACK_TAIL_MS
                )
                return True
        except TTSWSError as e:
            log.error("Voice", "TTS failed: {}".format(e))
        return False

    def routes(self):
        return []
