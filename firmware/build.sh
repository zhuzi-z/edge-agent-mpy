#!/usr/bin/env bash
# Build MicroPython + ESP-SR firmware for ESP32-S3 (N16R8).
#
#   MicroPython v1.29.0 + esp_sr user module (AFE/WakeNet on dedicated
#   FreeRTOS tasks, Python API: esp_sr.init/poll/on_event).
#   ESP-SR v2.4.x is pulled automatically from the Espressif component
#   registry — no extra repos needed.
#
# Prerequisite: an ESP-IDF v5.5.x environment (`. $IDF_PATH/export.sh` done,
# idf.py in PATH). MicroPython v1.29.0 supports IDF v5.3–v5.5.x; the
# currently recommended version is v5.5.5.
#
# Usage:
#   ./build.sh                                # clone micropython (if missing), patch, build
#   GH_PROXY=https://gh-proxy.com/ ./build.sh # route github.com through a proxy

set -euo pipefail
cd "$(dirname "$0")"
ROOT=$PWD

MPY_VER=v1.29.0
MPY_DIR=${MPY_DIR:-$ROOT/micropython}
GH_PROXY=${GH_PROXY-}

command -v idf.py >/dev/null || {
    echo "error: idf.py not found - run '. \$IDF_PATH/export.sh' first" >&2
    exit 1
}

# Route github.com git traffic (incl. IDF component manager git deps such as
# espressif/tinyusb) through the proxy.
if [ -n "$GH_PROXY" ]; then
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0="url.${GH_PROXY}https://github.com/.insteadOf"
    export GIT_CONFIG_VALUE_0="https://github.com/"
fi

step() { echo -e "\n\033[1;32m==> $*\033[0m"; }

step "1/4 MicroPython $MPY_VER"
if ! git -C "$MPY_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    git clone --depth 1 --branch $MPY_VER \
        "${GH_PROXY}https://github.com/micropython/micropython.git" "$MPY_DIR"
fi

step "2/4 Apply ESP-SR patch"
cd "$MPY_DIR"
# Deterministic state: reset tracked files to the clean tag, drop files from a
# previous patch application, then apply.
git fetch --depth 1 origin tag $MPY_VER 2>/dev/null || true
git checkout -qf $MPY_VER
git clean -fdq ports/esp32/espsr \
    ports/esp32/boards/ESP32_GENERIC_S3/mpconfigvariant_SPIRAM_OCT_SR.cmake \
    ports/esp32/boards/ESP32_GENERIC_S3/sdkconfig.sr \
    ports/esp32/partitions-16MiB-sr.csv 2>/dev/null || true
git apply "$ROOT/espsr-integration.patch"
# Stage the patched lockfile so the port's "lockfile dirty" build check
# (git diff on dependencies.lock.*) sees a clean tree; a genuine drift during
# the build (e.g. wrong IDF version) still shows up.
git add ports/esp32/lockfiles/dependencies.lock.esp32s3

step "3/4 Build (BOARD=ESP32_GENERIC_S3 BOARD_VARIANT=SPIRAM_OCT_SR)"
make -C ports/esp32 BOARD=ESP32_GENERIC_S3 BOARD_VARIANT=SPIRAM_OCT_SR submodules
make -C mpy-cross -j"$(nproc)"
make -C ports/esp32 BOARD=ESP32_GENERIC_S3 BOARD_VARIANT=SPIRAM_OCT_SR -j"$(nproc)"

BUILD=$MPY_DIR/ports/esp32/build-ESP32_GENERIC_S3-SPIRAM_OCT_SR

# The two images under the names the releases use, so what lands in firmware/ is
# both the set to upload and the set `make factory-image` merges - nothing to
# rename by hand, and no way for a release name to differ from a local build's.
# firmware.bin is already bootloader + partition table + app merged for 0x0; the
# date keeps two rebuilds of one MicroPython version apart.
step "4/4 Copy the flash images to firmware/"
for part in "$BUILD/firmware.bin" "$BUILD/srmodels/srmodels.bin"; do
    test -f "$part" || { echo "error: the build produced no $part" >&2; exit 1; }
done
FIRMWARE=$ROOT/ESP32_GENERIC_S3-SPIRAM_OCT-$(date +%Y%m%d)-$MPY_VER.bin
MODELS=$ROOT/srmodels-$MPY_VER.bin
cp "$BUILD/firmware.bin" "$FIRMWARE"
cp "$BUILD/srmodels/srmodels.bin" "$MODELS"

echo
echo "Build OK: $FIRMWARE"
echo "          $MODELS"
echo "Flash:    cd $BUILD && python -m esptool --chip esp32s3 write_flash \"@flash_args\""
echo "Merge:    make factory-image   # one file for 0x0, run from the repository root"
