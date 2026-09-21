"""ChatStore persistence tests."""

import compat  # noqa: F401
from helpers import mkdtemp, rmtree

import unittest
from app.storage.chat import ChatStore


class TestChatStore(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp(prefix="xzchat_")
        # Nested data dir: ChatStore must create missing parents.
        self.store = ChatStore(data_dir=self.tmp + "/deep/nested")

    def tearDown(self):
        rmtree(self.tmp)

    def test_append_load_clear_compact_lifecycle(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "Hello world 🌍"},
        ]
        self.assertTrue(self.store.append("web:default", msgs))
        loaded = self.store.load("web:default")
        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded[0]["content"], "hello")
        self.assertEqual(loaded[2]["content"], "Hello world 🌍")
        # clear() removes the session file.
        self.assertTrue(self.store.clear("web:default"))
        self.assertEqual(self.store.load("web:default"), [])
        # compact() rewrites the file keeping only the given tail.
        for i in range(10):
            self.store.append("s1", [{"role": "user", "content": "m%d" % i}])
        self.assertEqual(self.store.message_count("s1"), 10)
        keep = [{"role": "user", "content": "m8"}, {"role": "user", "content": "m9"}]
        self.assertTrue(self.store.compact("s1", keep))
        loaded = self.store.load("s1")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["content"], "m8")
        self.assertEqual(self.store.message_count("s1"), 2)

    def test_max_messages_skips_orphan_tool(self):
        msgs = [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "1", "name": "t", "content": "result"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "q2"},
        ]
        self.store.append("s1", msgs)
        loaded = self.store.load("s1", max_messages=3)
        self.assertNotEqual(loaded[0]["role"], "tool")


if __name__ == "__main__":
    unittest.main(globals())
