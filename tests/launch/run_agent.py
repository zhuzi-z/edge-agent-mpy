"""Run the full Edge Agent on the MicroPython unix port.

Shared entry for local development (`make unix-dev`, with the audio bridge
and fake LLM server) and the app-flow e2e test (tests/e2e/appflow_runner.py).
Audio hardware is replaced with TCP socket stubs (tests/stubs/audio_hal.py):
an external process streams PCM into the mic port and reads the speaker port.

Usage:
    make unix-dev
    # or directly:
    micropython tests/launch/run_agent.py --http-port 8080 --mic-port 19301 \
        --spk-port 19302 --wake-port 19303 --fake-port 19100 --seed-config
"""

import sys
import os

ROOT = os.getcwd()
sys.path.insert(0, ROOT + "/src")
sys.path.insert(0, ROOT + "/tests/stubs")
sys.path.insert(0, ROOT + "/tests")

import compat  # noqa: F401

import app.config as config

import argparse

_parser = argparse.ArgumentParser(description="Run Edge Agent on the MicroPython unix port")
_parser.add_argument("--http-port", type=int, default=8080, help="WebUI HTTP port")
_parser.add_argument("--mic-port", type=int, default=19301, help="Microphone TCP port")
_parser.add_argument("--spk-port", type=int, default=19302, help="Speaker TCP port")
_parser.add_argument("--wake-port", type=int, default=19303, help="Wake word trigger port")
_parser.add_argument(
    "--fake-port", type=int, default=19100, help="Fake server port (--seed-config)"
)
_parser.add_argument(
    "--data-dir", default="tmp/unix-dev-data", help="State dir, relative to repo root"
)
_parser.add_argument(
    "--seed-config",
    action="store_true",
    help="Point missing LLM/ASR/TTS config at the fake server",
)
_parser.add_argument(
    "--no-pace", action="store_true", help="Speaker writes skip audio-duration pacing (e2e)"
)
_parser.add_argument(
    "--fast-timers", action="store_true", help="Shorten listen/tail/follow-up windows (e2e)"
)
_args = _parser.parse_args()

BASE = ROOT + "/" + _args.data_dir

config.HTTP_PORT = _args.http_port
config.AGENT_CONFIG_PATH = BASE + "/agent.json"
config.WEIXIN_STATE_PATH = BASE + "/weixin.json"
config.DATA_DIR = BASE + "/"
config.SKILLS_DATA_DIR = BASE + "/skills/"
config.UPLOADED_SKILLS_PATH = BASE + "/uploaded"
config.WIFI_CONFIG_PATH = BASE + "/wifi.json"
# The WebUI and the builtin skills now come from config.APP_ROOT (the package
# directory), which already points into the sources here.  The OTA paths do
# not: an update *renames* the app directory, so keep it inside the sandbox and
# never on top of src/app.
config.OTA_STATE_PATH = BASE + "/ota.json"
config.OTA_BUNDLE_PATH = BASE + "/ota.tar"
config.OTA_APP_DIR = BASE + "/app"
config.OTA_PRE_DIR = BASE + "/pre"
# The voice thread runs the whole ASR/LLM/skill pipeline, whose DNS lookups
# go through glibc's resolver on the unix port -- 24 KB (fine on the ESP32)
# overflows it and segfaults silently.
config.VOICE_THREAD_STACK_SIZE = 256 * 1024
config.BACKGROUND_THREAD_STACK_SIZE = 256 * 1024
# The AFE capture path needs the real ESP-SR front end; here the mic is a TCP
# socket and esp_sr is a stub with nothing behind it, so the channel must keep
# reading that socket itself (energy VAD). Unit tests drive the AFE path.
config.VOICE_AFE_VAD = False
if _args.fast_timers:
    config.VOICE_LISTEN_TIMEOUT_MS = 2000
    # Shorten the post-playback mute tail and the follow-up window so the
    # e2e waits (wall-clock) stay short; real hardware keeps the defaults.
    config.VOICE_PLAYBACK_TAIL_MS = 200
    config.VOICE_FOLLOWUP_TIMEOUT_MS = 1200

for d in ("", "/skills", "/uploaded", "/sessions", "/memory"):
    try:
        os.mkdir(BASE + d)
    except OSError:
        pass

import json as _json

try:
    with open(config.WIFI_CONFIG_PATH, "r") as _f:
        _json.load(_f)
except (OSError, ValueError):
    with open(config.WIFI_CONFIG_PATH, "w") as _f:
        _json.dump({"ssid": "local-voice", "password": ""}, _f)

if _args.seed_config:
    try:
        with open(config.AGENT_CONFIG_PATH, "r") as _f:
            _cfg = _json.load(_f)
    except (OSError, ValueError):
        _cfg = {}
    _dirty = False
    if not _cfg.get("asr_ws_host"):
        _cfg["asr_ws_host"] = "ws://127.0.0.1:{}/api-ws/v1/inference".format(_args.fake_port)
        _cfg["asr_ws_api_key"] = "sk-local"
        _cfg["asr_ws_model"] = "paraformer-v2"
        _dirty = True
    if not _cfg.get("tts_ws_host"):
        _cfg["tts_ws_host"] = "ws://127.0.0.1:{}/api-ws/v1/inference".format(_args.fake_port)
        _cfg["tts_ws_api_key"] = "sk-local"
        _cfg["tts_ws_model"] = "sambert-zhiying-v1"
        _cfg["tts_ws_sample_rate"] = 16000
        _dirty = True
    if not _cfg.get("base_url"):
        _cfg["base_url"] = "https://127.0.0.1:{}".format(_args.fake_port)
        _cfg["api_key"] = "sk-local"
        _cfg["model"] = "fake-model"
        _dirty = True
    if _dirty:
        with open(config.AGENT_CONFIG_PATH, "w") as _f:
            _json.dump(_cfg, _f)
        print("[launch] Config updated with local voice server (port {})".format(_args.fake_port))

# Replace I2S hardware with socket-based mocks
import app.audio.io as _audio_io
import audio_hal as _hal


class _SocketInput(_hal.AudioInput):
    def __init__(self, **kwargs):
        super().__init__(port=_args.mic_port, rate=config.VOICE_SAMPLE_RATE)


class _SocketOutput(_hal.AudioOutput):
    def __init__(self, **kwargs):
        super().__init__(
            port=_args.spk_port, rate=config.VOICE_SAMPLE_RATE, pace=not _args.no_pace
        )


_audio_io.AudioInput = _SocketInput
_audio_io.AudioOutput = _SocketOutput

# Start esp_sr wake word trigger server (the bridge/runner sends wakeup events here)
import esp_sr

esp_sr.start_trigger_server(_args.wake_port)

print(
    "[launch] http={}, mic={}, spk={}, wake={}, data={}".format(
        _args.http_port, _args.mic_port, _args.spk_port, _args.wake_port, _args.data_dir
    )
)

from app.main import main

try:
    main()
except KeyboardInterrupt:
    print("[launch] stopped")
