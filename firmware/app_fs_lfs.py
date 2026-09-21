# Build and verify the littlefs volume of the ESP32-S3 ``vfs`` partition image.
#
#     micropython firmware/app_fs_lfs.py mkfs <staging_dir> <image>
#     micropython firmware/app_fs_lfs.py check <image> [<staging_dir>]
#
# Runs on the host with the MicroPython unix port on purpose: ``mkfs`` formats
# and fills the volume through the same littlefs build and the same geometry
# derivation the device uses, so what is written is what the board reads.  The
# volume is littlefs because MicroPython's own inisetup formats the partition
# labelled ``vfs`` as ``VfsLfs2``, whose block size is the 4096 byte erase page
# -- a FAT image cannot be mounted at all (see firmware/README.md).
#
# ``check`` mounts the image the way the device's _boot.py does: a bare block
# device, filesystem type autodetected.  With a staging directory it compares
# every name, size and byte, which is how a published image is traced back to
# the sources it was built from.
import os
import sys

BLOCK_SIZE = 4096  # esp32.Partition erase page, the geometry the device reports
VFS_SIZE = 12 * 1024 * 1024  # vfs partition size in partitions-16MiB-sr.csv
MOUNT_POINT = "/image"  # scratch mount inside this process, not a device path
IOCTL_BLOCK_COUNT = 4
IOCTL_BLOCK_SIZE = 5
S_IFDIR = 0o40000
STATVFS_BAVAIL = 4
CHUNK = 4096


class ImageBlockDev:
    """The image file as a block device with the vfs partition's geometry."""

    def __init__(self, path, create=False):
        if create:
            # Block by block: the unix port heap is far smaller than the volume.
            erased = b"\xff" * BLOCK_SIZE
            with open(path, "wb") as handle:
                for _ in range(VFS_SIZE // BLOCK_SIZE):
                    handle.write(erased)
        self.file = open(path, "r+b")
        self.blocks = os.stat(path)[6] // BLOCK_SIZE

    def readblocks(self, index, buf, offset=0):
        self.file.seek(index * BLOCK_SIZE + offset)
        if self.file.readinto(buf) != len(buf):
            raise OSError("short read at block {}".format(index))

    def writeblocks(self, index, buf, offset=0):
        # The device erases before it writes; filling a freshly formatted image
        # touches every block exactly once, so writing straight through matches.
        self.file.seek(index * BLOCK_SIZE + offset)
        self.file.write(buf)

    def ioctl(self, op, arg):
        if op == IOCTL_BLOCK_COUNT:
            return self.blocks
        if op == IOCTL_BLOCK_SIZE:
            return BLOCK_SIZE
        return 0


def list_tree(directory, prefix=""):
    """{relative path: size} of a directory, on the host or inside the image."""
    files = {}
    for name in sorted(os.listdir(directory)):
        path = directory + "/" + name
        info = os.stat(path)
        if info[0] & S_IFDIR:
            files.update(list_tree(path, prefix + name + "/"))
        else:
            files[prefix + name] = info[6]
    return files


def same_bytes(one, two):
    """Compare two files in chunks, so the image never has to be in memory."""
    with open(one, "rb") as left, open(two, "rb") as right:
        while True:
            data = left.read(CHUNK)
            if data != right.read(CHUNK):
                return False
            if not data:
                return True


def make_dirs(path):
    for index in range(1, len(path.split("/"))):
        parent = "/".join(path.split("/")[:index])
        try:
            os.mkdir(parent)
        except OSError:
            pass


def mkfs(staging, image):
    """Format <image> as the device would and copy the staging tree into it."""
    bdev = ImageBlockDev(image, create=True)
    os.VfsLfs2.mkfs(bdev)
    # mtime=False: littlefs stores a modification time per file, which would
    # give a different image on every run for the same input tree.
    os.mount(os.VfsLfs2(bdev, mtime=False), MOUNT_POINT)
    try:
        for path in sorted(list_tree(staging)):
            target = MOUNT_POINT + "/" + path
            make_dirs(target)
            with open(staging + "/" + path, "rb") as source:
                with open(target, "wb") as copied:
                    while True:
                        data = source.read(CHUNK)
                        if not data:
                            break
                        copied.write(data)
        filled = list_tree(MOUNT_POINT)
    finally:
        os.umount(MOUNT_POINT)
    print("{} files, {} bytes".format(len(filled), sum(filled.values())))


def check(image, staging=None):
    """Mount the image like the device does and compare it with <staging>."""
    os.mount(ImageBlockDev(image), MOUNT_POINT)
    problems = []
    try:
        found = list_tree(MOUNT_POINT)
        expected = list_tree(staging) if staging else None
        for path in sorted(found):
            print("{}\t{}".format(path, found[path]))
            if expected is None:
                continue
            if path not in expected:
                problems.append("extra     {}".format(path))
            elif found[path] != expected[path]:
                problems.append(
                    "bad size  {} ({} of {})".format(path, found[path], expected[path])
                )
            elif not same_bytes(MOUNT_POINT + "/" + path, staging + "/" + path):
                problems.append("corrupt   {}".format(path))
        if expected is not None:
            problems += [
                "missing   {}".format(path) for path in sorted(expected) if path not in found
            ]
        free = os.statvfs(MOUNT_POINT)[STATVFS_BAVAIL] * BLOCK_SIZE
    finally:
        os.umount(MOUNT_POINT)
    for problem in problems:
        print("ERROR " + problem)
    print("{} files, {} bytes, {} bytes free".format(len(found), sum(found.values()), free))
    return 1 if problems else 0


def main():
    args = sys.argv[1:]
    if args[0:1] == ["mkfs"] and len(args) == 3:
        mkfs(args[1], args[2])
        return 0
    if args[0:1] == ["check"] and len(args) in (2, 3):
        return check(args[1], args[2] if len(args) == 3 else None)
    print("usage: micropython {} mkfs <staging_dir> <image>".format(sys.argv[0]))
    print("       micropython {} check <image> [<staging_dir>]".format(sys.argv[0]))
    return 2


sys.exit(main())
