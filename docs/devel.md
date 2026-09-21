# Developer Guide

Working on the Edge Agent codebase: repo layout, running locally, testing,
deploying, and code style. For the skill format see [skill.md](skill.md); for
firmware builds see [../firmware/README.md](../firmware/README.md).

## Repository Layout

```
src/main.py              ESP32 entry point + OTA recovery layer (device only)
src/app/                 Application package - all of it lands in /app
  main.py                Boot flow: wifi, skills, agent, HTTP server, channels
  agent.py               LLM agent (chat + tool-calling loop)
  skills.py              Skill registry: discover/exec/persist skills
  control.py             Device config plane (agent.json) + control ops
  ota.py                 Update install + the /app vs /pre boot state
  tar.py                 Read-only ustar reader for the update bundles
  bus.py                 Channel <-> agent message bus
  channels/              voice.py, web.py, weixin.py
  providers/             LLM/ASR/TTS backends (openai_compat, dashscope)
  api/server.py          asyncio HTTP server + routes
  builtin_skills/        gpio, http_get, web_search, voice_control
  web/index.html         WebUI (single file, served from the app directory)
  storage/               Config/chat/memory persistence
skills/                  Uploadable skills (e.g. mijia) + upload.py
firmware/                Custom MicroPython firmware (ESP-SR)
tests/                   ut (MicroPython unix port), e2e app-flow
```

`src/main.py` sits outside the package on purpose: it is what decides whether
`/app` or the kept-back `/pre/app` runs, and an update never carries it. It
imports nothing from `app` — see [ota.md](ota.md).

## Prerequisites

```bash
python -m venv .venv
.venv/bin/python -m pip install piper-tts faster-whisper

# Needed for `make e2e` and `make unix-dev` (the fake server runs local ASR/TTS):
# TTS model (English)
.venv/bin/python -m piper.download_voices en_US-lessac-low \
  --data-dir ~/.local/share/piper
# ASR model (auto-cached on first use)
HF_HUB_OFFLINE=0 .venv/bin/python -c \
  "from faster_whisper import WhisperModel; WhisperModel('tiny')"
```

Unit tests additionally need the **MicroPython unix port** binary
(`micropython`) on PATH.

## Running Locally

```bash
make unix-dev
```

Runs the full agent on the MicroPython unix port with real host audio and
the WebUI at `http://127.0.0.1:8080/`. It starts three processes:

| Process | Runtime | Role |
|---------|---------|------|
| `tests/fake_llm_server.py` | CPython (`.venv`) | Local backend: echo LLM + real ASR (faster-whisper) + real TTS (piper) over TLS |
| `tests/launch/audio_bridge.py` | CPython (`.venv`) | Host mic/speaker + wake word detection, streams PCM over TCP |
| `tests/launch/run_agent.py` | MicroPython unix | The agent itself (`--seed-config` points LLM/ASR/TTS at the fake server) |

Hardware modules are shimmed by `tests/stubs/`; state lives in
`tmp/unix-dev-data/`.

### Runtime Notes

- Outbound TLS is unverified by default for trusted-LAN/self-signed services;
  `ca_path` opts that endpoint into CA verification.
- The synchronous and asynchronous HTTP clients each keep one keep-alive
  connection for the configured idle period.
- Async agent tool calls and slow memory consolidation run in short-lived
  worker threads so a skill cannot stop HTTP/WebUI handling.
- `/info` reports the pins named by `gpio_status_pins` (default: GPIO 38).
  The GPIO skill itself can still address any valid pin.
- The device enables a 30-second hardware watchdog after boot. The main loop
  feeds it on every maintenance pass; a stopped loop reboots the agent.

### Dependencies

Besides the base prerequisites (MicroPython unix binary; `.venv` with
piper-tts + faster-whisper and their models), the audio bridge needs:

```bash
.venv/bin/python -m pip install sounddevice numpy openwakeword
```

`sounddevice` requires the PortAudio system library (`libportaudio2` on
Debian/Ubuntu). `openwakeword` ships its pretrained models with the package.

### Wake Word

The on-device wake words ("Hi ESP" / "Hi 乐鑫") come from ESP-SR and only
run on the ESP32. In `unix-dev`, wake detection runs **host-side** via
openwakeword in the audio bridge — default model is `alexa`, so say
"Alexa" to start a conversation. To use another model/threshold, run the
bridge manually instead of `make unix-dev`:

```bash
.venv/bin/python tests/launch/audio_bridge.py --model hey_jarvis --threshold 0.5
```

Detection is gated while TTS playback is audible (half-duplex, no AEC —
mirrors the device behavior). After the agent's reply plays out, a follow-up
window listens without needing the wake word again.

### AFE VAD (device only)

The AFE cannot share the I2S mic, so on the device the channel never opens
`AudioInput` while listening: it keeps the detector running, takes the
utterance PCM from `esp_sr.read()` and the endpoints from the AFE's VAD events
(`esp_sr.capture()`), gated by `config.VOICE_AFE_VAD`. The firmware buffers
only the segment — its VAD pre-roll first, then frames until its
trailing-silence window expires — and a listen that sees zero AFE frames
latches the path off and falls back to the energy VAD.

Both launchers set `VOICE_AFE_VAD = False`: there the mic is a TCP socket and
the `esp_sr` stub has no front-end behind it. `tests/ut/test_wakeword.py`
covers the AFE path against the stub's capture model.

## Testing

All tests run on the **MicroPython unix port** — no ESP32 hardware required.
Hardware peripherals (I2S, WiFi, TLS) are replaced by socket-based stubs in
`tests/stubs/`. Test code uses only MicroPython-native APIs (`_thread`,
`os`), never CPython stdlib like `threading`/`tempfile`/`shutil`.

```bash
make ut      # unit tests
make e2e     # app-flow integration test
make test    # ut + e2e
```

### App-Flow Test (`make e2e`)

Launches the **full** Edge Agent process + a local fake backend, then
exercises real data links end-to-end. Principle: run the complete `main()`
flow; only intercept **hardware** and **configuration** — never patch network
or business logic.

| Layer | Real | Mocked |
|-------|------|--------|
| Voice I/O | VAD, full pipeline | I2S → TCP sockets (`tests/stubs/audio_hal.py`) |
| ASR | faster-whisper `tiny` | — |
| LLM | — | Echo server (`tests/fake_llm_server.py`) |
| TTS | piper `en_US-lessac-low` | — |
| Network | Real TLS to local fake server | — |
| Config | Pre-written `tmp/appflow/agent.json` | — |

Voice channel flow: piper synthesizes "who are you?" → PCM streamed into the
mock mic socket → VAD detects speech → faster-whisper ASR → fake LLM replies
→ piper TTS → PCM read from the mock speaker socket → assertions. The
orchestrator is `tests/e2e/appflow_runner.py` (CPython).

**Environment variables (optional):**

| Variable | Default | Description |
|----------|---------|-------------|
| `MICROPY` | `micropython` | Path to MicroPython unix binary |
| `PIPER_MODEL` | `~/.local/share/piper/en_US-lessac-low.onnx` | Piper ONNX model path |
| `WHISPER_MODEL` | `tiny` | faster-whisper model size |
| `HF_HUB_OFFLINE` | `1` (in Makefile) | Use cached whisper model without network |

Keep tests minimal: merge related single-assertion cases into one test
method; aim for ~10 cases per module at most.

## Deployment

```bash
make deploy       # copy src/ to the device via mpremote, then reset
make flash-repl   # deploy + open serial REPL for live log inspection
make repl         # serial REPL only
make diag         # flash & run audio diagnostic (tests/board/audio_diag.py)
make diag-clean   # remove it again
make ota          # build firmware/edge-agent-mpy-ota-<agent>.tar for POST /ota
make ota-push     # ...and push it to OTA_HOST=<ip>, waiting for the reboot
```

`DEVICE` selects the serial port (default: first `/dev/ttyACM*`).

`make deploy` is the developer path, and the only way to push `main.py` to a
device that is already running. It overwrites `main.py` and `app/` and nothing
else, so `/config` (WiFi credentials, agent settings, per-skill data), `/data`
(chat sessions and memory) and `/skills` (uploaded skills) survive it — which is
exactly what makes it the right tool for iterating: restart the agent, keep the
state.

`make ota` is the same app without the cable: a tar of everything but
`src/main.py`, uploaded to `POST /ota` from the WebUI's **Status → Firmware
Update** card or pushed by `make ota-push`. The device keeps the app it replaced
in `/pre/app` and boots that one back if the new app does not come up, so an
update cannot brick it. `OTA_SKILLS=mijia` adds a skill folder to the bundle.
The mechanics, the state file and what an update deliberately does not cover are
in [ota.md](ota.md).

`app-fs.bin` is the other half, for users without a toolchain: the same file
set as a littlefs image of the whole `vfs` partition, flashed from the browser
at `0x300000` next to the firmware. A device that only ever got flashed from
the browser otherwise has no way to receive the app at all.

The two are not interchangeable: `app-fs.bin` **replaces** the partition, so it
is a first-install / factory-reset artifact and never an updater. What survives
such a wipe is only what the user exported beforehand: the Config page's
*Backup & Restore* card (`GET /config/export`, `POST /config/import`) writes the
whole `agent.json` - API keys included - to a file and merges a file back into
it.

```bash
make app-fs          # build firmware/app-fs.bin (also verifies it)
make app-fs-check    # mount an existing image and compare it with the sources
make factory-image   # build firmware/edge-agent-mpy-v<agent>-mpy<mpy>.bin (all three)
```

The release image is what a user without a toolchain flashes:
bootloader, partition table, MicroPython, the app and the wake-word models in
one file, at one offset, so there are no three offsets to mistype. The page that
writes it is `tools/web-flasher.html` — Web Serial plus esptool-js from a CDN,
opened straight off the disk, with the release image and the three per-partition
images pinned to their offsets. It cannot reset the chip over the S3's native
USB port, so it asks the user to enter download mode by hand and to press RESET
once the write is done. It is bilingual, and the convention is worth keeping
when adding to it: every string the page shows is a key in the `I18N` table at
the top of its script, the markup points at that key with `data-i18n` (text),
`data-i18n-html` (text with inline markup) or `data-i18n-attr="attr:key"`
(attributes), and the script reads it through `t(key, params)`. `{name}`
placeholders are filled in after the lookup instead of by concatenation, because
the two languages do not share word order. Text the script has already put on
screen — the status line, the connection pill, the image rows — is redrawn from
the remembered key rather than left in the old language. The choice follows the
browser and is then kept in `localStorage` next to the theme preference; a
string added outside the table shows up in one language only. Both halves of the
image name are derived, never typed:
`<agent>` is `git describe` of the checkout (the tag when HEAD sits exactly on
one, `v1.3.0-4-g79eac03` when it does not) and `<mpy>` the MicroPython version
the firmware and model images carry. `make factory-image` depends on `app-fs`,
re-checks that image against the sources before merging, and takes the other two
parts from `firmware/`, where `firmware/build.sh` copies them under their release
names - so a merge runs right after a build with nothing to rename. It refuses a
pair whose names do not carry the same MicroPython version and aborts if a part
overruns its partition. The merge only lays the three parts into a 16 MiB field
of 0xFF, so the same sources give the same sha256.

To add or drop a deployed file, edit the `DEPLOYED` list in
`firmware/make_app_fs.py` only: every entry keeps its own last path component on
the device (`src/app` lands as `/app`), and both the `deploy` target above (via
`--print-mpremote`) and the factory image are built from that one list.
`make app-fs` reads its own output back through the host MicroPython
(which is what formats the volume), so a broken image is never written out;
`make app-fs-check` fails when a published image no longer matches the sources.
See [firmware/README.md](../firmware/README.md) for why the volume is littlefs
and which geometry constants that check covers.

## Code Style

```bash
make fmt    # ruff format src/ tests/ skills/
make lint   # ruff check src/ tests/ skills/
```

Type stubs for IDE support:

```bash
pip install -t typings micropython-esp32-stubs micropython-stdlib-stubs
```

### Web pages

Both browser pages — `src/app/web/index.html` (the WebUI) and
`tools/web-flasher.html` — are bilingual, English and Chinese, and both work the
same way. Every string a page shows is a key into an `I18N` table near the top of
its script; the markup points at a key with `data-i18n` (text), `data-i18n-html`
(text with inline markup) or `data-i18n-attr="attr:key;…"`, and the script reads
it through `t(key, params)`. `{name}` placeholders are filled in after the
lookup instead of by concatenation, because the two languages do not share word
order. A key that is not in the table comes back unchanged, which is what lets
text arriving from the device — an API error, a heap region name, a skill
description — pass straight through.

The language follows the browser on a first visit and is then kept in
`localStorage` under `esp32-ui.lang`, beside the theme preference; an inline
script in `<head>` sets `<html lang>` before the first paint. Two rules keep a
switch from leaving stale copy behind:

- Text the script has already put on screen is redrawn from the remembered key
  and parameters, never left as the string it happened to be rendered with.
- A panel built from a device response keeps that response (`lastInfo`,
  `lastChannels`, `lastSkills`, `lastOtaInfo`) and renders from it, so switching
  language redraws the panel without asking the device again — it may be out of
  reach at that moment.

Two things are deliberately not translated: config field names (`base_url`,
`asr_ws_host`) and slash-command names, because they are literals the device uses
too; and anything the device generates — `/help` output, agent replies, API error
strings. Translating those is a server-side change, not a WebUI one.

The WebUI is served whole from flash and cached in RAM by `route_index`
(`src/app/api/server.py`), so the table is part of what the device holds: adding
a language costs file size, not just one more request.
