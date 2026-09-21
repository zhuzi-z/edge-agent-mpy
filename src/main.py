"""ESP32-S3 entry point, and the recovery layer an update cannot touch.

On MicroPython this file is executed directly. On CPython (Linux) import
app.main instead.

An update puts the new app in /app and keeps the one it replaced in /pre/app
(app/ota.py).  This file is the only thing that decides which of the two runs,
and it sits outside both: it imports nothing from app, and no bundle ever
carries it, so a broken update cannot break the code that recovers from one.

The handshake is the ``status`` field of /config/ota.json:

    trial    an update landed; it has not been booted yet
    booting  this file is trying /app right now
    ok       /app came up and confirmed itself (app/ota.py: confirm)
    bad      /app did not come up; run /pre until the next update replaces it

Trying /app writes "booting" first, so a crash Python cannot catch - a hard
fault, a watchdog reset, a power dip - is still visible on the next boot as a
status that never became "ok".  A failure that does raise is caught here, marked
bad and answered with a reboot, rather than by importing the old app into a
process the new one already half started: whatever threads and peripherals it
got as far as opening would still be running underneath it.
"""

import json
import os
import sys

# The same three paths as config.OTA_STATE_PATH / OTA_APP_DIR / OTA_PRE_DIR,
# spelled out here because this file has to work when the app cannot be read.
STATE_PATH = "/config/ota.json"
# sys.path entries, not app directories: `import app` finds <root>/app/.
ROOT_NEW = "/"
ROOT_PRE = "/pre"


def read_state():
    """The update state, or {} when there is none (a device that never updated)."""
    try:
        with open(STATE_PATH) as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state):
    try:
        with open(STATE_PATH, "w") as handle:
            json.dump(state, handle)
    except OSError as e:
        print("ota: cannot write {}: {}".format(STATE_PATH, e))


def mark_bad(state):
    """Record that /app did not come up, so it is not tried again."""
    state["status"] = "bad"
    write_state(state)


def has_pre():
    """Is there a kept-back app to fall back to?"""
    try:
        os.listdir(ROOT_PRE + "/app")
        return True
    except OSError:
        return False


def run(root):
    """Import and run the app package under <root>. Returns only if it exits."""
    # Absolute, so a module's own __file__ (which config.py turns into APP_ROOT)
    # says which copy of the app is running.
    sys.path.insert(0, root)
    from app.main import main

    main()


def reset():
    import machine

    machine.reset()


def boot():
    state = read_state()
    status = state.get("status")

    if status == "trial":
        state["status"] = "booting"
        write_state(state)
        try:
            run(ROOT_NEW)
        except Exception as e:  # noqa: BLE001 - any failure counts
            print("ota: the updated app failed to start:")
            sys.print_exception(e)
            mark_bad(state)
            reset()
        return

    # "booting" is a trial boot that never confirmed; "bad" is sticky, so a
    # device that failed once keeps coming up on the version that worked until
    # the next update replaces it.
    if status in ("booting", "bad") and has_pre():
        if status == "booting":
            print("ota: the updated app did not come up, running the previous one")
            mark_bad(state)
        run(ROOT_PRE)
        return

    run(ROOT_NEW)


if __name__ == "__main__":
    boot()
