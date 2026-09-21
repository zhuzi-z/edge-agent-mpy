"""Long-term memory: one durable markdown file carried across sessions.

When a conversation ends, a single LLM call folds what was learned into
MEMORY.md - the file is fed back into that call, so facts merge and stale
ones drop out. MEMORY.md is then injected into the system prompt.
"""

import app.config as config
import app.log as log
from app.util import ensure_dir

# One prompt serves the sync and the async path: the file is rewritten from
# the same inputs either way.
CURATOR_SYSTEM = (
    "You maintain a long-term memory file for an assistant running on a home "
    "device. Given the current file and a conversation that just ended, "
    "produce the updated file.\n"
    "Rules:\n"
    "- Keep it concise and well-organized (markdown format)\n"
    "- Add durable facts, preferences, and decisions\n"
    "- Keep stable identifiers (device IDs, model numbers, property mappings) "
    "verbatim when still relevant\n"
    "- Drop information the conversation contradicts\n"
    "- Do NOT include transient details (timestamps, greetings)\n"
    "- Output ONLY the file content, nothing else\n"
    "- If the current memory is empty, create it from scratch"
)


class MemoryManager:
    """Owns the memory file and the one LLM call that updates it."""

    def __init__(self, data_dir=None):
        base = (data_dir or config.DATA_DIR).rstrip("/")
        self._mem_dir = base + "/memory"
        ensure_dir(self._mem_dir)
        self._memory_path = self._mem_dir + "/MEMORY.md"
        # Process-lifetime cache: every LLM turn reads the file for the system
        # prompt, and write_memory is the only path that changes it.
        self._cached = None

    def read_memory(self):
        """Read the memory file. Returns empty string if missing."""
        if self._cached is not None:
            return self._cached
        try:
            with open(self._memory_path, "r") as f:
                self._cached = f.read()
        except OSError:
            return ""
        return self._cached

    def write_memory(self, content):
        """Overwrite MEMORY.md with new content."""
        try:
            with open(self._memory_path, "w") as f:
                f.write(content)
            self._cached = content
            return True
        except OSError as e:
            log.error("Memory", "write MEMORY.md failed: {}".format(e))
            return False

    def get_context(self):
        """Return the memory block for the system prompt, '' when there is none."""
        memory = self.read_memory()
        if not memory.strip():
            return ""
        context = "[Long-term Memory]\n" + memory.strip()
        if len(context) > config.MEMORY_CONTEXT_MAX_CHARS:
            context = "[older memory omitted]\n" + context[-config.MEMORY_CONTEXT_MAX_CHARS :]
        return context

    # -- Consolidation -----------------------------------------------------

    def consolidate(self, messages, llm_fn, session_key=""):
        """Fold a finished conversation into MEMORY.md. True if it was updated.

        Args:
            messages: list of message dicts from the conversation.
            llm_fn: callable(user_content, system_prompt) -> str.
            session_key: originating session, logged for provenance.
        """
        if not messages:
            return False
        prompt = self._curator_prompt(messages)
        try:
            updated = llm_fn(prompt, CURATOR_SYSTEM)
        except Exception as e:  # noqa: BLE001
            log.error("Memory", "consolidate LLM failed for {}: {}".format(session_key, e))
            return False
        return self._store(updated, messages, session_key)

    async def consolidate_async(self, messages, llm_fn, session_key=""):
        """Async variant of consolidate; ``llm_fn`` must be awaitable."""
        if not messages:
            return False
        prompt = self._curator_prompt(messages)
        try:
            updated = await llm_fn(prompt, CURATOR_SYSTEM)
        except Exception as e:  # noqa: BLE001
            log.error("Memory", "consolidate LLM failed for {}: {}".format(session_key, e))
            return False
        return self._store(updated, messages, session_key)

    # -- Helpers -----------------------------------------------------------

    def _curator_prompt(self, messages):
        """The curator's input: the current file plus the finished conversation."""
        current = self.read_memory()
        content = ""
        if current.strip():
            content += "Current MEMORY.md:\n```\n" + current + "\n```\n\n"
        return content + "Conversation to fold in:\n" + self._format_messages(messages)

    def _store(self, updated, messages, session_key):
        """Write what the LLM produced; a call that failed leaves the file alone."""
        if not updated or not updated.strip():
            log.info("Memory", "consolidate: nothing to store for {}".format(session_key))
            return False
        # Strip markdown code fences if the LLM wrapped the output.
        if not self.write_memory(self._strip_code_fence(updated.strip())):
            return False
        log.info(
            "Memory",
            "consolidated {} messages for {} -> {} chars".format(
                len(messages), session_key, len(updated)
            ),
        )
        return True

    @staticmethod
    def _strip_code_fence(text):
        if not text.startswith("```"):
            return text
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)

    @staticmethod
    def _format_messages(messages):
        """Format messages into a readable conversation transcript."""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if role == "tool":
                name = msg.get("name", "tool")
                content = "[{} result] {}".format(name, str(content)[:200])
            elif role == "assistant" and not content:
                # Skip empty assistant messages (tool-call-only)
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                    content = "[called tools: {}]".format(", ".join(names))
                else:
                    continue
            if content:
                lines.append("{}: {}".format(role, str(content)[:500]))
        return "\n".join(lines)
