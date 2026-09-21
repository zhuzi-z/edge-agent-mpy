# Firmware

Custom MicroPython **v1.29.0** firmware for ESP32-S3 (N16R8), extended with an
ESP-SR user module (WakeNet wake word + AFE audio front-end running on
dedicated FreeRTOS tasks; Python API: `esp_sr.init/poll/on_event`, plus
`capture/read/capture_stats` for the AFE's VAD-gated utterance audio).

Pre-built images are published in this repository's **Releases** — download
them there if you don't want to rebuild. Each release ships the one file a user
needs (`edge-agent-mpy-v<agent>-mpy<mpy>.bin`, everything in one image) and the three
pieces it is made of: the merged firmware
(`ESP32_GENERIC_S3-SPIRAM_OCT-<date>-v<mpy>.bin`), the factory filesystem with
the agent app (`app-fs.bin`) and the WakeNet models (`srmodels-v<mpy>.bin`).
Those are also the names the local build gives them: `build.sh`, `make app-fs`
and `make factory-image` leave exactly that set of files here, so a release is
what a build produced under its own name, and nothing has to be renamed between
the two. The images are gitignored; only the build sources are tracked.

## Contents

| File | Description |
|------|-------------|
| `build.sh` | Clone MicroPython → apply patch → build |
| `espsr-integration.patch` | Adds ESP-SR user module, 16 MiB partition table with `model` partition, and the `SPIRAM_OCT_SR` board variant |
| `micropython/` | MicroPython source tree (created by `build.sh`) |
| `make_app_fs.py` | Builds the factory `app-fs.bin`: stages the deploy file set, calls the worker |
| `ota_push.py` | Pushes an OTA bundle to `POST /ota` and waits for the device to come back |
| `app_fs_lfs.py` | MicroPython worker: formats the littlefs volume, then mounts it back to verify it |
| `ESP32_GENERIC_S3-SPIRAM_OCT-<date>-v<mpy>.bin` (built) | `build.sh`'s firmware for `0x0`: bootloader + partition table + MicroPython |
| `srmodels-v<mpy>.bin` (built) | `build.sh`'s WakeNet models for `0xF00000` |
| `app-fs.bin` (built) | `make app-fs`: the agent app filesystem for `0x300000` |
| `edge-agent-mpy-v<agent>-mpy<mpy>.bin` (built) | The above three images merged into one whole-flash image for `0x0` |
| `edge-agent-mpy-ota-<agent>.tar` (built) | `make ota`: the app as an update bundle for `POST /ota` — no firmware, no `main.py`, see [../docs/ota.md](../docs/ota.md) |

## Build

Requires an ESP-IDF v5.5.x environment (recommended: v5.5.5). The easiest way
is Docker:

```bash
docker run --rm -v "$PWD":/firmware -w /firmware espressif/idf:v5.5.5 \
  bash -c "source /opt/esp/idf/export.sh && ./build.sh"
```

Whenever a build input's timestamp changes (`build.sh` re-applies the patch, a
`git checkout` in `micropython/` touches `idf_component.yml`) CMake
reconfigures, and the IDF component manager then needs github.com for its git
dependencies. On a host without a direct route, add host networking and the
local proxy:

```bash
docker run --rm --network host -e https_proxy=http://127.0.0.1:7890 \
  -e http_proxy=http://127.0.0.1:7890 -v "$PWD":/firmware -w /firmware \
  espressif/idf:v5.5.5 bash -c "source /opt/esp/idf/export.sh && ./build.sh"
```

`GH_PROXY=https://gh-proxy.com/` can be prepended to route github.com through
a proxy.

`build.sh` does four things:
1. Clone MicroPython v1.29.0 (skipped if already present)
2. Apply `espsr-integration.patch`
3. Build with `BOARD=ESP32_GENERIC_S3 BOARD_VARIANT=SPIRAM_OCT_SR`
4. Copy the two images that build produced into this directory, under the
   release names: `ESP32_GENERIC_S3-SPIRAM_OCT-<date>-v<mpy>.bin` from the
   build's `firmware.bin` (bootloader + partition table + app, already merged
   for `0x0`) and `srmodels-v<mpy>.bin` from its `srmodels/srmodels.bin`

Step 4 is what makes this directory hold the release set, and it is why
`make factory-image` can run straight after a build: that target merges these two
files plus `app-fs.bin`, so there is nothing to rename in between and a published
image carries the name the build gave it. The build tree keeps its own copies
under `micropython/ports/esp32/build-ESP32_GENERIC_S3-SPIRAM_OCT_SR/`, which is
what the `flash_args` in there refer to. A rebuild on a later day leaves the
earlier firmware file behind — its date is in its name, `make factory-image`
takes the newest, and the older ones are yours to delete.

## Flash

### One file (what a user should flash)

`edge-agent-mpy-v<agent>-mpy<mpy>.bin` covers the whole 16 MiB flash — bootloader,
partition table, MicroPython, the agent app and the wake-word models, at their
partition offsets. Both halves of the name describe the image: `<agent>` is the
edge-agent build it carries (`git describe` of the checkout), `<mpy>` the
MicroPython version inside it, taken from the parts it was merged from. One
file, one offset, nothing to mistype:

| File | Offset |
|------|--------|
| `edge-agent-mpy-v<agent>-mpy<mpy>.bin` | `0x0` |

```bash
python -m esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 \
    --flash_mode dio --flash_freq 80m --flash_size 16MB \
    write_flash --compress 0x0 edge-agent-mpy-v<agent>-mpy<mpy>.bin
```

It is a **factory restore**, not an update: the image holds the `nvs` and `vfs`
partitions too, so WiFi credentials, settings, chat memory and uploaded skills
end up erased. Only ~1.8 MiB of the 16 MiB is real content, and both esptool
(compression is on by default when the stub loader runs) and the browser
flasher deflate what they send, so the serial transfer stays small while the
whole flash is still rewritten. Build it with `make factory-image`.

No command line? Use this repository's own flasher page:
[`tools/web-flasher.html`](../tools/web-flasher.html) (Chrome/Edge) works opened
straight off the disk — no server, no toolchain. The release image is already on
the `0x0` row, the flash size is fixed at 16 MB, the read/write mode comes from
the image, and when the write finishes the page walks the user through resetting
the board and opening the provisioning hotspot. Espressif's generic
[ESP Web Flasher](https://espressif.github.io/esptool-js/) does the same write:
pick the release image at offset `0x0` and set the flash size to 16 MB.

### Three files, one layer at a time

The same content as three images, for when only one layer has to change — the
firmware after a code change, or `srmodels.bin` after enabling another wake
word. Interchangeable with the release image; `make factory-image` merges
exactly these three.

Download the files from the release and flash them at these offsets —
`srmodels.bin` is required, without it `esp_sr.init()` finds no wake-word
models, and `app-fs.bin` is optional (it pre-installs the agent app):

| File | Offset |
|------|--------|
| `ESP32_GENERIC_S3-SPIRAM_OCT-<date>-v<ver>.bin` | `0x0` |
| `app-fs.bin` | `0x300000` (optional) |
| `srmodels-v<ver>.bin` | `0xF00000` |

With [esptool](https://github.com/espressif/esptool) (`pip install esptool`):

```bash
python -m esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 \
    --flash_mode dio --flash_freq 80m --flash_size 16MB \
    write_flash --compress \
    0x0 ESP32_GENERIC_S3-SPIRAM_OCT-<date>-v<ver>.bin \
    0x300000 app-fs.bin \
    0xF00000 srmodels-v<ver>.bin
```

Port names: `/dev/ttyACM0` (Linux, native USB), `/dev/cu.usbmodem*` (macOS),
`COMx` (Windows, check Device Manager). Boards with a USB-UART bridge chip
show up as `/dev/ttyUSB0`; if flashing doesn't start, hold BOOT and tap
RESET to enter download mode. If the device already runs MicroPython you can
also enter download mode without buttons:
`mpremote connect /dev/ttyACM0 exec "import machine; machine.bootloader()"`,
then flash with `--before no_reset` (and press RESET or power-cycle
afterwards if esptool's auto-reset doesn't take). Optional: `erase-flash`
first for a fully clean device.

No command line? The flasher page has these three under **Per-partition images
(advanced)**, each pinned to its offset — a row you leave without a file simply
is not written, so updating one layer means picking one file.

What the page cannot do is supply the images. A real one-click install —
one link, one Program button, nothing to download — needs the images published
at plain URLs together with an
[esp-web-tools](https://github.com/espressif/esp-web-tools) manifest and a
small install page; this repository has no remote or release hosting yet, so
that step is still ahead of it.

Note: the firmware alone is just MicroPython — afterwards either deploy the
agent application over the serial port as described in the repository README
("Deploy the agent code"), or flash `app-fs.bin` (see below) which already
contains it.

### Self-built firmware

```bash
cd micropython/ports/esp32/build-ESP32_GENERIC_S3-SPIRAM_OCT_SR
python -m esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 write_flash @flash_args
```

## Factory Rootfs Image

`app-fs.bin` is a 12 MB **littlefs** image of the `vfs` partition that holds the
same file set `make deploy` pushes over the serial port (`main.py` and `app/`,
the WebUI and the builtin skills inside it). That list is `DEPLOYED` in
`make_app_fs.py`, which the `deploy` target also asks for
(`--print-mpremote`), so it is defined in exactly one place. `OTA_PAYLOAD` in
the same file is that set minus `main.py` — the recovery layer, which an update
must never replace — and is what `make ota` packs. Flash the image next to the
firmware and the board boots straight into the agent and opens the
`EdgeAgent-Setup` access point — no serial code push needed, which is what makes
a browser-only workflow (esptool-js can flash, but it cannot copy Python files
afterwards) work end to end.

```bash
make app-fs          # -> firmware/app-fs.bin, prints its sha256 and file summary
make app-fs-check    # verify an existing image against the current sources
make factory-image   # -> firmware/edge-agent-mpy-v<agent>-mpy<mpy>.bin, the three merged
```

The only build dependency is the MicroPython unix port (the same binary
`make test` uses, found through `MICROPY` or `PATH`) — no ESP-IDF checkout, no
extra host packages. `make_app_fs.py` (CPython) stages the file set into a
temporary directory and then runs `app_fs_lfs.py` twice:

```bash
micropython firmware/app_fs_lfs.py mkfs <staging_dir> <image>   # format + fill
micropython firmware/app_fs_lfs.py check <image> <staging_dir>  # mount + compare
```

`make app-fs` never hands out an image it cannot read back: `check` mounts it
the way the device's own `_boot.py` does — a bare block device, filesystem type
autodetected — and compares every name, size and byte, then prints `extra`,
`missing`, `bad size` or `corrupt` for anything that is off. `app-fs-check`
runs the same comparison, which is how a published image is traced back to the
sources it was built from.

The split between the two scripts is deliberate: building and reading the
volume has to happen in the *same littlefs implementation the device runs*,
which means MicroPython. Everything around it (argument parsing, staging,
hashing, reporting) is ordinary CPython.

### Why littlefs and not FAT

The `data, fat` subtype in the partition CSV only labels the partition for
ESP-IDF's own C API; MicroPython keys off the partition **name** — `inisetup.py`
formats `vfs` with `VfsLfs2.mkfs` and would need it to be called `ffat` to get a
FAT volume. FAT is not a workable alternative here either: MicroPython's flash
block device exposes the 4096 byte erase page as its block size and `ff.c`
requires `BPB_BytsPerSec` to equal it, so a FAT16 image with 512 byte sectors
fails to mount with `ENODEV`. `_boot.py` then falls back to `inisetup`, whose
`check_bootsec()` sees a non-blank block 0 and loops on "filesystem appears to
be corrupted" until the whole flash is erased.

Three geometry constants in `app_fs_lfs.py` have to stay in sync with the
device, and the `check` step is what catches them:

- `BLOCK_SIZE = 4096` — `NATIVE_BLOCK_SIZE_BYTES` in `esp32_partition.c`
- `VFS_SIZE = 12 MiB` — the `vfs` row of `partitions-16MiB-sr.csv`
- mount with `os.VfsLfs2(bdev)` defaults — `vfs_lfs.c` derives
  read/prog/lookahead 32, cache 128, block_cycles 100 from them, and the
  superblock written by `mkfs` has to match what the device expects

Rebuilding the same app tree gives the same bytes: littlefs stores a
modification time per file, so the worker mounts with `mtime=False` and the
image has no random or time-dependent field left (`sha256sum firmware/app-fs.bin`
is a real fingerprint of the sources). Names are stored verbatim, so unlike FAT
there is no 8.3 or lower-case mangling to design around.

Caveats:

- this is a **first-install / factory-reset** artifact: flashing it replaces
  everything in `vfs`, so the WiFi credentials and settings in `/config`, the
  chat memory in `/data` and the uploaded skills in `/skills` of an already
  provisioned device are gone. Updates to a device in use go over `make deploy`
  instead, which writes only `main.py` and `app/`, or over the network with
  `make ota` for a user without a serial port — see
  [../docs/ota.md](../docs/ota.md)
- the image is 12 MiB even though the app inside is ~300 KB: littlefs allocates
  blocks anywhere in the partition, so the file has to describe all of it, with
  the unused part already erased (0xFF) and ready to allocate. Flashing with
  `write_flash --compress` sends roughly 100 KB, so the transfer stays quick

### Merged Whole-Flash Image

`make factory-image` builds the single file the "One file" section above tells
users to flash. It runs `app-fs` first, verifies that image against the current
sources (a user cannot fix an image that drifted), then copies the firmware, the
app filesystem and the wake-word models into one 16 MiB file at their partition
offsets, leaving every other byte 0xFF — the state of a blank chip.

The two flash images it merges are the ones `build.sh` copied into this directory
(or the same files downloaded from a release): the newest
`ESP32_GENERIC_S3-SPIRAM_OCT-<date>-v<mpy>.bin` and `srmodels-v<mpy>.bin`, the
date in the name deciding which is newest. When this directory has neither, the
error says to run `build.sh` or fetch them from the release.

Three guards keep the merge honest. A part that does not fit its partition
aborts before anything is written. Firmware and models must carry the *same*
version in their file names, because pairing `v1.29.0` firmware with `v1.28.0`
models is exactly the mistake the merged file exists to prevent. And no part of
the output name is typed: `edge-agent-mpy-v<agent>-mpy<mpy>.bin` takes `<mpy>`
from those parts and `<agent>` from `git describe` of the agent checkout, so a
release name cannot promise a build the image does not hold. `--firmware` /
`--models` pick a part explicitly instead.

Nothing enters the image that is not in one of the three parts, so it is
reproducible: rebuilding from the same sources prints the same sha256.

## Partition Layout (16 MiB)

Defined in `ports/esp32/partitions-16MiB-sr.csv` (added by the patch):

| Name | Type | Offset | Size | Content |
|------|------|--------|------|---------|
| nvs | data | 0x9000 | 24 KB | NVS |
| phy_init | data | 0xF000 | 4 KB | PHY calibration |
| factory | app | 0x10000 | ~3 MB | micropython.bin |
| vfs | data/fat | 0x300000 | 12 MB | littlefs filesystem, `app-fs.bin` (`fat` is only an IDF-side label, see above) |
| model | data/spiffs | 0xF00000 | 1 MB | srmodels.bin (WakeNet models) |

## Wake Word Models

Configured in `espsr-integration.patch` via sdkconfig options:

```
CONFIG_SR_WN_WN9_HIESP=y      # "Hi ESP"
CONFIG_SR_WN_WN9_HILEXIN=y    # "Hi 乐鑫"
```

The ESP-SR component packs enabled models into `srmodels.bin` at build time
(via `movemodel.py`), which is then flashed to the `model` partition.
