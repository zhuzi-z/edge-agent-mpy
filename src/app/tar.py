"""A read-only ustar reader: the subset of tar that ``make ota`` writes.

MicroPython has no ``tarfile``, and reading an update bundle only ever needs two
things - walk the headers, copy a member out - so that is all this is.  No
writing, no compression, no hard links, no checksum verification: the sha256 in
the bundle's manifest is what vouches for a file's contents, and a header this
cannot make sense of is a bundle that is not a bundle.

Nothing here knows what an update is.  Members are handed back as
``(name, size, offset, is_file)`` and read straight off the file at that offset,
so a bundle is parsed without ever being held in memory.
"""

import hashlib
import os

BLOCK = 512
CHUNK = 4096
# The header fields this reads: name, size (octal ASCII), type flag, ustar's
# long-name prefix, and the magic that says the block is a header at all.
_NAME, _SIZE, _TYPE, _PREFIX, _MAGIC = (0, 100), (124, 136), (156, 157), (345, 500), (257, 262)
# Type flags that mean "a file": "0", and the NUL an old tar writer leaves.
_FILE_FLAGS = (b"0", b"\0", b"")


class TarError(Exception):
    """Not a tar this can read - worded for whoever uploaded it."""


def _field(block, span):
    """One NUL-terminated, space-padded header field as text."""
    return block[span[0] : span[1]].split(b"\0")[0].strip().decode()


def _header(block):
    """(name, size, is_file) of one header block, or None at the end of the tar.

    A block that is not a header raises TarError: whatever else came out of it -
    a ValueError from the octal size field, say - would not say what is actually
    wrong with the file.
    """
    if len(block) < BLOCK or block == b"\0" * BLOCK:
        return None  # a short read (end of file) or the zero block that ends a tar
    if block[_MAGIC[0] : _MAGIC[1]] != b"ustar":
        raise TarError("this is not a tar bundle")
    try:
        name = _field(block, _NAME)
        prefix = _field(block, _PREFIX)
        size = int(_field(block, _SIZE) or "0", 8)
    except (UnicodeError, ValueError):
        raise TarError("this bundle has a header this device cannot read")
    if size < 0:
        raise TarError("this bundle claims a {} byte file".format(size))
    if prefix:
        name = prefix + "/" + name  # ustar splits a long name in two
    return name, size, block[_TYPE[0] : _TYPE[1]] in _FILE_FLAGS


def members(path):
    """Walk a tar's headers: (name, size, data_offset, is_file) per member.

    Only the 512 byte headers are read; the data between them is skipped with a
    seek, so walking a bundle costs nothing per byte of its contents.
    """
    with open(path, "rb") as handle:
        at = 0
        while True:
            handle.seek(at)
            header = _header(handle.read(BLOCK))
            if header is None:
                return
            name, size, is_file = header
            yield name, size, at + BLOCK, is_file
            at += BLOCK + size + (-size % BLOCK)  # data, padded to a block


def read(path, member):
    """One member whole, as bytes - for the small ones (a manifest)."""
    name, size, offset, _is_file = member
    # Against the file's own size first: read() would otherwise try to allocate
    # whatever a lying header claims, and there is no second chance after that.
    if offset + size > os.stat(path)[6]:
        raise TarError("the bundle ends inside {}".format(name))
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(size)
    return data


def extract(path, member, target):
    """Copy one member out to <target>, in chunks.  Returns its sha256 hex.

    Digesting on the way out is the point of doing it here: the caller has to
    compare the digest anyway, and this way the member is neither read twice nor
    ever in memory.  Parent directories are the caller's business.
    """
    name, size, offset, _is_file = member
    digest = hashlib.sha256()
    with open(path, "rb") as source, open(target, "wb") as written:
        source.seek(offset)
        left = size
        while left > 0:
            block = source.read(min(CHUNK, left))
            if not block:
                raise TarError("the bundle ends inside {}".format(name))
            digest.update(block)
            written.write(block)
            left -= len(block)
    return digest.digest().hex()
