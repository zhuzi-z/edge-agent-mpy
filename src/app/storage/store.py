"""Flash-backed JSON config persistence."""

import json
import app.config as config
import app.log as log
from app.util import ensure_parent_dir


class Store:
    """Read/write agent config on flash."""

    def __init__(self, cfg_path=config.AGENT_CONFIG_PATH):
        self._cfg_path = cfg_path

    def load_config(self):
        """Load config, merging with defaults for missing keys."""
        try:
            with open(self._cfg_path, "r") as f:
                data = json.load(f)
            self._migrate(data)
            defaults = self._default_config()
            defaults.update(data)
            return defaults
        except (OSError, ValueError):
            return self._default_config()

    @staticmethod
    def _migrate(data):
        """In-place fixups for configs written by older firmware."""
        # The qwen3 realtime TTS provider was removed; Sambert is the only
        # one left. tts_ws_voice only applied to the removed provider.
        if data.get("tts_provider") in ("dashscope_realtime", "dashscope_nonrealtime"):
            data["tts_provider"] = "dashscope"
            data.pop("tts_ws_voice", None)
        # Qwen-TTS model names are invalid on the Sambert endpoint; fall
        # back to the default sambert voice (the model name picks it).
        if str(data.get("tts_ws_model") or "").startswith(("qwen3-tts", "qwen-tts")):
            data["tts_ws_model"] = ""
        # Same DashScope host serves both WS endpoints; only the path moves.
        host = data.get("tts_ws_host") or ""
        if host.endswith("/api-ws/v1/realtime"):
            data["tts_ws_host"] = host[: -len("/api-ws/v1/realtime")] + "/api-ws/v1/inference"

    def save_config(self, data):
        try:
            ensure_parent_dir(self._cfg_path)
            with open(self._cfg_path, "w") as f:
                json.dump(data, f)
            return True
        except OSError as e:
            log.error("Store", "save config failed: {}".format(e))
            return False

    @staticmethod
    def _default_config():
        return {
            "base_url": "",
            "api_key": "",
            "model": "",
            "ca_path": "",
            "asr_ws_host": "",
            "asr_ws_api_key": "",
            "asr_ws_model": "",
            "tts_ws_host": "",
            "tts_ws_api_key": "",
            "tts_ws_model": "",
            "tts_ws_sample_rate": "",
            "voice_enabled": True,
            "channels": {"web": True, "voice": True},
        }
