"""Message bus: aggregates inbound messages, dispatches replies, isolates sessions.

The bus is the text-only boundary between channels and the agent.
Each channel+chat_id combination gets its own conversation history.
History is persisted via ChatStore and consolidated via MemoryManager.
Slash commands are intercepted before reaching the agent, following the
nanobot command pattern. Commands are pluggable: use register_command()
(or the command() decorator) to add new ones; /help is generated from
the registry automatically.
"""

import time
import json
import app.config as config
import app.log as log
from app.channel import Message, Reply
from app.util import ms_since, run_sync


def _trim_history(history, limit=None):
    """Trim to the last ``limit`` messages without orphaning tool calls.

    OpenAI-compatible APIs reject a window that starts with a ``tool``
    message or holds an assistant tool-call message whose results were
    trimmed away, so advance the window start past such messages.
    """
    if limit is None:
        limit = config.AGENT_HISTORY_MAX
    if len(history) <= limit:
        return history
    trimmed = history[-limit:]
    while trimmed:
        first = trimmed[0]
        if first.get("role") == "tool":
            trimmed.pop(0)
            continue
        if first.get("role") == "assistant" and first.get("tool_calls"):
            ids = [tc.get("id") for tc in first.get("tool_calls") or []]
            results = {m.get("tool_call_id") for m in trimmed if m.get("role") == "tool"}
            if all(i in results for i in ids):
                break
            trimmed.pop(0)
            continue
        break
    return trimmed


class Bus:
    """Routes messages between channels and the agent with session isolation."""

    def __init__(self, agent, chat_store=None, memory=None, session_cfg_fn=None):
        self._agent = agent
        self._chat_store = chat_store
        self._memory = memory
        self._sessions = {}
        self._last_active = {}  # session key -> ticks_ms of last message
        self._session_cfg_fn = session_cfg_fn
        self._commands = {}
        self._command_names = []  # registration order (MicroPython dict is unordered)
        self._register_builtins()

    # -- Command registry ----------------------------------------------------

    def register_command(self, name, description, handler, usage=None):
        """Register a slash command. Re-registering a name overrides it.

        name: command name, leading slash optional (e.g. "/new" or "new").
        description: one-line help text shown in /help and command listings.
        handler: callable(message, raw_text) returning a Reply.
        usage: optional usage hint shown instead of name (e.g. "/history [n]").
        """
        if not callable(handler):
            raise ValueError("command handler must be callable")
        if not name.startswith("/"):
            name = "/" + name
        name = name.lower()
        if name not in self._commands:
            self._command_names.append(name)
        self._commands[name] = {
            "name": name,
            "usage": usage or name,
            "description": description,
            "handler": handler,
        }

    def command(self, name, description, usage=None):
        """Decorator form of register_command.

        Example:
            @bus.command("/ping", "Check the agent is alive")
            def _ping(message, raw):
                return Reply(content="pong")
        """

        def decorate(fn):
            self.register_command(name, description, fn, usage=usage)
            return fn

        return decorate

    def _register_builtins(self):
        self.register_command("/new", "Clear conversation and start a new session", self._cmd_new)
        self.register_command(
            "/history",
            "View the last n conversation records",
            self._cmd_history,
            usage="/history [n]",
        )
        self.register_command("/status", "View device and memory status", self._cmd_status)
        self.register_command("/help", "Show this help", self._cmd_help)

    def _load_session(self, key):
        """Load session from memory cache or persistent store."""
        history = self._sessions.get(key)
        if history is not None:
            return history
        if self._chat_store:
            history = self._chat_store.load(key, max_messages=config.AGENT_HISTORY_MAX)
        else:
            history = []
        self._sessions[key] = history
        return history

    def _session_timeout_ms(self):
        """Idle timeout in milliseconds. The agent config key
        session_timeout_min overrides the default; 0 disables expiry."""
        sec = config.SESSION_IDLE_TIMEOUT_SEC
        if self._session_cfg_fn:
            try:
                minutes = (self._session_cfg_fn() or {}).get("session_timeout_min")
                if minutes is not None:
                    sec = int(minutes) * 60
            except Exception:
                sec = config.SESSION_IDLE_TIMEOUT_SEC
        if sec <= 0:
            return 0
        return sec * 1000

    def _expire_if_idle(self, key):
        """Drop a session that has been silent longer than the timeout,
        so the next message starts a fresh conversation."""
        last = self._last_active.get(key)
        if last is None:
            return
        timeout_ms = self._session_timeout_ms()
        if not timeout_ms or time.ticks_diff(time.ticks_ms(), last) < timeout_ms:
            return
        if not self._sessions.get(key) and not (
            self._chat_store and self._chat_store.message_count(key)
        ):
            # Nothing to archive. Drop the bookkeeping anyway, or an empty key
            # would linger forever and the sweep would keep re-selecting it.
            self._sessions.pop(key, None)
            self._last_active.pop(key, None)
            return
        log.info("Bus", "session {} expired after idle, starting new session".format(key))
        self._maybe_consolidate(key, session_ending=True)
        self._sessions.pop(key, None)
        self._last_active.pop(key, None)
        if self._chat_store:
            self._chat_store.clear(key)

    async def _expire_if_idle_async(self, key):
        """Async expiry path used by the main-loop maintenance pass."""
        last = self._last_active.get(key)
        if last is None:
            return
        timeout_ms = self._session_timeout_ms()
        if not timeout_ms or time.ticks_diff(time.ticks_ms(), last) < timeout_ms:
            return
        if not self._sessions.get(key) and not (
            self._chat_store and self._chat_store.message_count(key)
        ):
            self._sessions.pop(key, None)
            self._last_active.pop(key, None)
            return
        log.info("Bus", "session {} expired after idle, starting new session".format(key))
        await self._maybe_consolidate_async(key, session_ending=True)
        self._sessions.pop(key, None)
        self._last_active.pop(key, None)
        if self._chat_store:
            self._chat_store.clear(key)

    def sweep_idle(self, max_sessions=None):
        """Expire every session that has been silent past the timeout.

        ``_expire_if_idle`` only ever looks at the key a new message arrived
        for, so a session belonging to someone who never messages again (a
        WeChat contact, say) keeps its history in RAM for the life of the
        process. The main loop calls this periodically to reclaim those.
        Returns the number of sessions handled.

        Work per pass is capped: expiring a session can trigger memory
        consolidation, which is a blocking LLM call, and this runs on the
        event-loop thread.
        """
        timeout_ms = self._session_timeout_ms()
        if not timeout_ms or not self._last_active:
            return 0
        if max_sessions is None:
            max_sessions = config.SESSION_SWEEP_MAX_PER_PASS
        now = time.ticks_ms()
        expired = [
            key
            for key, last in self._last_active.items()
            if time.ticks_diff(now, last) >= timeout_ms
        ]
        for key in expired[:max_sessions]:
            self._expire_if_idle(key)
        return len(expired[:max_sessions])

    async def sweep_idle_async(self, max_sessions=None):
        """Async sweep; consolidation runs off the event-loop thread."""
        timeout_ms = self._session_timeout_ms()
        if not timeout_ms or not self._last_active:
            return 0
        if max_sessions is None:
            max_sessions = config.SESSION_SWEEP_MAX_PER_PASS
        now = time.ticks_ms()
        expired = [
            key
            for key, last in self._last_active.items()
            if time.ticks_diff(now, last) >= timeout_ms
        ]
        for key in expired[:max_sessions]:
            await self._expire_if_idle_async(key)
        return len(expired[:max_sessions])

    def _command_reply(self, message):
        """Handle a slash command. Returns a Reply, or None for normal text."""
        text = message.content.strip()
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            entry = self._commands.get(cmd)
            if entry:
                return entry["handler"](message, text)
        return None

    async def _command_reply_async(self, message):
        """Handle slash commands; slow commands yield to the event loop."""
        text = message.content.strip()
        if text.startswith("/"):
            command = text.split()[0].lower()
            if command == "/new":
                return await self._cmd_new_async(message)
            entry = self._commands.get(command)
            if entry:
                return entry["handler"](message, text)
        return None

    def _dispatch_prepare(self, message):
        """Expire/load/append the session for a normal message."""
        t = time.ticks_ms()
        key = message.session_key
        self._expire_if_idle(key)
        history = self._load_session(key)
        t_load = time.ticks_ms()
        self._last_active[key] = time.ticks_ms()

        user_msg = {"role": "user", "content": message.content}
        history.append(user_msg)
        if self._chat_store:
            self._chat_store.append(key, [user_msg])
        # Both of these touch littlefs on a cold session and happen before the
        # LLM request, so they are invisible in the Agent line; a first turn
        # was measured paying ~1s here.
        log.timing(
            "Bus",
            "session load={}ms persist={}ms".format(time.ticks_diff(t_load, t), ms_since(t_load)),
        )

        if len(history) > config.AGENT_HISTORY_MAX:
            history = _trim_history(history)
            self._sessions[key] = history
        return key, history

    def _finalize_base(self, key, history, assistant_msgs, reply_text, error):
        """Persist assistant messages and extract voice-exit metadata."""

        for msg in assistant_msgs:
            history.append(msg)
        if self._chat_store and assistant_msgs:
            self._chat_store.append(key, assistant_msgs)

        if len(history) > config.AGENT_HISTORY_MAX:
            self._sessions[key] = _trim_history(history)

        metadata = None
        for msg in assistant_msgs:
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") != config.VOICE_CONTROL_SKILL:
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                # Only the exit action ends the conversation (a missing
                # action means the original exit-only schema); other
                # actions like volume just run and the talk goes on.
                if args.get("action", "exit") != "exit":
                    continue
                metadata = {"exit_voice": True, "exit_ack": str(args.get("ack") or "")}
                break
            if metadata:
                break
        return Reply(
            content=reply_text or "", error=error, metadata=metadata
        ), key if metadata else None

    def _finalize(self, key, history, assistant_msgs, reply_text, error):
        """Persist replies; consolidation runs only when voice exits."""
        reply, pending_key = self._finalize_base(key, history, assistant_msgs, reply_text, error)
        if pending_key:
            self._maybe_consolidate(pending_key)
        return reply

    async def _finalize_async(self, key, history, assistant_msgs, reply_text, error):
        """Persist replies and run exit consolidation off the event loop."""
        reply, pending_key = self._finalize_base(key, history, assistant_msgs, reply_text, error)
        if pending_key:
            await self._maybe_consolidate_async(pending_key)
        return reply

    def dispatch_sync(self, message):
        """Route a text message synchronously (voice-channel thread)."""
        reply = self._command_reply(message)
        if reply is not None:
            return reply

        key, history = self._dispatch_prepare(message)
        reply_text, error, assistant_msgs = self._agent.reply(
            message.content, history, channel=message.channel
        )
        return self._finalize(key, history, assistant_msgs, reply_text, error)

    async def dispatch(self, message):
        """Route a text message async (web/Weixin channels)."""
        reply = await self._command_reply_async(message)
        if reply is not None:
            return reply

        key, history = self._dispatch_prepare(message)
        reply_text, error, assistant_msgs = await self._agent.reply_async(
            message.content, history, channel=message.channel
        )
        return await self._finalize_async(key, history, assistant_msgs, reply_text, error)

    # -- Slash commands ----------------------------------------------------

    def _cmd_new(self, message, raw):
        """Clear session and start a fresh conversation."""
        self.clear_session(message.channel, message.chat_id)
        return Reply(content="✅ New conversation started.")

    async def _cmd_new_async(self, message, raw=None):
        await self.clear_session_async(message.channel, message.chat_id)
        return Reply(content="✅ New conversation started.")

    def _cmd_history(self, message, raw):
        """Show recent conversation history."""
        key = message.session_key
        history = self._load_session(key)
        display = [
            m for m in history if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if not display:
            return Reply(content="📭 No conversation records yet.")
        # Parse optional count: /history [n]
        parts = raw.split()
        count = 10
        if len(parts) > 1:
            try:
                count = int(parts[1])
            except ValueError:
                pass
        display = display[-count:]
        lines = ["📜 **Last {} records**".format(len(display)), ""]
        for i, m in enumerate(display, 1):
            role = "You" if m["role"] == "user" else "Agent"
            content = str(m["content"]).replace("\n", " ")
            if len(content) > 80:
                content = content[:80] + "…"
            lines.append("{}. **{}** — {}".format(i, role, content))
        return Reply(content="\n".join(lines))

    def _cmd_help(self, message, raw):
        """List available commands (generated from the registry)."""
        lines = [
            "🐈 **Available Commands**",
            "",
            "| Command | Description |",
            "| --- | --- |",
        ]
        for name in self._command_names:
            e = self._commands[name]
            lines.append("| `{}` | {} |".format(e["usage"], e["description"]))
        return Reply(content="\n".join(lines))

    def _cmd_status(self, message, raw):
        """Show device and memory status."""
        import gc
        import os

        def fmt_size(n):
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024 or unit == "GB":
                    return "{}{}".format(int(n) if n == int(n) else round(n, 1), unit)
                n /= 1024.0

        gc.collect()
        lines = ["📊 **Device Status**", ""]
        try:
            mem_free = gc.mem_free()
            mem_alloc = gc.mem_alloc()
            lines.append(
                "- **PyHeap** — {} free / {} used".format(fmt_size(mem_free), fmt_size(mem_alloc))
            )
        except AttributeError:
            pass
        try:
            stat = os.statvfs("/")
            disk_free = stat[3] * stat[1]
            lines.append("- **Storage** — {} free".format(fmt_size(disk_free)))
        except OSError:
            pass
        # Session info
        key = message.session_key
        history = self._load_session(key)
        lines.append("- **Current session** — {} messages".format(len(history)))
        # Memory info
        if self._memory:
            memory_text = self._memory.read_memory()
            lines.append("- **Long-term memory** — {} chars".format(len(memory_text)))
        return Reply(content="\n".join(lines))

    # -- Consolidation -----------------------------------------------------

    def _maybe_consolidate(self, key, session_ending=False):
        """Consolidate messages once persisted history exceeds threshold.

        Only runs when a conversation ends (voice exit, idle expiry or
        /new), never mid-conversation. By default the most recent
        messages are kept so a resumed session retains local context;
        with ``session_ending`` the store is about to be cleared, so the
        full history goes into MEMORY.md and the compact step is skipped.
        """
        if not self._chat_store or not self._memory:
            return
        if self._chat_store.message_count(key) < config.MEMORY_CONSOLIDATE_THRESHOLD:
            return

        all_msgs = self._chat_store.load(key)
        if session_ending:
            to_summarize = all_msgs
            to_keep = []
        else:
            keep = config.MEMORY_KEEP_RECENT
            if len(all_msgs) <= keep:
                return
            to_summarize = all_msgs[:-keep]
            to_keep = all_msgs[-keep:]
            while to_keep and to_keep[0].get("role") == "tool":
                if to_summarize:
                    to_keep.insert(0, to_summarize.pop())
                else:
                    break
            if not to_summarize:
                return

        log.info("Bus", "consolidating {} messages for {}".format(len(to_summarize), key))
        llm_fn = self._agent.make_llm_fn()
        if not llm_fn:
            return
        self._memory.consolidate(to_summarize, llm_fn, session_key=key)
        if to_keep:
            self._chat_store.compact(key, to_keep)
            self._sessions[key] = to_keep

    async def _maybe_consolidate_async(self, key, session_ending=False):
        if not self._chat_store or not self._memory:
            return
        if self._chat_store.message_count(key) < config.MEMORY_CONSOLIDATE_THRESHOLD:
            return
        all_msgs = self._chat_store.load(key)
        if session_ending:
            to_summarize = all_msgs
            to_keep = []
        else:
            keep = config.MEMORY_KEEP_RECENT
            if len(all_msgs) <= keep:
                return
            to_summarize = all_msgs[:-keep]
            to_keep = all_msgs[-keep:]
            while to_keep and to_keep[0].get("role") == "tool":
                if to_summarize:
                    to_keep.insert(0, to_summarize.pop())
                else:
                    break
            if not to_summarize:
                return
        log.info("Bus", "consolidating {} messages for {}".format(len(to_summarize), key))
        make_llm_fn_async = getattr(self._agent, "make_llm_fn_async", None)
        llm_fn = make_llm_fn_async() if make_llm_fn_async is not None else None
        if llm_fn is None:
            llm_fn = self._agent.make_llm_fn()
        if not llm_fn:
            return
        if make_llm_fn_async is not None:
            await self._memory.consolidate_async(to_summarize, llm_fn, session_key=key)
        else:
            await run_sync(self._memory.consolidate, to_summarize, llm_fn, key)
        if to_keep:
            self._chat_store.compact(key, to_keep)
            self._sessions[key] = to_keep

    # -- Public API --------------------------------------------------------

    def clear_session(self, channel, chat_id="default"):
        """Clear history for a specific session."""
        key = "{}:{}".format(channel, chat_id)
        self._maybe_consolidate(key, session_ending=True)
        self._sessions.pop(key, None)
        self._last_active.pop(key, None)
        if self._chat_store:
            self._chat_store.clear(key)

    async def clear_session_async(self, channel, chat_id="default"):
        key = "{}:{}".format(channel, chat_id)
        await self._maybe_consolidate_async(key, session_ending=True)
        self._sessions.pop(key, None)
        self._last_active.pop(key, None)
        if self._chat_store:
            self._chat_store.clear(key)
