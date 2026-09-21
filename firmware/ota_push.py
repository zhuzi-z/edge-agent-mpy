#!/usr/bin/env python3
"""Push an OTA bundle to a device and wait for it to come back on the new app.

    python3 firmware/ota_push.py --host 192.168.2.216 firmware/edge-agent-mpy-ota-<agent>.tar

POSTs the bundle as the raw body of ``POST /ota``, exactly like the WebUI's
Firmware Update card does, then polls ``GET /ota`` through the reboot until the
device says what it ended up running.  A device whose new app does not come up
answers from the kept-back one and reports ``bad``: the push worked, the update
did not, so that exits non-zero too.

``make ota-push OTA_HOST=<ip>`` builds the bundle and calls this with it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# What the device expects an upload to be labelled as; it reads the body either way.
CONTENT_TYPE = "application/x-tar"


def post_bundle(base: str, bundle: str, timeout: float):
    """POST <bundle> to /ota.  Returns (http_status, reply)."""
    with open(bundle, "rb") as handle:
        body = handle.read()
    request = urllib.request.Request(
        base + "/ota", data=body, method="POST", headers={"Content-Type": CONTENT_TYPE}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, json.loads(reply.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw}


def poll(base: str, timeout: float):
    """GET /ota until the rebooted device settles on "ok" or "bad", or None.

    "trial" and "booting" are the states the device passes through on the way,
    and the connection simply fails while it is down - both are expected here.
    """
    deadline = time.time() + timeout
    seen_down = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/ota", timeout=5) as reply:
                data = json.loads(reply.read().decode())
            if data.get("status") in ("ok", "bad"):
                return data
        except (OSError, ValueError, urllib.error.URLError):
            seen_down = True
            print(".", end="", flush=True)
        time.sleep(1)
    if seen_down:
        print()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", help="the .tar built by `make ota`")
    parser.add_argument("--host", required=True, help="device IP, or a full http:// base URL")
    parser.add_argument("--timeout", type=float, default=120, help="seconds to wait for the reboot")
    args = parser.parse_args()

    base = args.host if args.host.startswith("http") else "http://" + args.host
    base = base.rstrip("/")
    if not os.path.exists(args.bundle):
        sys.exit("error: no such bundle: {}".format(args.bundle))

    size = os.path.getsize(args.bundle)
    print("pushing {} ({:,} bytes) to {}".format(args.bundle, size, base))
    status, reply = post_bundle(base, args.bundle, args.timeout)
    if status != 200:
        sys.exit("error: the device refused the bundle (HTTP {}): {}".format(
            status, reply.get("error", reply)))
    print("accepted {}, restarting - waiting for it to come back".format(reply.get("version", "?")))

    data = poll(base, args.timeout)
    if data is None:
        sys.exit("error: the device did not answer /ota within {:.0f}s".format(args.timeout))
    print(
        "back online: {} running from {} (fallback {})".format(
            data.get("version") or "?", data.get("running") or "?", data.get("fallback") or "none"
        )
    )
    if data.get("status") != "ok":
        sys.exit(
            "error: the new app did not start (status {}) - the device fell back to {}.\n"
            "       fix the bundle and push again; it runs the kept-back app meanwhile".format(
                data.get("status"), data.get("version") or "?"
            )
        )
    print("update confirmed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
