# Over-the-Air Update

Updating a device that is already installed and somewhere you cannot reach with
a USB cable: one tar file, uploaded from the WebUI, and a boot that falls back
to the app it replaced if the new one does not start.

```bash
make ota                          # -> firmware/edge-agent-mpy-ota-<agent>.tar
make ota OTA_SKILLS=mijia         # the same, plus skills/mijia in the bundle
make ota-push OTA_HOST=192.168.2.216   # build it, push it, wait for the reboot
```

`OTA_HOST` defaults to `192.168.2.216`, the static IP a provisioned device is
given here; pass it explicitly for any other address.

Or in the browser: **Status → Firmware Update → Upload bundle**. The card shows
what is running, what is kept as a fallback, and whether the device had to go
back to it.

## What an update does

Two directories on the device, and one file that says which of them runs:

| Path | Holds |
|------|-------|
| `/app` | the app that is running |
| `/pre/app` | the app the last update replaced — the fallback |
| `/config/ota.json` | the state `src/main.py` reads on the next boot |
| `/main.py` | the recovery layer: picks one of the two, and is never updated |

Installing a bundle **renames** `/app` to `/pre/app` (littlefs renames in place,
so keeping 400 KB of the old app costs no copy), unpacks the new tree into
`/app`, writes the state below and reboots.

```
trial    an update landed; it has not been booted yet
booting  src/main.py is trying /app right now
ok       /app came up and said so — this is the version to keep
bad      /app did not come up — run /pre until the next update
```

`src/main.py` runs before the app does, so it can afford to know nothing about
it:

- `trial` → write `booting`, then try `/app`. If anything in it raises, mark
  `bad` and reboot, which lands in the next case.
- `booting` → the trial boot never confirmed (a hard fault, a watchdog reset, a
  power dip — anything Python could not catch). Mark `bad` and run `/pre`.
- `bad` → run `/pre`. Sticky: it keeps coming up on the version that worked
  until an update replaces it.
- anything else → run `/app`.

The new app calls `ota.confirm()` once it is built — before it touches the
network, so a dead router or a first boot spent in the provisioning AP is not
mistaken for a failed update — which turns `booting` into `ok`.

Running from `/pre` is a normal boot, not a degraded one: the WebUI, the
builtin skills and the app code are all the kept version's, because everything
the app ships lives inside its own package directory (`config.APP_ROOT`, which
follows `__file__`). Only user data is shared between the two — `/config`,
`/data` and the uploaded skills in `/skills` — which is the point: a rollback
keeps your settings and your chat history.

`/pre` always holds the last version that booted. Updating while already on the
fallback replaces the failed `/app` and leaves `/pre` alone, so two bad bundles
in a row still land on the same good app.

## The bundle

What `make ota` builds: an uncompressed tar, `manifest.json` first, then every
file under the names it gets on the device.

```
manifest.json                 {"version": ..., "files": {name: {size, sha256}}}
app/...                       src/app, WebUI and builtin skills included
skills/<name>/...             only with OTA_SKILLS=<name>
```

The version is `git describe` of the checkout, the same `<agent>` the release
images are named after, so a bundle says which commit it came from.

Two halves, and the split is the whole safety story:

- `DEPLOYED` (`firmware/make_app_fs.py`) is what `make deploy` and a factory
  image carry: `src/main.py` + `src/app`.
- `OTA_PAYLOAD` is `DEPLOYED` minus `RECOVERY` (`src/main.py`). **A bundle never
  carries the recovery layer**, because a bundle that broke it would break the
  only code that can recover from a broken bundle. `main.py` reaches a device
  over the cable or in a factory image.

On the device a bundle is at most `config.OTA_MAX_BUNDLE_BYTES` (2 MB): the tar
is not compressed, so the `Content-Length` a client announces is the number of
bytes that would land on flash, and one that is too big is refused before
anything is written. The body is streamed to `/data/ota.tar` as it arrives —
it never becomes a Python object — and parsed from there, so an update costs a
few KB of heap however big the bundle is.

The manifest is the only integrity there is: every file is digested as it is
written and compared, and a mismatch puts the previous app back where it was
and reports the failure without rebooting. There is no signature and no
encryption, and the API is as open as the rest of this device's — it belongs on
a trusted LAN.

Two modules, split along the only line that matters here: `src/app/tar.py`
reads the tar — walk the headers, copy one member out, digest it on the way —
and knows nothing about updates, while `src/app/ota.py` is what an update
*means*: the manifest, which paths may be written, keeping the old app, and the
state the next boot reads.

A member is installed only if it is under `app/` or `skills/`, has something
after that first component, and has no `..` in it. Everything else — `/main.py`,
`/config`, `/data` — is refused by construction.

## API

| Route | Body | Returns |
|-------|------|---------|
| `GET /ota` | — | `{status, version, fallback, running, has_previous, max_bytes}` |
| `POST /ota` | the bundle, raw | `{status, version, ...}` and reboots ~1 s later |
| `POST /ota/rollback` | `{}` | the same, after marking the update `bad` |

`version` is the one actually running, so after a rollback it is the fallback's
and `fallback` names the app that failed. Errors come back as `{"error": "..."}`
with 400 (refused bundle), 409 (nothing kept to roll back to), 411/413 (no or
too big a `Content-Length`) or 500 (flash said no).

The reply is sent before the reset, and the WebUI then polls `GET /ota` through
the gap where nothing answers — which is also what `make ota-push` does, exiting
non-zero if the device comes back as `bad`.

## What it does not do

- **No delta updates.** Every bundle is the whole app (~400 KB); a bundle only
  carrying changed files would need the device to know what it has.
- **One fallback, not a history.** `/pre` is the previous version, and only
  that. There is no third copy and no way to pick a version.
- **Uploaded skills are not rolled back.** A bundle may add to `/skills`, and
  an app that fails because of a skill it does not know is a problem to fix, not
  one to recover from — the fallback app runs the skills that are there.
- **Settings are not versioned.** A new app that migrates `/config/agent.json`
  leaves the migrated file behind for the old one. Migrations in
  `storage/store.py` are written to tolerate that.
- **The firmware is out of scope.** MicroPython itself and the wake-word models
  live in their own partitions and only move by flashing — see
  [../firmware/README.md](../firmware/README.md).

## When it rolls back

The WebUI card says `rolled back` and names both versions; `GET /ota` says the
same. The device is fully usable on the old app — that is the whole point — so
the way forward is to fix whatever the new one did, `make ota` again and push
it. The serial log has the traceback: `src/main.py` prints it before it marks
the update bad. With a cable, `make deploy` overwrites both directories' worth
of app and is the quicker way out.
