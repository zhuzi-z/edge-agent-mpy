"""Device control plane: owns the device config, operated by agent and skills.

Loads the live config dict (agent.json) once from the Store; every
consumer reads it through this object — the Agent for LLM settings,
the voice channel and bus for volume/session settings, skills via the
registry. All writes go through update(), which validates/clamps and
persists, so the agent itself never mutates config.
"""

import json
import time

import app.config as config

# Config keys writable through update() (WebUI /config POST, skills).
_CONFIG_KEYS = (
    "base_url",
    "api_key",
    "model",
    "ca_path",
    "asr_ws_host",
    "asr_ws_api_key",
    "asr_ws_model",
    "tts_ws_host",
    "tts_ws_api_key",
    "tts_ws_model",
    "tts_ws_sample_rate",
    "llm_provider",
    "asr_provider",
    "tts_provider",
    "voice_enabled",
    "volume",
    "max_tool_rounds",
    "session_timeout_min",
    "gpio_status_pins",
    "reasoning_effort",
    "system_prompt",
    "channels",
)

# Secrets, masked by masked_config() and skipped by parse_backup() so a file
# built from the display view cannot overwrite a real key with its own mask.
_SECRET_KEYS = ("api_key", "asr_ws_api_key", "tts_ws_api_key")
_SECRET_MASK = "***"


class DeviceControl:
    """Owns the device config and exposes control operations."""

    def __init__(self, store):
        self._store = store
        self._cfg = store.load_config()

    def config(self):
        """The live config dict (shared with voice channel/bus)."""
        return self._cfg

    def channel_config(self):
        """Return channel enable/disable map."""
        return self._cfg.get("channels") or {"web": True, "voice": True}

    def gpio_status_pins(self):
        """Pins reported by the periodic status endpoint."""
        pins = self._cfg.get("gpio_status_pins")
        if isinstance(pins, (list, tuple)) and pins:
            return tuple(pins)
        return config.GPIO_STATUS_PINS

    def update(self, data):
        """Merge config fields, clamp known ranges, persist."""
        for k in _CONFIG_KEYS:
            if k in data:
                self._cfg[k] = data[k]
        if "volume" in self._cfg:
            try:
                vol = int(self._cfg["volume"])
            except (TypeError, ValueError):
                vol = config.SPEAKER_VOLUME_DEFAULT
            self._cfg["volume"] = max(0, min(100, vol))
        if "session_timeout_min" in self._cfg:
            try:
                self._cfg["session_timeout_min"] = max(0, int(self._cfg["session_timeout_min"]))
            except (TypeError, ValueError):
                del self._cfg["session_timeout_min"]
        if "reasoning_effort" in self._cfg:
            try:
                effort = self._cfg["reasoning_effort"].strip()
            except AttributeError:
                effort = ""
            if effort:
                self._cfg["reasoning_effort"] = effort
            else:
                del self._cfg["reasoning_effort"]
        if "gpio_status_pins" in self._cfg:
            pins = self._cfg["gpio_status_pins"]
            if isinstance(pins, (list, tuple)):
                try:
                    self._cfg["gpio_status_pins"] = tuple(
                        max(0, min(48, int(pin))) for pin in pins
                    )
                except (TypeError, ValueError):
                    self._cfg["gpio_status_pins"] = config.GPIO_STATUS_PINS
            else:
                self._cfg["gpio_status_pins"] = config.GPIO_STATUS_PINS
        self._store.save_config(self._cfg)

    def masked_config(self):
        """Config with API keys masked (for display)."""
        cfg = dict(self._cfg)
        for k in _SECRET_KEYS:
            if cfg.get(k):
                cfg[k] = _SECRET_MASK
        cfg["system_prompt_default"] = config.SYSTEM_PROMPT_DEFAULT
        return cfg

    # -- Backup / restore (WebUI export & import of agent.json) -------------

    def export_config(self):
        """Backup envelope holding the whole config, API keys in clear text.

        The JSON round-trip is the deep copy: nothing else keeps a reference
        into the returned structure, so a later settings write cannot reach
        into a file the browser is still holding.
        """
        return {
            "kind": config.CONFIG_BACKUP_KIND,
            "version": config.CONFIG_BACKUP_VERSION,
            "exported_at": int(time.time()),
            "config": json.loads(json.dumps(self._cfg)),
        }

    def parse_backup(self, data):
        """Validate a backup and pick out the settings it carries.

        Accepts the envelope export_config() writes or a bare settings dict
        (hand-edited file, partial restore). Unknown keys are dropped so a
        file from another firmware cannot inject junk, and nothing is written
        here: the caller hands the result to update(), which is the same
        clamp-and-persist path the settings form goes through.
        Returns (settings, error).
        """
        if not isinstance(data, dict):
            return None, "backup must be a JSON object"
        kind = data.get("kind")
        if kind is not None and kind != config.CONFIG_BACKUP_KIND:
            return None, "unsupported backup kind: {}".format(kind)
        payload = data.get("config", data)
        if not isinstance(payload, dict):
            return None, "backup carries no config object"
        channels = payload.get("channels")
        if channels is not None and not isinstance(channels, dict):
            return None, "channels must be an object"
        settings = {}
        for k in _CONFIG_KEYS:
            if k not in payload:
                continue
            if k in _SECRET_KEYS and payload[k] == _SECRET_MASK:
                continue
            settings[k] = payload[k]
        if not settings:
            return None, "backup carries no known settings"
        return settings, None

    # -- Control operations (used by skills via the registry) ---------------

    def get_volume(self):
        try:
            return int(self._cfg.get("volume", config.SPEAKER_VOLUME_DEFAULT))
        except (TypeError, ValueError):
            return config.SPEAKER_VOLUME_DEFAULT

    def set_volume(self, level):
        self.update({"volume": level})
        return self.get_volume()
