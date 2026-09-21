#!/usr/bin/env python3
"""Build the ESP32-S3 flash images: the agent app filesystem, and one merged
whole-flash image built out of it.

The image holds exactly what ``make deploy`` pushes to the device (``main.py`` +
``app/``, the WebUI and the builtin skills included), so a board flashed with
``app-fs.bin`` boots straight into the agent and opens the ``EdgeAgent-Setup``
provisioning AP - no serial code push to get started.  That is what makes
browser-only flashing complete: esptool-js can flash files, but it cannot copy
Python onto the device afterwards.

    make app-fs             # -> firmware/app-fs.bin
    make app-fs-check       # verify an existing image against the current sources
    make factory-image      # -> firmware/edge-agent-mpy-<agent>-mpy<micropython>.bin
    make ota                # -> firmware/edge-agent-mpy-ota-<agent>.tar, for POST /ota

The release image is the one for users: firmware + app + wake-word models at
their partition offsets inside a single 16 MiB image, flashed as one file at
``0x0``.  Its name carries both versions that went into it - this checkout of the
agent, and the MicroPython inside it - and both are derived, never typed.

The volume is littlefs, not FAT, and is formatted by ``app_fs_lfs.py`` through
the host MicroPython port: only that build has the same littlefs as the device.
So this script stages files, hands the directory over, and reports - reading the
image back happens in the worker.  See firmware/README.md for the geometry
behind all this and for the offsets in the flash command printed below.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import re
import subprocess
import sys
import tarfile
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(FIRMWARE_DIR, "app_fs_lfs.py")
DEFAULT_IMAGE = os.path.join("firmware", "app-fs.bin")
# the merged image is released as <PROJECT>-<agent version>-mpy<micropython version>.bin
PROJECT = "edge-agent-mpy"
FLASH_SIZE = 16 * 1024 * 1024  # the whole flash, as in partitions-16MiB-sr.csv
VFS_OFFSET = 0x300000  # vfs partition, holds the littlefs image built here
MODELS_OFFSET = 0xF00000  # model partition, holds srmodels.bin
FIRMWARE_GLOB = "ESP32_GENERIC_S3-SPIRAM_OCT-*.bin"
MODELS_GLOB = "srmodels-*.bin"
# The deployed file set, in one place: paths inside the repository.  On the
# device every entry keeps its last component (src/app -> /app), which is also
# how mpremote installs it, so the deploy target and the factory image cannot
# drift apart.
DEPLOYED = ("src/main.py", "src/app")
# Never part of the app: build leftovers that must not reach the device.
SKIP = ("__pycache__", ".git", ".DS_Store", "*.pyc", "*.pyo")

# The recovery layer.  src/main.py is what decides whether to run /app or the
# kept-back /pre copy, so an over-the-air bundle must never carry it: a bundle
# that broke it would break the only code that can recover from a broken bundle.
# It reaches a device through `make deploy` or a factory image, never through OTA.
RECOVERY = ("src/main.py",)
# What an OTA bundle holds: the deployed set minus the recovery layer.
OTA_PAYLOAD = tuple(path for path in DEPLOYED if path not in RECOVERY)
# The only top-level names a bundle may contain, which is also what keeps
# /main.py, /config, /data and /skills' user data out of reach of an update.
OTA_ALLOWED_ROOTS = ("app", "skills")
OTA_MANIFEST = "manifest.json"
OTA_SKILLS_DIR = "skills"
# The device refuses a bigger body (config.OTA_MAX_BUNDLE_BYTES); refusing the
# same bundle here saves the user a round trip that cannot succeed.
OTA_MAX_BUNDLE_BYTES = 2 * 1024 * 1024


def micropython() -> str:
    """Host MicroPython binary - the unix port, with the device's littlefs."""
    path = os.environ.get("MICROPY") or shutil.which("micropython")
    if not path or not os.path.exists(path):
        raise SystemExit(
            "error: MicroPython (unix port) not found\n"
            "       set MICROPY=/path/to/micropython, the binary `make test` uses"
        )
    return path


def worker(*args: str) -> str:
    """Run the littlefs worker and return its summary, the last line it printed."""
    done = subprocess.run(
        [micropython(), WORKER, *args], capture_output=True, text=True, cwd=REPO_ROOT
    )
    output = (done.stdout + done.stderr).strip()
    if done.returncode != 0:
        raise SystemExit("error: MicroPython rejected the image:\n" + output)
    return output.splitlines()[-1] if output else "ok"


def check_image(image: str, stage: str) -> None:
    """Mount the image the way the device does and compare it with the sources."""
    summary = worker("check", image, stage)
    print("{}: matches the sources ({})".format(relpath(image), summary))


def _copy_in(src: str, dst: str) -> None:
    """Copy one deployed entry (a file or a tree) into a staging directory."""
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP))
    else:
        shutil.copy(src, dst)


def _require_sources(paths) -> None:
    missing = [src for src in paths if not os.path.exists(os.path.join(REPO_ROOT, src))]
    if missing:
        raise SystemExit("error: this checkout has no {}".format(", ".join(missing)))


def stage_app() -> str:
    """Copy the deployed file set into a staging dir with the on-device layout."""
    _require_sources(DEPLOYED)
    stage = tempfile.mkdtemp(prefix="edge-app-")
    for source in DEPLOYED:
        _copy_in(os.path.join(REPO_ROOT, source), os.path.join(stage, os.path.basename(source)))
    return stage


def sha256(path: str) -> str:
    """Digest of the image, so a published build can be traced to its sources."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree(directory: str, prefix: str = "") -> list:
    """Every file under <directory>, as sorted bundle-relative paths."""
    found = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            found += tree(path, prefix + name + "/")
        else:
            found.append(prefix + name)
    return found


def stage_ota(skills) -> str:
    """Copy the OTA payload into a staging dir laid out like the device's flash.

    Member names are the paths they get on the device (``app/main.py``,
    ``skills/mijia/code.py``), so the device side is a straight "/" + name.
    No directory entries are staged: the extractor creates parents as it goes.
    """
    _require_sources(OTA_PAYLOAD)
    stage = tempfile.mkdtemp(prefix="edge-ota-")
    for source in OTA_PAYLOAD:
        _copy_in(os.path.join(REPO_ROOT, source), os.path.join(stage, os.path.basename(source)))
    for name in skills:
        source = os.path.join(OTA_SKILLS_DIR, name)
        _require_sources([source])
        _copy_in(os.path.join(REPO_ROOT, source), os.path.join(stage, OTA_SKILLS_DIR, name))
    for path in tree(stage):
        if path.split("/")[0] not in OTA_ALLOWED_ROOTS:
            raise SystemExit("error: {} does not belong in a bundle".format(path))
    return stage


def write_manifest(stage: str, version: str) -> dict:
    """Write manifest.json into the staging root: the version, size+sha256 per file.

    The device checks every file it extracts against this before it reboots into
    the new app, which is the only integrity the format has - the tar itself is
    neither compressed nor signed.
    """
    files = {}
    for name in tree(stage):
        path = os.path.join(stage, name)
        files[name] = {"size": os.path.getsize(path), "sha256": sha256(path)}
    manifest = {"version": version, "files": files}
    with open(os.path.join(stage, OTA_MANIFEST), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return manifest


def write_tar(stage: str, output: str) -> None:
    """The bundle itself: plain ustar, manifest first, fixed metadata.

    ustar because the device parses it with a few dozen lines of its own (no
    tarfile on MicroPython), and the longest name here is far inside its 100
    character field.  The manifest goes first so the device can read it and
    refuse a bad bundle without walking the rest.  Zeroed mtime/uid/uname keep
    one checkout's bundle byte-identical between builds.
    """
    names = [OTA_MANIFEST] + [n for n in tree(stage) if n != OTA_MANIFEST]
    with tarfile.open(output, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name in names:
            path = os.path.join(stage, name)
            info = tarfile.TarInfo(name)
            info.size = os.path.getsize(path)
            info.mtime = 0
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            with open(path, "rb") as handle:
                tar.addfile(info, handle)


def build_ota(skills) -> str:
    """Build firmware/<project>-ota-<agent>.tar and print it; last line is its path."""
    version = agent_version()
    output = os.path.join(FIRMWARE_DIR, "{}-ota-{}.tar".format(PROJECT, version))
    stage = stage_ota(skills)
    try:
        manifest = write_manifest(stage, version)
        write_tar(stage, output)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    size = os.path.getsize(output)
    if size > OTA_MAX_BUNDLE_BYTES:
        os.remove(output)
        raise SystemExit(
            "error: the bundle is {:,} bytes, over the {:,} the device accepts\n"
            "       drop files from OTA_PAYLOAD or raise both limits".format(
                size, OTA_MAX_BUNDLE_BYTES
            )
        )
    print(
        "{}: {} files, {:,} bytes of {}{}".format(
            version,
            len(manifest["files"]),
            size,
            ", ".join(OTA_PAYLOAD),
            " + skills/" + ",skills/".join(skills) if skills else "",
        )
    )
    print(relpath(output))
    return output


def deploy_chain() -> str:
    """The deployed file set as mpremote arguments, used by the Makefile."""
    chain = []
    for source in DEPLOYED:
        if os.path.isdir(os.path.join(REPO_ROOT, source)):
            chain.append("cp -r {} :".format(source))
        else:
            chain.append("cp {} :{}".format(source, os.path.basename(source)))
    return " + ".join(chain)


def newest(pattern: str) -> str:
    """Newest image in firmware/ matching <pattern>, empty when there is none.

    Both a release download and `firmware/build.sh` put the firmware and model
    images here under names that carry the version and the build date, so the
    last one in sort order is the newest build.
    """
    found = sorted(glob.glob(os.path.join(FIRMWARE_DIR, pattern)))
    return found[-1] if found else ""


def require_part(pattern: str) -> str:
    """The part <pattern> names, with a plain error when firmware/ has none."""
    part = newest(pattern)
    if not part:
        raise SystemExit(
            "error: no {} in firmware/\n"
            "       run firmware/build.sh, which copies its images there, or"
            " download them from the release - see firmware/README.md".format(pattern)
        )
    return part


def version_of(path: str) -> str:
    """The version a release image name carries, e.g. v1.29.0 (empty if it has none)."""
    found = re.search(r"v\d+\.\d+\.\d+", os.path.basename(path))
    return found.group(0) if found else ""


def agent_version() -> str:
    """This checkout the way git names it, the agent half of the merged image's name.

    ``git describe`` gives the tag when HEAD sits exactly on one (``v1.3.0``), and
    adds the commits since it when it does not (``v1.3.0-4-g79eac03``, or the bare
    commit id in a repo with no tags at all).  So a name that reads like a release
    *is* that release, and every other build says which commit it came from.
    """
    done = subprocess.run(
        ["git", "describe", "--tags", "--always"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if done.returncode != 0 or not done.stdout.strip():
        why = done.stderr.strip().splitlines()[0] if done.stderr.strip() else "git not found"
        raise SystemExit(
            "error: git cannot name this checkout - {}\n"
            "       the merged image is named after it - build inside the repository".format(why)
        )
    # A tag may carry characters a file name should not (/ in release/v1.0, say).
    return re.sub(r"[^A-Za-z0-9._+-]", "-", done.stdout.strip())


def merged_name(mpy_version: str) -> str:
    """The merged image's file name: the agent build, on a MicroPython version.

    Both halves are read off what is being shipped - the agent one from git, the
    MicroPython one from the firmware and model images - so the name cannot
    promise a build the file does not hold.
    """
    return "{}-{}-mpy{}.bin".format(PROJECT, agent_version(), mpy_version.removeprefix("v"))


def relpath(path: str) -> str:
    """<path> seen from the repository root, for readable output."""
    return os.path.relpath(path, REPO_ROOT)


def require(path: str) -> str:
    """Fail with a plain message when an image we were asked to read is missing."""
    if not os.path.exists(path):
        raise SystemExit("error: {} does not exist - run 'make app-fs' first".format(path))
    return path


def describe(path: str) -> str:
    """One built image as size plus digest: the fingerprint of a release build."""
    return "{}: {:,} bytes, sha256 {}".format(relpath(path), os.path.getsize(path), sha256(path))


def flash_hint(targets: str) -> str:
    """An esptool command line for <targets>, wrapped so it pastes as it is."""
    return (
        "flash: python -m esptool --chip esp32s3 -p $DEVICE -b 460800 \\\n"
        "         write_flash --compress {}".format(targets)
    )


def merge_flash(parts, output: str) -> None:
    """Write (offset, image) pairs into one padded image covering the whole flash."""
    parts = tuple(parts)
    for index, (offset, path) in enumerate(parts):
        size = os.path.getsize(path)
        limit = parts[index + 1][0] if index + 1 < len(parts) else FLASH_SIZE
        if offset + size > limit:
            raise SystemExit(
                "error: {} ({:,} bytes at 0x{:X}) overruns its partition by {:,} bytes".format(
                    os.path.basename(path), size, offset, offset + size - limit
                )
            )
    with open(output, "wb") as image:
        image.write(b"\xff" * FLASH_SIZE)  # erased flash, the state of a blank chip
        for offset, path in parts:
            image.seek(offset)
            with open(path, "rb") as part:
                shutil.copyfileobj(part, image)


def merged_parts(app_image: str, firmware: str, models: str) -> tuple:
    """The (offset, image) parts of one release image, and the MicroPython version they carry.

    That is the point of a merged image: the three-part flow asks a user without
    a toolchain to type three offsets into the browser flasher, and one wrong
    digit stops the board from booting.  The merged image covers nvs and both
    app slots, so it is a factory restore - the WiFi credentials, settings, chat
    memory and uploaded skills of a provisioned device land in its erased
    ranges.

    The parts are the copies in firmware/, which is where both a release download
    and `firmware/build.sh` leave them, and the version comes from their own
    names - so a release name cannot claim a MicroPython build the image does not
    hold.
    """
    firmware = firmware or require_part(FIRMWARE_GLOB)
    models = models or require_part(MODELS_GLOB)
    mpy_version = version_of(firmware)
    if not mpy_version or mpy_version != version_of(models):
        raise SystemExit(
            "error: {} and {} must come from one build and carry a version in"
            " their names ({} vs {})".format(
                os.path.basename(firmware),
                os.path.basename(models),
                version_of(firmware) or "no version in name",
                version_of(models) or "no version in name",
            )
        )
    return ((0, firmware), (VFS_OFFSET, app_image), (MODELS_OFFSET, models)), mpy_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_IMAGE,
        help="app filesystem image (default: {})".format(DEFAULT_IMAGE),
    )
    parser.add_argument(
        "--factory",
        action="store_true",
        help="merge firmware + app + models into firmware/<project>-<agent>-mpy<mpy>.bin",
    )
    parser.add_argument("--firmware", metavar="IMAGE", help="release firmware image to merge")
    parser.add_argument("--models", metavar="IMAGE", help="srmodels image to merge")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an existing image against the current sources, instead of rebuilding",
    )
    parser.add_argument(
        "--print-mpremote",
        action="store_true",
        help="print the mpremote deploy chain for the file set and exit",
    )
    parser.add_argument(
        "--ota",
        action="store_true",
        help="build firmware/<project>-ota-<agent>.tar for POST /ota, and print its path last",
    )
    parser.add_argument(
        "--skills",
        metavar="NAME[,NAME]",
        default="",
        help="skill folders under skills/ to add to the OTA bundle",
    )
    args = parser.parse_args()

    if args.print_mpremote:
        print(deploy_chain())
        return 0

    if args.ota:
        build_ota([name for name in args.skills.split(",") if name])
        return 0

    stage = stage_app()
    try:
        if args.factory or args.check:
            image = require(os.path.abspath(args.output))
        else:
            image = os.path.abspath(args.output)
            worker("mkfs", stage, image)
        # Never ship an image that has drifted from the sources: it is what a
        # user ends up with, and a user cannot fix it.
        check_image(image, stage)
        if args.factory:
            parts, mpy_version = merged_parts(image, args.firmware, args.models)
            merged = os.path.join(FIRMWARE_DIR, merged_name(mpy_version))
            merge_flash(parts, merged)
            print("one file, built from:")
            for offset, path in parts:
                print("  0x{:06X}  {}".format(offset, relpath(path)))
            print(describe(merged))
            print(flash_hint("0x0 {}".format(relpath(merged))))
        elif not args.check:
            # The three-file command, naming the copies firmware/ actually has, so
            # it pastes as it is from the repository root.
            firmware, models = newest(FIRMWARE_GLOB), newest(MODELS_GLOB)
            print(describe(image))
            print(
                flash_hint(
                    "0x0 {} 0x{:X} {} 0x{:X} {}".format(
                        relpath(firmware) if firmware else "firmware.bin",
                        VFS_OFFSET,
                        relpath(image),
                        MODELS_OFFSET,
                        relpath(models) if models else "srmodels.bin",
                    )
                )
            )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
