"""Over-the-air app update: take a tar bundle, keep the old app as a fallback.

The bundle is what ``make ota`` builds: an uncompressed tar of the app tree
(and, optionally, some skills) with a ``manifest.json`` of per-file size and
sha256 in front of it.  Installing moves the running app out of the way instead
of writing over it:

    /app        the app that is running
    /pre/app    the app the last update replaced - the fallback

and leaves behind the "trial" status that ``src/main.py`` reads on the next
boot.  That file, not this module, decides which of the two runs: an app that
does not come up is answered by booting ``/pre`` instead, which is what makes an
update unable to leave a device that does not start.  ``/main.py`` itself is
never in a bundle (``RECOVERY`` in firmware/make_app_fs.py), so the recovery
layer cannot be updated into a broken state.

Reading the tar itself - the headers, the offsets, copying a member out - is
``app/tar.py``; this module is what an update means.  Integrity is the manifest
digest per file, and that is all of it: no signature, no compression, no
resume.  The API is open like the rest of this device's, so it belongs on a
trusted LAN.
"""

import asyncio
import gc
import json
import os

import app.config as config
import app.log as log
import app.tar as tar
from app.util import ensure_parent_dir, json_response as _json

MANIFEST = "manifest.json"
# Body bytes taken off the socket per read while a bundle arrives.
_RECV_CHUNK = 4096


class OtaError(Exception):
    """A bundle that was refused, worded for whoever uploaded it."""


def _pre_app():
    """The kept-back app.  One level down, because `import app` wants <root>/app."""
    return config.OTA_PRE_DIR + "/app"


def read_state():
    """The update state, or {} on a device that has never been updated."""
    try:
        with open(config.OTA_STATE_PATH) as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state):
    ensure_parent_dir(config.OTA_STATE_PATH)
    with open(config.OTA_STATE_PATH, "w") as handle:
        json.dump(state, handle)


def confirm():
    """This app came up: it is the one to keep.

    Called once, from app/main.py, as soon as the app is built - before it talks
    to any network.  Waiting for WiFi or for the HTTP server would roll a good
    update back because of a router, and a first boot spends minutes in the
    provisioning AP.
    """
    state = read_state()
    if state.get("status") in ("trial", "booting"):
        state["status"] = "ok"
        write_state(state)
        log.info(
            "OTA",
            "running {}, keeping {} as the fallback".format(
                state.get("current") or "?", state.get("previous") or "nothing"
            ),
        )


def has_pre():
    """Is there a kept-back app to fall back to?"""
    try:
        os.listdir(_pre_app())
        return True
    except OSError:
        return False


def status():
    """What is on the device, for GET /ota and the WebUI's update card."""
    state = read_state()
    bad = state.get("status") == "bad"
    return {
        "status": state.get("status") or "none",
        # "bad" means src/main.py runs /pre, so the version named "previous" is
        # the one answering this request and "current" is the one that failed.
        "version": state.get("previous" if bad else "current", ""),
        "fallback": state.get("current" if bad else "previous", ""),
        "running": config.OTA_PRE_DIR if bad else config.OTA_APP_DIR,
        "has_previous": has_pre(),
        "max_bytes": config.OTA_MAX_BUNDLE_BYTES,
    }


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _rm_tree(path):
    """Delete a directory tree.  There is no shutil on the device."""
    try:
        names = os.listdir(path)
    except OSError:
        _rm(path)  # a file, or nothing at all
        return
    for name in names:
        _rm_tree(path + "/" + name)
    try:
        os.rmdir(path)
    except OSError as e:
        raise OtaError("cannot remove {}: {}".format(path, e))


def _target(name):
    """Where a bundle member lands, or None when it must not be installed.

    Two roots, and only two: the app itself and the uploaded skills.  Anything
    else a bundle could name - /main.py, /config, /data - is refused here, which
    is what keeps the recovery layer and the user's own data out of an update's
    reach.  The rest of the name is appended, never interpreted.
    """
    top, _, rest = name.partition("/")
    if not rest or ".." in name.split("/"):
        return None
    if top == "app":
        return config.OTA_APP_DIR + "/" + rest
    if top == "skills":
        return config.UPLOADED_SKILLS_PATH + "/" + rest
    return None


def _load_manifest(path, member):
    try:
        manifest = json.loads(tar.read(path, member).decode())
    except (ValueError, UnicodeError):
        raise OtaError("{} is not JSON this device can read".format(MANIFEST))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise OtaError("{} lists no files".format(MANIFEST))
    return manifest, files


def scan(path):
    """Check a whole bundle before touching the flash: (manifest, entries).

    ``entries`` maps a member name to (size, offset).  Only headers are read
    here, so a bundle that is refused - wrong shape, a path outside the app, a
    file the manifest does not vouch for - costs nothing and leaves the running
    app exactly as it was.
    """
    entries = {}
    manifest = None
    for member in tar.members(path):
        name, _size, _offset, is_file = member
        if name == MANIFEST:
            if manifest is not None:
                raise OtaError("{} appears twice".format(MANIFEST))
            manifest = _load_manifest(path, member)
            continue
        if _target(name) is None:
            raise OtaError("{} is not a path this device installs".format(name))
        if not is_file:  # a directory entry: the extractor makes parents itself
            continue
        if name in entries:
            raise OtaError("{} appears twice in the bundle".format(name))
        entries[name] = member
    if manifest is None:
        raise OtaError("the bundle carries no {}".format(MANIFEST))
    manifest, files = manifest
    for name, member in entries.items():
        known = files.get(name)
        if not isinstance(known, dict):
            raise OtaError("{} is not in the manifest".format(name))
        if known.get("size") != member[1]:
            raise OtaError(
                "{} is {} bytes in the bundle, the manifest says {}".format(
                    name, member[1], known.get("size")
                )
            )
    for name in files:
        if name not in entries:
            raise OtaError("the manifest lists {}, the bundle does not carry it".format(name))
    return manifest, entries


def _extract(bundle, entries, files):
    """Write every member to its place on flash, digesting it as it goes."""
    for name, member in entries.items():
        target = _target(name)
        ensure_parent_dir(target)
        if tar.extract(bundle, member, target) != files[name].get("sha256"):
            raise OtaError("{} does not match the manifest".format(name))


def _keep_previous(state):
    """Move the app that is running to /pre/app.  Returns (version, moved).

    A rename, not a copy: the app is a few hundred KB and littlefs renames in
    place.  When the device is already running the fallback there is nothing to
    keep - /pre/app *is* the good version, and /app is the one that failed.
    """
    if state.get("status") == "bad":
        _rm_tree(config.OTA_APP_DIR)
        return state.get("previous", ""), False
    previous = state.get("current", "")
    try:
        os.listdir(config.OTA_APP_DIR)
    except OSError:
        return previous, False  # nothing there to keep (a first install)
    _rm_tree(_pre_app())
    ensure_parent_dir(_pre_app())
    os.rename(config.OTA_APP_DIR, _pre_app())
    return previous, True


def install(bundle):
    """Verify <bundle> and put it in place, leaving a trial for the next boot.

    Raises OtaError with something worth showing the user.  On success the new
    app is in /app, the old one in /pre/app and the state says "trial"; the
    caller reboots and src/main.py takes it from there.
    """
    manifest, entries = scan(bundle)
    files = manifest["files"]
    version = str(manifest.get("version") or "unknown")
    state = read_state()
    previous, moved = _keep_previous(state)
    try:
        _extract(bundle, entries, files)
    except Exception as e:  # noqa: BLE001 - the old app has to come back
        log.error("OTA", "install failed: {}".format(e))
        if moved:
            _rm_tree(config.OTA_APP_DIR)
            os.rename(_pre_app(), config.OTA_APP_DIR)
        raise OtaError("install failed, the previous app is back: {}".format(e))
    state["status"] = "trial"
    state["current"] = version
    state["previous"] = previous
    write_state(state)
    log.info(
        "OTA",
        "installed {} ({} files), fallback is {}".format(
            version, len(entries), previous or "nothing"
        ),
    )
    return status()


def rollback():
    """Go back to the kept app on the next boot.  Raises OtaError without one."""
    if not has_pre():
        raise OtaError("this device has no previous app kept")
    state = read_state()
    state["status"] = "bad"  # what src/main.py reads: run /pre
    write_state(state)
    log.info("OTA", "rolling back to {}".format(state.get("previous", "?")))
    return status()


def _reboot():
    """Reset - once the caller's reply has had its second to leave."""

    async def _later():
        await asyncio.sleep(config.OTA_REBOOT_DELAY_SEC)
        import machine

        log.info("OTA", "restarting")
        machine.reset()

    asyncio.create_task(_later())


async def _receive(reader, length, extra, bundle):
    """Stream <length> body bytes into <bundle>, refusing a stalled client."""
    ensure_parent_dir(bundle)
    written = 0
    with open(bundle, "wb") as out:
        if extra:
            extra = extra[:length]  # a head read can overrun into the body
            out.write(extra)
            written = len(extra)
        while written < length:
            try:
                block = await asyncio.wait_for(
                    reader.read(min(_RECV_CHUNK, length - written)), config.OTA_READ_TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                raise OtaError(
                    "no data for {}s at {} of {} bytes".format(
                        config.OTA_READ_TIMEOUT_SEC, written, length
                    )
                )
            if not block:
                raise OtaError("connection closed at {} of {} bytes".format(written, length))
            out.write(block)
            written += len(block)
    return written


async def route_status(server, method, path, body):
    """GET /ota -- what is installed, what is kept, and which one is running."""
    return _json(200, "OK", status())


async def route_upload(server, reader, content_length, extra):
    """POST /ota -- the bundle is the raw body (a stream route, not a JSON one).

    The body never becomes a Python object: it is written to flash as it arrives
    and parsed from there, so a bundle costs a few KB of heap however big it is.
    """
    if content_length <= 0:
        return _json(411, "Length Required", {"error": "send the bundle with a Content-Length"})
    if content_length > config.OTA_MAX_BUNDLE_BYTES:
        return _json(
            413,
            "Payload Too Large",
            {
                "error": "a bundle is at most {} bytes, this one says {}".format(
                    config.OTA_MAX_BUNDLE_BYTES, content_length
                )
            },
        )
    bundle = config.OTA_BUNDLE_PATH
    try:
        await _receive(reader, content_length, extra, bundle)
    except (OtaError, OSError) as e:
        log.warn("OTA", "upload failed: {}".format(e))
        _rm(bundle)
        return _json(400, "Bad Request", {"error": str(e)})
    try:
        result = install(bundle)
    except (OtaError, tar.TarError) as e:
        return _json(400, "Bad Request", {"error": str(e)})
    except (OSError, ValueError) as e:
        log.error("OTA", "install failed: {}".format(e))
        return _json(500, "Internal Server Error", {"error": str(e)})
    finally:
        _rm(bundle)
        gc.collect()
    log.info("OTA", "rebooting into {}".format(result["version"]))
    _reboot()
    return _json(200, "OK", result)


async def route_rollback(server, method, path, body):
    """POST /ota/rollback -- boot the kept app from now on."""
    try:
        result = rollback()
    except (OtaError, OSError) as e:
        return _json(409, "Conflict", {"error": str(e)})
    _reboot()
    return _json(200, "OK", result)
