.PHONY: ut e2e test fmt lint loc deploy app-fs app-fs-check factory-image ota ota-push flash-repl unix-dev repl diag diag-clean

MICROPY ?= $(shell command -v micropython 2>/dev/null || echo /usr/bin/micropython)
MPREMOTE ?= $(shell command -v mpremote 2>/dev/null || echo $(HOME)/.local/bin/mpremote)
PYTHON  ?= $(shell test -f .venv/bin/python && echo .venv/bin/python || echo python3)
RUFF    ?= $(shell command -v ruff 2>/dev/null || echo $(HOME)/.local/bin/ruff)
DEVICE ?= $(shell ls /dev/ttyACM* 2>/dev/null | head -1)

ut:
	MICROPY=$(MICROPY) ./tests/ut/test_unix.sh

e2e:
	HF_ENDPOINT=$(HF_ENDPOINT) MICROPY=$(MICROPY) $(PYTHON) tests/e2e/appflow_runner.py

test: ut e2e

fmt:
	$(RUFF) format src/ tests/ skills/

lint:
	$(RUFF) check src/ tests/ skills/

loc:
	@./tools/loc.sh

FAKE_PORT ?= 19100
HF_ENDPOINT ?= https://hf-mirror.com

unix-dev:
	HF_ENDPOINT=$(HF_ENDPOINT) $(PYTHON) tests/fake_llm_server.py $(FAKE_PORT) 2>/dev/null & FAKE_PID=$$!; \
	PYTHONUNBUFFERED=1 $(PYTHON) tests/launch/audio_bridge.py 2>/dev/null & BRIDGE_PID=$$!; \
	MICROPYPATH=".frozen:$(CURDIR)/src:$(CURDIR)/tests/stubs:$(CURDIR)/tests" \
	$(MICROPY) -X heapsize=8M tests/launch/run_agent.py --fake-port $(FAKE_PORT) --seed-config; \
	kill $$BRIDGE_PID $$FAKE_PID 2>/dev/null

# Two paths to the same app, for two audiences:
#   deploy        developer path - overwrites only main.py and app/ (the WebUI
#                 is inside it), so /config, /data and /skills survive
#   factory-image user path - one whole-flash image for 0x0, so a device that
#                 never had a serial connection still boots into the agent;
#                 the app-fs target builds the filesystem inside it
# Both use one file set: DEPLOYED in firmware/make_app_fs.py.
# The third path, for a device in the field, is `ota` below: the same set minus
# the recovery layer, as a tar - see docs/ota.md.
deploy:
	@find src -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "Deploying to $(DEVICE)..."
	-$(MPREMOTE) connect $(DEVICE) rm -r :app
	# A fresh main.py + app/ is the whole truth, so the OTA fallback and its
	# state go too: leaving them would boot /pre on a device just deployed to.
	-$(MPREMOTE) connect $(DEVICE) rm -r :pre
	-$(MPREMOTE) connect $(DEVICE) rm :config/ota.json
	$(MPREMOTE) connect $(DEVICE) resume $$($(PYTHON) firmware/make_app_fs.py --print-mpremote) + reset
	@echo "Deploy complete!"

app-fs:
	$(PYTHON) firmware/make_app_fs.py

app-fs-check:
	$(PYTHON) firmware/make_app_fs.py --check

# Over-the-air update: the deployed set minus src/main.py (the recovery layer,
# which only ever moves by deploy or a factory image), as one tar.  The last
# line `--ota` prints is the bundle's path, which is what ota-push hands on.
OTA_SKILLS ?=
OTA_ARGS = $(if $(OTA_SKILLS),--skills $(OTA_SKILLS))
OTA_HOST ?= 192.168.2.216

ota:
	$(PYTHON) firmware/make_app_fs.py --ota $(OTA_ARGS)

# Build the bundle, push it, and wait for the device to come back and say which
# version it ended up running (a fallback to the kept-back app counts as a
# failure and exits non-zero).
ota-push:
	@$(PYTHON) firmware/ota_push.py --host $(OTA_HOST) \
		$$($(PYTHON) firmware/make_app_fs.py --ota $(OTA_ARGS) | tail -1)

# One file for the whole 16 MiB flash, for users without a toolchain: they pick
# it, type 0x0, done.  It wipes the device, so it is an installer, never an
# updater - see firmware/README.md.
factory-image: app-fs
	$(PYTHON) firmware/make_app_fs.py --factory

flash-repl:
	$(MAKE) --no-print-directory deploy
	$(MPREMOTE) connect $(DEVICE) resume + sleep 2 + resume repl

repl:
	$(MPREMOTE) connect $(DEVICE) repl

diag:
	@echo "Deploying audio diagnostic to $(DEVICE)..."
	$(MPREMOTE) connect $(DEVICE) cp tests/board/audio_diag.py :audio_diag.py
	$(MPREMOTE) connect $(DEVICE) exec "import audio_diag; audio_diag.run()"

diag-clean:
	@echo "Removing audio diagnostic from $(DEVICE)..."
	-$(MPREMOTE) connect $(DEVICE) rm :audio_diag.py
