"""Configuration constants."""

# WiFi credentials file (saved by AP provisioning)
WIFI_CONFIG_PATH = "/config/wifi.json"

# WiFi defaults (used only if no saved config exists)
WIFI_SSID = ""
WIFI_PASS = ""

# AP provisioning
AP_SSID = "EdgeAgent-Setup"
AP_PASSWORD = ""
AP_TIMEOUT_SEC = 300
# After credentials are saved, keep the AP + portal up so the user's phone
# (still connected to it) can read the device's new IP from /status.
AP_POST_KEEPALIVE_SEC = 180
# ...and stay up this long after the STA got its IP (grace for the page).
AP_POST_GRACE_SEC = 15
# After a connect attempt starts, wait this long before trusting failure
# statuses from WLAN.status() (lets stale disconnect reasons clear).
AP_FAIL_DETECT_GRACE_SEC = 3
# Report a generic connect failure to the portal page if the STA still has
# not connected after this long (covers firmwares that never surface a
# terminal status code).
AP_CONNECT_TIMEOUT_SEC = 25
# Max number of networks returned by the portal's /scan endpoint.
AP_SCAN_MAX_RESULTS = 20

# Network
HTTP_PORT = 80

# WiFi retry
WIFI_RETRY_COUNT = 20
WIFI_RETRY_INTERVAL_SEC = 0.5
WIFI_MAX_CONNECT_FAILURES = 3

# HTTP server
HTTP_RECV_BUF_SIZE = 1024
HTTP_MAX_HEADER_SIZE = 4096
HTTP_CLIENT_TIMEOUT_SEC = 2.0
# Pending connections the listen socket queues. lwIP allows only
# CONFIG_LWIP_MAX_SOCKETS (10) in total, so this stays small - but 1 made the
# WebUI's parallel fetches on page load race each other for the single slot.
HTTP_SERVER_BACKLOG = 4
# A client that connects and then goes quiet (browser preconnect, LAN scanner)
# would otherwise hold its task and socket until the device ran out of them.
HTTP_CLIENT_READ_TIMEOUT_SEC = 10
# Upper bound on a request body. The largest legitimate one is a skill upload
# (POST /skills carries the module source inside JSON).
HTTP_MAX_BODY_SIZE = 256 * 1024
# A warm connection skips dns+tcp+tls on the next request to the same
# endpoint - measured 621ms on device, 514ms of it the mbedTLS handshake on
# the ESP32. Held for at most this long before being dropped, so a gateway
# that silently reaps idle connections is not handed a doomed request.
# Escape hatch: without CONFIG_MBEDTLS_DYNAMIC_BUFFER a warm TLS context pins
# its 16K in / 4K out record buffers for as long as it is cached, so set
# HTTP_KEEPALIVE False if the device turns out to be short of internal RAM.
HTTP_KEEPALIVE = True
HTTP_KEEPALIVE_IDLE_MS = 30000
MAIN_LOOP_SLEEP_MS = 10
# Cadence of the main loop's maintenance pass: recover a crashed audio thread,
# reclaim abandoned sessions, keep the wraparound-safe uptime counter fresh.
MAINTENANCE_INTERVAL_MS = 5000

# App package location
# Everything the app ships - code, WebUI, builtin skills - lives under its own
# package directory, so the path is derived from this file instead of being
# hardcoded.  That is what lets src/main.py run a kept-back copy of the whole
# app from /pre after a failed update: the fallback carries its own WebUI and
# builtin skills rather than pointing at the ones of the version it replaced.
# Only user data (/config, /data, /skills below) stays version-independent.
# (No os.path on device, hence the rsplit.)
APP_ROOT = __file__.rsplit("/", 1)[0]

# Web UI
WEB_UI_PATH = APP_ROOT + "/web/index.html"

# App update (OTA)
# The three paths below are also written as literals in src/main.py: that file
# is the recovery layer, it has to stay readable before the app is, so it does
# not import this module.  Keep the two in sync.
OTA_STATE_PATH = "/config/ota.json"
OTA_APP_DIR = "/app"
OTA_PRE_DIR = "/pre"
# Where an uploaded bundle is buffered while it is verified.  The tar is not
# compressed, so the Content-Length the client announces is the number of bytes
# that land here - checking it up front is enough, no free-space maths needed
# (the vfs partition is 12 MB, the app is ~400 KB).
OTA_BUNDLE_PATH = "/data/ota.tar"
OTA_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
# Per-read timeout while a bundle streams in: a 2 MB upload over WiFi takes far
# longer than the 10s the server gives an ordinary request, but a stalled client
# must not pin the socket forever either.
OTA_READ_TIMEOUT_SEC = 15
# Grace between "here is your answer" and the reboot, so the browser (and
# `make ota-push`) actually receive it before the device goes away.
OTA_REBOOT_DELAY_SEC = 1.0

# Diagnostics
# Per-stage latency breakdown of a voice turn (listen / ASR / LLM rounds / TTS
# / time to speaker), one log line per stage, all prefixed with "timing:".
# This is what tells you where the seconds actually go - the fixed costs
# (TLS handshakes, VAD tail silence, TTS prebuffer) dwarf the ones that scale
# with utterance length. Set False to silence it.
TIMING_LOG = True

# Agent / LLM
AGENT_CONFIG_PATH = "/config/agent.json"
# Config backup file (WebUI export/import). The envelope carries the whole
# agent.json, API keys included - that is what makes it a backup rather than
# a settings dump - so the export route is the one place keys leave the
# device in clear text. VERSION lets a later firmware recognise (and migrate)
# files written by an older one.
CONFIG_BACKUP_KIND = "edge-agent-config"
CONFIG_BACKUP_VERSION = 1
SYSTEM_PROMPT_DEFAULT = (
    "You are an assistant running on an ESP32 device. "
    "Use the available tools when the user asks for something a tool can do, "
    "then answer the user in their language."
)
# Appended to the system prompt for voice-channel requests (spoken aloud by TTS).
# Style adapted from xiaozhi-esp32-server's agent-base-prompt.
VOICE_PROMPT_SUFFIX = (
    "\n\nThis user is talking to you by voice, and your reply will be read aloud by TTS. "
    "Write like natural daily conversation: "
    "1. Get to the point in 1-2 short sentences, no preamble or pleasantries. "
    "2. Casual, warm, spoken tone, like a helpful friend; avoid robotic or written-style phrasing. "
    "3. Plain text only: no markdown, lists, emojis, or bracketed actions. "
    "4. The user's input comes from speech recognition and may contain homophone typos; "
    "infer the true intent and never correct their wording. "
    "5. Do not stack follow-up questions; at most one natural question when truly needed."
)
SKILLS_DATA_DIR = "/config/skills/"
DATA_DIR = "/data/"
BUILTIN_SKILLS_PATH = APP_ROOT + "/builtin_skills"
UPLOADED_SKILLS_PATH = "/skills"
LLM_REQUEST_TIMEOUT_SEC = 60
# Safety valve for a single LLM response body. Must comfortably fit a chat
# completion JSON including reasoning text and tool calls (also the default
# cap for skill http_get/http_post that don't pass their own max_bytes).
LLM_MAX_RESPONSE_BYTES = 65536
# Budget counts LLM round-trips: a cold-start skill chain (e.g. mijia
# list_devices -> device_spec -> set_prop -> final answer) needs 4 rounds.
LLM_MAX_TOOL_ROUNDS = 6
AGENT_HISTORY_MAX = 12
BACKGROUND_THREAD_STACK_SIZE = 24 * 1024

# Chat persistence
# A session expires after this much silence (no message); the next message
# then starts a fresh conversation. Overridable at runtime through the
# agent config key "session_timeout_min" (minutes; 0 disables expiry).
SESSION_IDLE_TIMEOUT_SEC = 15 * 60
# Expiry is also checked for *every* key by a periodic sweep, because a session
# belonging to someone who never messages again (a WeChat contact, say) would
# otherwise keep its history in RAM for the life of the process. A sweep can
# trigger memory consolidation - a blocking LLM call on the event-loop thread -
# so each pass handles at most this many sessions.
SESSION_SWEEP_MAX_PER_PASS = 4

# Memory system
# A conversation needs this many messages to be worth the LLM call that folds
# it into MEMORY.md.
MEMORY_CONSOLIDATE_THRESHOLD = 20
# Messages kept verbatim when compacting a session that is still going.
MEMORY_KEEP_RECENT = 8
MEMORY_CONTEXT_MAX_CHARS = 6000

# GPIO status reporting. The default reports the on-board LED only; scanning
# every input pin on every status poll interacts poorly with I2S and SPI pins.
GPIO_STATUS_PINS = (38,)

# Voice I/O
VOICE_SAMPLE_RATE = 16000
VOICE_REQUEST_TIMEOUT_SEC = 60
VOICE_TTS_MAX_CHARS = 120
# PCM accumulated before playback starts, so mid-stream network jitter
# doesn't underrun the speaker.
VOICE_TTS_PREBUFFER_MS = 500

# Audio I2S hardware (xiaozhi bread-compact-wifi compatible)
I2S_MIC_SCK = 5
I2S_MIC_WS = 4
I2S_MIC_SD = 6
I2S_SPK_SCK = 15
I2S_SPK_WS = 16
I2S_SPK_SD = 7
# Mic ring buffer. Sized to hold a full second because the ASR session is
# opened when speech starts, and its TLS handshake stalls the capture loop for
# 600-900ms; a smaller ring would drop exactly the beginning of the utterance.
# The speaker buffer below is the same size and the two are never open at once
# on the ESP-SR path (the mic is released before playback starts).
I2S_BUF_SIZE = 32000
# Speaker gets a larger ring buffer: TTS chunks arrive over TLS with jitter
# and each chunk costs CPU (JSON parse, b64 decode, volume scale, GC), so
# the feed loop can stall briefly; the extra buffer rides through the gaps.
I2S_SPK_BUF_SIZE = 32000
I2S_MIC_GAIN = 4
SPEAKER_VOLUME_DEFAULT = 100
VOICE_CHUNK_MS = 100

# Voice wake word / listen timeout
VOICE_LISTEN_TIMEOUT_MS = 8000
# Warmup applies only until the VAD noise floor is learned (first listen
# after boot); later listens start detecting immediately.
VOICE_WARMUP_MS = 300
# Trailing silence the VAD needs before it commits an utterance. On-device
# timing showed 1500ms costs twice: it delays the commit by that much *and*
# pads the PCM sent to ASR (3000ms of audio for 1400ms of speech). 800ms
# still rides out a normal mid-sentence pause.
VOICE_VAD_POST_SILENCE_MS = 800
# The ESP-SR AFE owns the microphone, so when the firmware exposes its capture
# API (esp_sr.capture/read) the channel keeps the AFE running through
# LISTENING and uses the AFE's own VAD instead of the energy VAD: the firmware
# buffers only the utterance (pre-roll included) and reports speech end once
# its trailing-silence window - VOICE_VAD_POST_SILENCE_MS - has elapsed. That
# removes the detector teardown/I2S re-open per turn and endpoints on spectral
# features rather than amplitude, so a noise burst no longer opens a segment.
# Older firmware without the API falls back to the energy VAD automatically.
VOICE_AFE_VAD = True
# Pre-roll the AFE keeps ahead of its VAD trigger so the first syllable of an
# utterance is not clipped (esp_sr.init(vad_delay_ms=...)).
VOICE_AFE_PREROLL_MS = 256
# Longest one capture read waits for audio. Bounds how late the state machine
# notices its timeouts, and the granularity of draining the playback tail.
VOICE_AFE_READ_TIMEOUT_MS = 200
# Safety net for a segment the AFE never closes (VAD held open by sustained
# noise): commit anyway instead of staying in LISTENING forever.
VOICE_AFE_MAX_SEGMENT_MS = 20000
VOICE_THREAD_STACK_SIZE = 24 * 1024
# The audio thread catches its own exceptions and exits rather than taking the
# device down; the main loop's supervisor then restarts it. Rate-limited so a
# failure that recurs immediately (dead I2S, provider bug) retries at a sane
# cadence instead of spinning up threads back to back.
VOICE_THREAD_RESTART_COOLDOWN_MS = 10000

# Hardware watchdog. The event loop feeds it during its maintenance pass; a
# hard fault or a task that stops the loop reboots the device.
WATCHDOG_ENABLED = True
WATCHDOG_TIMEOUT_MS = 30000
# After TTS playback, ignore wake events until the speaker queue drains so
# the device's own voice is not mistaken for user speech (half-duplex;
# there is no AEC on single-mic boards, same strategy as xiaozhi non-AFE).
VOICE_PLAYBACK_TAIL_MS = 700
# After TTS playback the channel keeps listening for a follow-up utterance
# (no wake word needed); with no speech within this window it returns to
# the wake-word stage.
VOICE_FOLLOWUP_TIMEOUT_MS = 5000
# The LLM calls this skill to control the voice session: end the
# conversation (action=exit, passing the farewell to speak as its "ack"
# argument) or adjust the speaker volume mid-conversation (action=volume).
# On exit the voice channel speaks the ack and returns to the wake stage
# instead of opening follow-up listening. VOICE_EXIT_ACK is only the
# last-resort fallback when no ack is available.
VOICE_CONTROL_SKILL = "voice_control"
VOICE_EXIT_ACK = "OK"

# Channel registry: name → forced on (cannot be disabled by user)
CHANNELS_FORCED_ON = ("web",)

# WeChat channel (iLink API, personal WeChat text chat)
WEIXIN_STATE_PATH = "/config/weixin.json"
WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
# Long-poll window for getupdates; the server may override via
# longpolling_timeout_ms. Reply context tokens expire after ~90-160s of
# silence, so refresh them before sending when older than the threshold.
WEIXIN_POLL_TIMEOUT_SEC = 35
WEIXIN_CONTEXT_TOKEN_MAX_AGE_MS = 60 * 1000
WEIXIN_MAX_TEXT_LEN = 1800
WEIXIN_QR_SESSION_MS = 600 * 1000
