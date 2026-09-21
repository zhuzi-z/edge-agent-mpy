"""LLM Agent: stateless conversation + OpenAI tool-call loop.

The Agent is pure LLM logic. Conversation history is owned by the Bus,
which passes it in and receives new messages back.
Memory context is injected into the system prompt when available.
Device config is owned by the control plane (app.control); the agent
only reads the LLM-related fields.
"""

import json
import time
import app.config as config
import app.log as log
from app.util import ms_since
from app.util import run_sync


class _TurnTimer:
    """Latency collector for one reply: LLM round-trips and tool execution.

    Reported as a single line so a turn's agent cost can be read against the
    ASR and TTS lines without correlating timestamps by hand. ``llm`` is one
    entry per round - a tool chain shows up here as several round-trips, each
    paying its own TLS handshake. ``prep`` is the prompt/memory/tool-schema
    build, which on a cold session means reading MEMORY.md and the session
    JSONL off littlefs before the first request goes out.
    """

    def __init__(self, t0=None, prep_ms=0):
        self._t0 = time.ticks_ms() if t0 is None else t0
        self._prep_ms = prep_ms
        self._llm = []
        self._tools_ms = 0

    def llm(self, ms):
        self._llm.append(ms)

    def tools(self, ms):
        self._tools_ms += ms

    def log(self):
        log.timing(
            "Agent",
            "prep={}ms rounds={} llm={}ms tools={}ms total={}ms".format(
                self._prep_ms,
                len(self._llm),
                "+".join(str(ms) for ms in self._llm),
                self._tools_ms,
                ms_since(self._t0),
            ),
        )


class Agent:
    """LLM conversation and skill dispatch. Stateless per-call."""

    def __init__(self, control, skills, memory=None):
        self._control = control
        self._skills = skills
        self._memory = memory

    @property
    def _cfg(self):
        """Live device config (owned by DeviceControl)."""
        return self._control.config()

    @staticmethod
    def _current_time_str():
        """Format current local time for prompt injection."""
        t = time.localtime()
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            t[0], t[1], t[2], t[3], t[4], t[5]
        )

    def _build_system_prompt(self, channel=None):
        """Build system prompt with time and memory context.

        Voice-channel requests get VOICE_PROMPT_SUFFIX appended so replies
        are short, spoken-style and TTS-friendly.
        """
        prompt = self._cfg.get("system_prompt") or config.SYSTEM_PROMPT_DEFAULT
        if channel == "voice":
            prompt += config.VOICE_PROMPT_SUFFIX
        prompt += "\nCurrent time: {}".format(self._current_time_str())
        if self._memory:
            context = self._memory.get_context()
            if context:
                prompt += "\n\n" + context
        return prompt

    def _prepare_reply(self, user_text, history, channel):
        """Shared setup for reply()/reply_async().

        Returns (error_reply, msgs, tools, new_messages); when error_reply
        is not None it must be returned as-is.
        """
        if not self._cfg.get("base_url") or not self._cfg.get("api_key"):
            return (None, "LLM not configured (POST /config)", []), None, None, None
        if history is None:
            history = [{"role": "user", "content": user_text}]
        msgs = [{"role": "system", "content": self._build_system_prompt(channel)}]
        msgs.extend(history)
        return None, msgs, self._skills.tools_schema(), []

    def reply(self, user_text, history=None, channel=None):
        """Generate reply using provided history.

        Args:
            user_text: The user's message (already appended to history by caller).
            history: List of message dicts (owned by caller, e.g. Bus).
            channel: Source channel name (e.g. "web", "voice"); voice gets
                spoken-style prompt instructions.

        Returns:
            (text, error, new_messages) where new_messages are assistant/tool
            messages generated during this call (caller should persist them).
        """
        t_prep = time.ticks_ms()
        early, msgs, tools, new_messages = self._prepare_reply(user_text, history, channel)
        if early:
            return early

        from app.providers import LLMError, get_llm

        timer = _TurnTimer(t_prep, ms_since(t_prep))
        try:
            max_rounds = self._cfg.get("max_tool_rounds") or config.LLM_MAX_TOOL_ROUNDS
            for _ in range(max_rounds):
                t_llm = time.ticks_ms()
                resp = get_llm(self._cfg).chat(self._cfg, msgs, tools)
                timer.llm(ms_since(t_llm))
                done = self._consume_llm_response(resp, msgs, new_messages, timer)
                if done is not None:
                    return done[0], done[1], new_messages

            return None, "tool-call loop limit reached", new_messages
        except LLMError as e:
            return None, "LLM error: {}".format(e), new_messages
        finally:
            timer.log()

    async def reply_async(self, user_text, history=None, channel=None):
        """Async version of :meth:`reply` for the event-loop thread.

        Identical to ``reply`` except the LLM call is awaited, so a slow
        chat completion yields to other asyncio tasks (other HTTP handlers)
        instead of blocking them.
        """
        t_prep = time.ticks_ms()
        early, msgs, tools, new_messages = self._prepare_reply(user_text, history, channel)
        if early:
            return early

        from app.providers import LLMError, get_llm

        timer = _TurnTimer(t_prep, ms_since(t_prep))
        try:
            max_rounds = self._cfg.get("max_tool_rounds") or config.LLM_MAX_TOOL_ROUNDS
            for _ in range(max_rounds):
                t_llm = time.ticks_ms()
                resp = await get_llm(self._cfg).chat_async(self._cfg, msgs, tools)
                timer.llm(ms_since(t_llm))
                done = await self._consume_llm_response_async(resp, msgs, new_messages, timer)
                if done is not None:
                    return done[0], done[1], new_messages

            return None, "tool-call loop limit reached", new_messages
        except LLMError as e:
            return None, "LLM error: {}".format(e), new_messages
        finally:
            timer.log()

    def _consume_llm_response(self, resp, msgs, new_messages, timer):
        """Append one LLM response to ``msgs`` and run its tool calls.

        Returns ``(reply_text, error)`` when the conversation is final
        (no choices, or a message without tool calls); ``None`` when tool
        results were appended and another LLM round is needed.
        """
        choices = resp.get("choices")
        if not choices:
            return None, "LLM returned no choices (rate-limited or empty response)"
        msg = choices[0].get("message", {})

        new_messages.append(msg)
        msgs.append(msg)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content") or "", None

        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except ValueError:
                args = {}
            log.info("Agent", "tool call: {} args={}".format(name, raw_args))
            t_tool = time.ticks_ms()
            try:
                result = self._skills.exec(name, args)
            except Exception as e:  # noqa: BLE001
                result = "ERROR: {}".format(e)
            timer.tools(ms_since(t_tool))
            log.info("Agent", "tool result: {} -> {}".format(name, str(result)[:120]))
            tmsg = {
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": str(result),
            }
            new_messages.append(tmsg)
            msgs.append(tmsg)
        return None

    async def _consume_llm_response_async(self, resp, msgs, new_messages, timer):
        """Consume a response, running tool calls off the event-loop thread."""
        choices = resp.get("choices")
        if not choices:
            return None, "LLM returned no choices (rate-limited or empty response)"
        msg = choices[0].get("message", {})
        new_messages.append(msg)
        msgs.append(msg)
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content") or "", None

        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except ValueError:
                args = {}
            log.info("Agent", "tool call: {} args={}".format(name, raw_args))
            t_tool = time.ticks_ms()
            try:
                result = await run_sync(self._skills.exec, name, args)
            except Exception as e:  # noqa: BLE001
                result = "ERROR: {}".format(e)
            timer.tools(ms_since(t_tool))
            log.info("Agent", "tool result: {} -> {}".format(name, str(result)[:120]))
            tmsg = {
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": str(result),
            }
            new_messages.append(tmsg)
            msgs.append(tmsg)
        return None

    def make_llm_fn(self):
        """Return a simple LLM callable for memory consolidation.

        Returns callable(user_content, system_prompt) -> str, or None if
        LLM is not configured.
        """
        if not self._cfg.get("base_url") or not self._cfg.get("api_key"):
            return None

        cfg = self._cfg

        def llm_fn(user_content, system_prompt):
            from app.providers import LLMError, get_llm

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            resp = get_llm(cfg).chat(cfg, messages, tools=None)
            choices = resp.get("choices")
            if not choices:
                raise LLMError("no choices in consolidation response")
            return choices[0].get("message", {}).get("content", "")

        return llm_fn

    def make_llm_fn_async(self):
        """Return an awaitable LLM callable for async memory consolidation."""
        if not self._cfg.get("base_url") or not self._cfg.get("api_key"):
            return None

        cfg = self._cfg

        async def llm_fn(user_content, system_prompt):
            from app.providers import LLMError, get_llm

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            resp = await get_llm(cfg).chat_async(cfg, messages, tools=None)
            choices = resp.get("choices")
            if not choices:
                raise LLMError("no choices in consolidation response")
            return choices[0].get("message", {}).get("content", "")

        return llm_fn
