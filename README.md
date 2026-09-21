# Edge Agent (MicroPython + ESP32-S3)

A voice-first edge AI agent for the ESP32-S3, written in MicroPython. Say the
wake word and talk to it like a smart speaker; it understands you through
cloud ASR, thinks with an LLM, acts with tools (GPIO, web search, Mijia smart
home, ...), and answers out loud. It also ships a browser WebUI for chat and
settings.

## Read This First

- **Why** — agents on ESP32 are nothing new; this one exists for fast
  iteration and *vibe coding*. Being MicroPython, the whole app can run and
  be fully tested on a unix port without hardware, and any change redeploys
  in seconds. Raw performance is explicitly not the goal.
- **Scope** — Edge Agent is for controlling edge devices: conversation,
  GPIO/peripherals, and simple on-device tool calls. It is a hobby project,
  not production-ready, and it does not try to match general-purpose agents —
  document parsing, complex task automation, and vision are out of scope.
- **Security** — deploy only inside a trusted LAN. The WebUI and HTTP API
  have **no authentication**: anyone on the network can operate the device
  and read its state. Skills run as unsandboxed Python, so a skill can drive
  any hardware and read everything stored on the device (WiFi credentials,
  account tokens, chat history). Only install external skills you trust.
  The config backup the WebUI can export holds your LLM/ASR/TTS API keys in
  plain text, so treat that file like a password.
  Outbound HTTPS/WebSocket traffic is encrypted but not certificate-verified
  by default, matching trusted-LAN and self-signed services; set `ca_path`
  in the agent config when verification is needed.

## Features

- **Voice conversation** — wake word ("Hi ESP" / "Hi 乐鑫") detection with
  ESP-SR on-device, then full-duplex-style talk: cloud ASR → LLM → streaming
  TTS. Follow-up utterances need no wake word; say goodbye and it returns to
  standby. Volume can be changed by voice.
- **WebUI** — built-in web page at `http://<device-ip>/` for chatting,
  settings (LLM/ASR/TTS providers, channels, volume), GPIO panel, skill and
  memory management, plus config backup and restore (export/import a JSON
  file, so settings survive a reflash or move to another board). English and
  Chinese: it follows your browser's language, and the button at the bottom of
  the sidebar switches.
- **Tool use** — the LLM gets function-calling skills: GPIO control, HTTPS
  requests, web search, voice-session control, and more.
- **Extensible via skills** — upload new Python skills at runtime, no
  reflash needed. See [docs/skill.md](docs/skill.md).
- **Channels** — web chat and voice are built in; a WeChat text channel
  (iLink) can be enabled from the WebUI.
- **WiFi provisioning** — first boot opens an access point (`EdgeAgent-Setup`)
  with a captive portal; pick a network, enter the password, done. Reopens
  automatically if the saved network keeps failing.
- **Memory** — when a conversation ends, the agent folds what it learned
  into one long-term memory file on device, and reads that back into every
  later conversation.
- **Over-the-air update** — upload a bundle from the WebUI; the device keeps
  the app it replaced and boots that one back if the new app does not start,
  so an update cannot leave it unreachable. See [docs/ota.md](docs/ota.md).

## Hardware

- ESP32-S3 board with **16 MiB flash + 8 MiB octal PSRAM** (N16R8)
- I2S MEMS microphone + I2S amplifier/speaker, wired as on the
  xiaozhi *bread-compact-wifi* board (mic SCK/WS/SD = GPIO 5/4/6,
  speaker SCK/WS/SD = GPIO 15/16/7; on-board LED on GPIO 38)

## Quick Start

### 1. Flash the firmware

Download the newest `edge-agent-mpy-v<agent>-mpy<mpy>.bin` from this
repository's **Releases** page — one file with everything in it: the custom
MicroPython v1.29.0 firmware (ESP-SR wake word), the agent app and the wake-word
models. The name says what is inside: `<agent>` is the edge-agent version (its
git tag, plus `-<n>-g<commit>` for a build that is not exactly a tag) and `<mpy>`
the MicroPython version it runs on.

Flash it in the browser with [`tools/web-flasher.html`](tools/web-flasher.html)
(Chrome or Edge): opening the file from disk is enough, no server and no
toolchain. It follows your browser's language, Chinese or English, and the
button at the top right switches it (the labels below are the English ones).
Choose the image, press **Connect & Detect** — the page says how to put the
board into download mode — then **Start Flashing**. The offset `0x0` and the
16 MB flash size are fixed, the read/write mode comes from the image itself, and
when the write finishes the page points you at the device's provisioning hotspot.
From a terminal instead:

```bash
pip install esptool   # once
python -m esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 \
    --flash_mode dio --flash_freq 80m --flash_size 16MB \
    write_flash --compress 0x0 edge-agent-mpy-v<agent>-mpy<mpy>.bin
```

The board then boots straight into the provisioning page, so steps 2 and 3
below can be skipped. The image is a **factory restore**: it covers the whole
flash, so on a device that is already in use it also wipes WiFi credentials,
settings, chat memory and uploaded skills — export a config backup from the
WebUI first if you want to keep your settings. `make factory-image` rebuilds
it from the current sources.

Developers can also flash the three images it is made of separately, to update
one layer at a time (or to avoid the wipe) — the same page lists them under
**Per-partition images (advanced)**, and
[firmware/README.md](firmware/README.md) has the offsets, port names and the
BOOT-button trick for esptool.

### 2. Deploy the agent code

```bash
make deploy                      # auto-detects /dev/ttyACM*
make deploy DEVICE=/dev/ttyUSB0  # or pick the port explicitly
```

This copies `src/main.py` and `src/app/` (the WebUI and the builtin skills are
inside it) to the device and resets it. Only those two paths are touched:
`/config`, `/data` and uploaded skills in `/skills` stay as they are, which is
why this - not reflashing the release image - is how a device you already use
gets updates.

No cable? The same app goes over the network instead: `make ota` builds a
bundle you upload from **Status → Firmware Update**, and `make ota-push
OTA_HOST=<ip>` does it from the terminal. The device keeps the app it replaced
and boots that one back if the new one does not start — see
[docs/ota.md](docs/ota.md).

### 3. Connect WiFi

On first boot the device opens the `EdgeAgent-Setup` access point. Join it
with your phone/laptop, open `http://192.168.4.1/`, pick your WiFi network
and enter the password. The portal then shows the device's IP address.

Forgot the IP later? Open `src/app/web/index.html` directly in a browser (works
offline) and use **Scan**: type your WiFi subnet into the address field,
then press Scan — the page probes that /24 and connects to the device.
The subnet is the first three numbers of your phone/laptop's own WiFi IP
(e.g. your phone shows `192.168.2.53` in WiFi settings → type
`192.168.2.`).

### 4. Configure AI providers

Open `http://<device-ip>/` in a browser, go to settings, and enter your LLM
endpoint (any OpenAI-compatible API: base URL / API key / model) plus ASR/TTS
credentials (DashScope by default). Settings are stored on the device.

Once it all works, press **Export** in *Backup & Restore* at the bottom of the
Config page: it downloads every setting as one JSON file. **Import** on the
same page writes such a file back (WiFi credentials are not part of it), which
is how a device recovers its settings after a factory-image reflash.

### 5. Talk

Say the wake word ("Hi ESP" or "Hi 乐鑫"), wait for the tone, then speak.
Just talk: ask questions, "turn on the LED on gpio38", "search the web
for ...", or "turn the volume down". Say goodbye (or "退出对话") to end the
session.

## Skills

Skills are small Python modules the LLM can call as tools. Built-in skills:
`gpio`, `http_get`, `web_search`, `voice_control`. You can upload your own at
runtime (e.g. the bundled `mijia` skill for Xiaomi smart home) — the format
and APIs are specified in [docs/skill.md](docs/skill.md).

## Documentation

- [docs/devel.md](docs/devel.md) — developer guide: testing, local run,
  deployment, code style
- [docs/ota.md](docs/ota.md) — over-the-air update: bundles, fallback boot, API
- [docs/skill.md](docs/skill.md) — skill specification
- [firmware/README.md](firmware/README.md) — firmware build & flash

## Acknowledgements

This project builds on the ideas and code of great open-source projects:

- [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — the XiaoZhi AI
  chatbot for ESP32
- [nanobot](https://github.com/HKUDS/nanobot) — a self-hosted personal AI
  agent runtime
- [esp-claw](https://github.com/espressif/esp-claw) — Espressif's Chat
  Coding AI agent framework for IoT devices
