"""Aliyun DashScope TTS WebSocket protocol (Sambert, sambert-*-v1).

1. Connect to the endpoint (tts_ws_host)
2. Send run-task with the full text (streaming "out"; no streaming
   input: continue-task / finish-task are unsupported)
3. Receive task-started
4. Receive binary PCM frames interleaved with result-generated events
5. Receive task-finished
"""

import json
import time
import app.config as config
import app.log as log
from app.providers.base import TTSProvider, TTSWSError
from app.providers.ws import WSError, make_task_id, parse_ws_endpoint, ws_connect, _WS_BINARY
from app.util import ms_since


TTS_WS_MODEL_DEFAULT = "sambert-zhiying-v1"
TTS_WS_SAMPLE_RATE_DEFAULT = 16000


def _run_task_msg(task_id, model, sample_rate, text):
    """Build the run-task JSON message carrying the full text."""
    return json.dumps(
        {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "out",
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": model,
                "parameters": {
                    "text_type": "PlainText",
                    "format": "pcm",
                    "sample_rate": sample_rate,
                },
                "input": {"text": text},
            },
        }
    )


class DashScopeTTS(TTSProvider):
    """Aliyun DashScope TTS (sambert-*-v1 over WebSocket)."""

    name = "dashscope"

    def sample_rate(self, cfg):
        """Output sample rate in Hz selected by cfg."""
        return cfg.get("tts_ws_sample_rate") or TTS_WS_SAMPLE_RATE_DEFAULT

    def _session_args(self, cfg, timeout):
        """Resolve cfg into everything a session needs.

        Returns (endpoint, model, sample_rate, ca_path, sock_timeout, headers).
        """
        endpoint = (cfg.get("tts_ws_host") or "").strip()
        api_key = (cfg.get("tts_ws_api_key") or "").strip()
        if not endpoint or not api_key:
            raise TTSWSError("tts_ws_host and tts_ws_api_key required")
        model = cfg.get("tts_ws_model") or TTS_WS_MODEL_DEFAULT
        ca_path = cfg.get("ca_path") or None
        sock_timeout = timeout or config.VOICE_REQUEST_TIMEOUT_SEC
        headers = {"Authorization": "Bearer " + api_key}
        return endpoint, model, self.sample_rate(cfg), ca_path, sock_timeout, headers

    @staticmethod
    def _connect(endpoint, headers, sock_timeout, ca_path):
        try:
            host, port, path = parse_ws_endpoint(endpoint)
            return ws_connect(host, port, path, headers, timeout=sock_timeout, ca_path=ca_path)
        except (OSError, WSError) as e:
            raise TTSWSError("connect failed: {}".format(e))

    def connect(self, cfg, timeout=None):
        """Open the WebSocket before the text is known. Returns WSConnection.

        The handshake costs 590-630ms on device, all of which used to land
        between "reply ready" and "sound out of the speaker". Opening it while
        the LLM is still thinking hides it entirely; the caller must then pass
        the connection to :meth:`synthesize` or close it.
        """
        endpoint, _, _, ca_path, sock_timeout, headers = self._session_args(cfg, timeout)
        return self._connect(endpoint, headers, sock_timeout, ca_path)

    def synthesize(self, cfg, text, sink=None, timeout=None, ws=None):
        """TTS via WebSocket: text -> PCM bytes.

        cfg keys:
          - tts_ws_host: full endpoint URL
            (e.g. 'wss://dashscope.aliyuncs.com/api-ws/v1/inference')
          - tts_ws_api_key: API key for Authorization header
          - tts_ws_model: Sambert voice model (default: sambert-zhiying-v1;
            the model name selects the voice)
          - tts_ws_sample_rate: output sample rate (default: 16000)
          - ca_path: optional CA cert path for TLS

        Returns PCM bytes (signed 16-bit LE, mono). If ``sink`` is given, PCM
        chunks are passed to ``sink(chunk)`` as they arrive and the total
        byte count is returned (memory stays bounded regardless of length).
        ``ws`` may be a connection already opened by :meth:`connect`, whose
        handshake then no longer shows up in the ``connect=`` timing.
        """
        if not text:
            return 0 if sink is not None else b""
        endpoint, model, sample_rate, ca_path, sock_timeout, headers = self._session_args(
            cfg, timeout
        )

        t_open = time.ticks_ms()
        if ws is None:
            ws = self._connect(endpoint, headers, sock_timeout, ca_path)
        t_connect = time.ticks_ms()

        try:
            ws.send_text(_run_task_msg(make_task_id(), model, sample_rate, text))

            pcm_chunks = [] if sink is None else None
            total = 0
            t_first = 0
            while True:
                opcode, data = ws.recv()
                if opcode == _WS_BINARY:
                    if not t_first:
                        t_first = time.ticks_ms()
                    total += len(data)
                    if sink is None:
                        pcm_chunks.append(data)
                    else:
                        sink(data)
                    continue
                msg = json.loads(data)
                hdr = msg.get("header", {})
                event = hdr.get("event", "")
                if event == "task-finished":
                    break
                if event == "task-failed":
                    raise TTSWSError(
                        "task-failed: {} {}".format(
                            hdr.get("error_code", ""), hdr.get("error_message", "")
                        )
                    )
                # task-started / result-generated carry no audio.

            # first_audio is the synthesis latency the listener actually waits
            # through; stream is the transfer the prebuffer rides out.
            log.timing(
                "TTS",
                "chars={} connect={}ms first_audio={}ms stream={}ms pcm={}ms total={}ms".format(
                    len(text),
                    time.ticks_diff(t_connect, t_open),
                    time.ticks_diff(t_first, t_connect) if t_first else -1,
                    ms_since(t_first) if t_first else 0,
                    total * 1000 // (sample_rate * 2),
                    ms_since(t_open),
                ),
            )
            if sink is not None:
                return total
            return b"".join(pcm_chunks)
        except WSError as e:
            raise TTSWSError(str(e))
        finally:
            ws.close()
