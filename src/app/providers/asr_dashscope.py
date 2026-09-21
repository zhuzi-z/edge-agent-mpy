"""Aliyun DashScope Fun-ASR realtime WebSocket protocol.

Implements the Fun-ASR realtime protocol:
  1. Connect with Authorization header
  2. Send run-task JSON
  3. Stream binary PCM audio frames
  4. Send finish-task JSON
  5. Collect result-generated events until task-finished
"""

import json
import time
import app.config as config
import app.log as log
from app.providers.base import ASRProvider, ProviderError
from app.providers.ws import WSError, make_task_id, parse_ws_endpoint, ws_connect


class ASRWSError(ProviderError):
    """Fun-ASR failure: missing config, transport, or protocol error."""


ASR_WS_MODEL_DEFAULT = "paraformer-realtime-v2"


def _run_task_msg(task_id, model, sample_rate):
    """Build run-task JSON message."""
    return json.dumps(
        {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": model,
                "parameters": {
                    "format": "pcm",
                    "sample_rate": sample_rate,
                },
                "input": {},
            },
        }
    )


def _finish_task_msg(task_id):
    """Build finish-task JSON message."""
    return json.dumps(
        {
            "header": {
                "action": "finish-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "input": {},
            },
        }
    )


# 100ms of 16kHz 16-bit mono: the frame size the duplex protocol expects.
_CHUNK_BYTES = 3200


class ASRStream:
    """One Fun-ASR duplex session, fed while the user is still talking.

    On a buffered transcribe the handshake and the audio upload both land on
    the critical path *after* the VAD commits (measured on device: connect
    500-740ms, send 620-950ms for 2-3s of speech). Opening the session when
    speech starts and pushing each captured chunk straight out leaves only the
    finish-task round-trip after the commit, so both costs hide behind the
    utterance itself.

    ``feed`` never raises: a transport failure marks the session dead and the
    caller falls back to the buffered path, which still has the PCM.
    """

    def __init__(self, ws, task_id, sample_rate, t_open, t_connect, t_started):
        self._ws = ws
        self._task_id = task_id
        self._rate = sample_rate
        self._t_open = t_open
        self._t_connect = t_connect
        self._t_started = t_started
        self._t_sent = t_started
        self._t_done = 0
        self._send_ms = 0
        self._sent = 0
        self._pending = bytearray()
        self._dead = False

    @property
    def dead(self):
        """True once a transport error has made the session unusable."""
        return self._dead

    def feed(self, pcm):
        """Send captured PCM, chunked into protocol-sized frames.

        Callers pass on whatever their audio source produced - the AFE
        delivers 32ms frames - so short reads are accumulated here until a
        full frame is ready. Sending thirds of a frame costs three times the
        framing, masking and TCP segments for the same audio.
        """
        if self._dead or not pcm or self._ws is None:
            return
        t = time.ticks_ms()
        try:
            self._pending.extend(pcm)
            # Everything but the trailing partial frame goes out now.
            offset = 0
            limit = len(self._pending) - _CHUNK_BYTES + 1
            while offset < limit:
                end = offset + _CHUNK_BYTES
                self._ws.send_binary(bytes(self._pending[offset:end]))
                self._sent += _CHUNK_BYTES
                offset = end
            if offset:
                # Slice, not del: MicroPython bytearrays have no item deletion.
                self._pending = self._pending[offset:]
                self._t_sent = time.ticks_ms()
        except (WSError, OSError):
            self._dead = True
        self._send_ms += time.ticks_diff(time.ticks_ms(), t)

    def _flush(self):
        """Send the trailing partial frame once the capture has ended."""
        if not self._pending or self._ws is None:
            return
        pending, self._pending = self._pending, bytearray()
        t = time.ticks_ms()
        try:
            self._ws.send_binary(bytes(pending))
        finally:
            self._send_ms += time.ticks_diff(time.ticks_ms(), t)
        self._sent += len(pending)
        self._t_sent = time.ticks_ms()

    def finish(self):
        """Send finish-task and collect the transcript.

        Raises ASRWSError on any transport or protocol failure (including a
        session killed by an earlier ``feed``), so the caller can retry with
        the buffered PCM. Always closes the session.
        """
        if self._dead:
            self.close()
            raise ASRWSError("audio upload failed")
        try:
            self._flush()
            self._ws.send_text(_finish_task_msg(self._task_id))
            sentences = []
            while True:
                msg = self._ws.recv_json()
                hdr = msg.get("header", {})
                event = hdr.get("event", "")
                if event == "task-finished":
                    break
                if event == "task-failed":
                    code = hdr.get("error_code", "unknown")
                    err_msg = hdr.get("error_message", "")
                    raise ASRWSError("task-failed: {} {}".format(code, err_msg))
                if event == "result-generated":
                    sentence = msg.get("payload", {}).get("output", {}).get("sentence", {})
                    if sentence.get("heartbeat"):
                        continue
                    if sentence.get("sentence_end"):
                        text = sentence.get("text", "")
                        if text:
                            sentences.append(text)
            self._t_done = time.ticks_ms()
            return "".join(sentences)
        except WSError as e:
            raise ASRWSError(str(e))
        finally:
            self.close()

    def close(self):
        """Drop the session and emit the latency breakdown (idempotent)."""
        if self._ws is None:
            return
        ws, self._ws = self._ws, None
        done = self._t_done or time.ticks_ms()
        # connect/started/send now happen while the user is still talking, so
        # they are costs the listener no longer pays: "overlap" is how much of
        # the session rode along with the utterance (0 on the buffered path),
        # and "result" is the only part left after the commit.
        span = time.ticks_diff(self._t_sent, self._t_started)
        log.timing(
            "ASR",
            "audio={}ms connect={}ms started={}ms send={}ms overlap={}ms result={}ms total={}ms".format(
                self._sent * 1000 // (self._rate * 2),
                time.ticks_diff(self._t_connect, self._t_open),
                time.ticks_diff(self._t_started, self._t_connect),
                self._send_ms,
                span - self._send_ms,
                time.ticks_diff(done, self._t_sent),
                time.ticks_diff(done, self._t_open),
            ),
        )
        ws.close()


class DashScopeASR(ASRProvider):
    """Aliyun DashScope Fun-ASR (paraformer-realtime over WebSocket)."""

    name = "dashscope"

    def _session(self, cfg, timeout):
        """Resolve cfg and open a session that has reached task-started."""
        endpoint = (cfg.get("asr_ws_host") or "").strip()
        api_key = (cfg.get("asr_ws_api_key") or "").strip()
        if not endpoint or not api_key:
            raise ASRWSError("asr_ws_host and asr_ws_api_key required")

        model = cfg.get("asr_ws_model") or ASR_WS_MODEL_DEFAULT
        sample_rate = config.VOICE_SAMPLE_RATE
        ca_path = cfg.get("ca_path") or None
        sock_timeout = timeout or config.VOICE_REQUEST_TIMEOUT_SEC
        headers = {"Authorization": "Bearer " + api_key}

        t_open = time.ticks_ms()
        try:
            host, port, path = parse_ws_endpoint(endpoint)
            ws = ws_connect(host, port, path, headers, timeout=sock_timeout, ca_path=ca_path)
        except (OSError, WSError) as e:
            raise ASRWSError("connect failed: {}".format(e))
        t_connect = time.ticks_ms()

        task_id = make_task_id()
        try:
            ws.send_text(_run_task_msg(task_id, model, sample_rate))
            msg = ws.recv_json()
            event = msg.get("header", {}).get("event", "")
            if event != "task-started":
                raise ASRWSError("expected task-started, got: {}".format(event))
        except WSError as e:
            ws.close()
            raise ASRWSError(str(e))
        return ASRStream(ws, task_id, sample_rate, t_open, t_connect, time.ticks_ms())

    def stream(self, cfg, timeout=None):
        """Open a duplex session for live capture. Returns ASRStream.

        cfg keys are the same as for :meth:`transcribe`. The caller owns the
        result and must call ``finish()`` or ``close()``.
        """
        return self._session(cfg, timeout)

    def transcribe(self, cfg, pcm_bytes, timeout=None):
        """ASR via WebSocket: PCM16-LE bytes -> recognized text.

        cfg keys:
          - asr_ws_host: full endpoint URL
            (e.g. 'wss://dashscope.aliyuncs.com/api-ws/v1/inference')
          - asr_ws_api_key: API key for Authorization header
          - asr_ws_model: model name (default: paraformer-realtime-v2)
          - ca_path: optional CA cert path for TLS

        Returns the final recognized text (concatenation of all sentence_end
        results). This is the buffered path - the voice channel prefers
        :meth:`stream` so the upload overlaps with the utterance.
        """
        stream = self._session(cfg, timeout)
        try:
            stream.feed(pcm_bytes)
            return stream.finish()
        finally:
            stream.close()
