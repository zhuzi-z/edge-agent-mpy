"""MemoryManager tests: the memory file, prompt injection, consolidation."""

import asyncio
import compat  # noqa: F401
from helpers import mkdtemp, rmtree

import unittest
from app.storage.memory import MemoryManager
import app.config as config


class TestMemoryFile(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp(prefix="xzmem_")
        self.mem = MemoryManager(data_dir=self.tmp)

    def tearDown(self):
        rmtree(self.tmp)

    def test_read_write_and_context(self):
        self.assertEqual(self.mem.read_memory(), "")
        self.assertEqual(self.mem.get_context(), "")
        self.mem.write_memory("# Facts\n- User likes tea")
        self.assertIn("User likes tea", self.mem.read_memory())
        ctx = self.mem.get_context()
        self.assertIn("Long-term Memory", ctx)
        self.assertIn("User likes tea", ctx)

        original_limit = config.MEMORY_CONTEXT_MAX_CHARS
        config.MEMORY_CONTEXT_MAX_CHARS = 40
        try:
            self.mem.write_memory("x" * 200)
            bounded = self.mem.get_context()
            self.assertLessEqual(len(bounded), 40 + len("[older memory omitted]\n"))
            self.assertIn("[older memory omitted]", bounded)
        finally:
            config.MEMORY_CONTEXT_MAX_CHARS = original_limit


class TestConsolidate(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp(prefix="xzmem_")
        self.mem = MemoryManager(data_dir=self.tmp)
        self.prompts = []

    def tearDown(self):
        rmtree(self.tmp)

    def _llm(self, user_content, system_prompt):
        self.prompts.append(user_content)
        return "# Memory\n- User likes coffee"

    def test_consolidate_rewrites_memory(self):
        msgs = [
            {"role": "user", "content": "I like coffee"},
            {"role": "assistant", "content": "Noted!"},
        ]
        self.assertTrue(self.mem.consolidate(msgs, self._llm, "web:default"))
        self.assertEqual(self.mem.read_memory(), "# Memory\n- User likes coffee")
        # The current file and the conversation both go into the one LLM call,
        # so a second pass merges rather than replaces blindly.
        self.mem.write_memory("- Old fact worth keeping")
        self.mem.consolidate(msgs, self._llm, "web:default")
        self.assertIn("- Old fact worth keeping", self.prompts[-1])
        self.assertIn("user: I like coffee", self.prompts[-1])

    def test_consolidate_strips_fences_and_survives_failure(self):
        def fenced(user_content, system_prompt):
            return "```markdown\n# Memory\n- fact\n```"

        self.mem.consolidate([{"role": "user", "content": "hi"}], fenced)
        self.assertFalse(self.mem.read_memory().startswith("```"))

        def failing(user_content, system_prompt):
            raise RuntimeError("LLM down")

        self.mem.write_memory("# Keep me")
        self.assertFalse(self.mem.consolidate([{"role": "user", "content": "hi"}], failing))
        self.assertEqual(self.mem.read_memory(), "# Keep me")

    def test_consolidate_async(self):
        async def llm(user_content, system_prompt):
            return "- Async fact"

        updated = asyncio.run(self.mem.consolidate_async([{"role": "user", "content": "hi"}], llm))
        self.assertTrue(updated)
        self.assertEqual(self.mem.read_memory(), "- Async fact")


if __name__ == "__main__":
    unittest.main(globals())
