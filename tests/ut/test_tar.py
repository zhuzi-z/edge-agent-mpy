"""app/tar.py: reading the uncompressed ustar bundles `make ota` writes."""

import compat  # noqa: F401

import hashlib
import unittest

import app.tar as tar
from helpers import mkdtemp, rmtree, tar_entry

MANIFEST = b'{"version": "v2", "files": {}}'
END = b"\0" * 1024  # the two zero blocks that end a tar
JUNK = b"not a tar header" + b" " * (512 - 16)  # a full block that is no header


class TarCase(unittest.TestCase):
    """Writes a bundle into a throwaway directory and reads it back."""

    def setUp(self):
        self.root = mkdtemp("tar_")
        self.path = self.root + "/bundle.tar"

    def tearDown(self):
        rmtree(self.root)

    def _put(self, data):
        """<data> on flash, at the path the reader is pointed at."""
        with open(self.path, "wb") as handle:
            handle.write(data)
        return self.path

    def _members(self, data):
        return list(tar.members(self._put(data)))


class TestMembers(TarCase):
    def test_the_headers_give_names_sizes_offsets_and_kinds(self):
        """A walk reads only the headers, and says where each member's data is."""
        files = {"manifest.json": MANIFEST, "app/main.py": b"new = True\n"}
        data = (
            tar_entry("app/", b"", kind=b"5")  # a directory: the flag says so, not the size
            + tar_entry("manifest.json", MANIFEST)
            + tar_entry("app/main.py", files["app/main.py"])
            + END
        )
        found = self._members(data)
        self.assertEqual(
            [(name, size, is_file) for name, size, _offset, is_file in found],
            [
                ("app/", 0, False),
                ("manifest.json", len(MANIFEST), True),
                ("app/main.py", 11, True),
            ],
        )
        # The offsets are the point of the walk: each one really is that member.
        for member in found:
            if member[3]:
                self.assertEqual(tar.read(self.path, member), files[member[0]])

    def test_a_long_name_is_joined_from_the_prefix(self):
        """ustar splits a name too long for its own field; the reader joins it back."""
        found = self._members(tar_entry("main.py", b"x = 1\n", prefix="app/a/long/way/down") + END)
        self.assertEqual([member[0] for member in found], ["app/a/long/way/down/main.py"])

    def test_the_zero_blocks_end_the_walk(self):
        """Nothing past the terminator is looked at, and a header has to say ustar."""
        self.assertEqual(self._members(END), [])  # a tar carrying no members
        self.assertEqual(self._members(b""), [])  # nor one that is simply empty
        with self.assertRaises(tar.TarError):
            self._members(JUNK + END)


class TestReading(TarCase):
    def test_read_refuses_a_member_that_runs_past_the_end(self):
        """A header may claim any size; the file's own length is what counts."""
        truncated = self._put(tar_entry("app/main.py", b"x" * 100)[: 512 + 10])
        member = next(iter(tar.members(truncated)))
        self.assertEqual(member[1], 100)  # the header still claims all of it
        with self.assertRaises(tar.TarError):
            tar.read(truncated, member)

    def test_extract_writes_the_file_and_digests_it_on_the_way(self):
        """One pass: the member on flash, and the sha256 the manifest is checked against."""
        payload = bytes(range(256)) * 40  # 10 KiB, so the chunked copy has to loop
        path = self._put(tar_entry("app/blob.bin", payload) + END)
        member = next(iter(tar.members(path)))
        out = self.root + "/blob.bin"
        self.assertEqual(tar.extract(path, member, out), hashlib.sha256(payload).digest().hex())
        with open(out, "rb") as handle:
            self.assertEqual(handle.read(), payload)  # and no block padding came along

    def test_extract_stops_at_a_short_read(self):
        """A bundle cut off mid-member is an error, not a quietly short file."""
        path = self._put(tar_entry("app/main.py", b"x" * 100)[: 512 + 10])
        member = next(iter(tar.members(path)))
        with self.assertRaises(tar.TarError):
            tar.extract(path, member, self.root + "/main.py")


if __name__ == "__main__":
    unittest.main(globals())
