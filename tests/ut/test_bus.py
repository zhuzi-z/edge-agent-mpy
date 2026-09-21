"""Bus session isolation, persistence, and slash command tests."""

import compat  # noqa: F401

import asyncio
import time
import unittest
from helpers import mkdtemp, rmtree, FakeAgent
from app.bus import Bus
from app.channel import Message
from app.storage.chat import ChatStore
from app.storage.memory import MemoryManager


class TestBusDispatch(unittest.TestCase):
    def test_dispatch_reply_and_error(self):
        agent = FakeAgent(reply_text="world")
        bus = Bus(agent)
        reply = asyncio.run(bus.dispatch(Message(channel="web", content="hello")))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.content, "world")
        self.assertEqual(agent.calls[0]["channel"], "web")
        agent2 = FakeAgent(reply_text=None, error="LLM not configured")
        reply = asyncio.run(Bus(agent2).dispatch(Message(channel="web", content="x")))
        self.assertFalse(reply.ok)

    def test_session_isolation_and_clear(self):
        agent = FakeAgent(reply_text="ok")
        bus = Bus(agent)
        asyncio.run(bus.dispatch(Message(channel="web", content="msg1")))
        asyncio.run(bus.dispatch(Message(channel="voice", content="msg2")))
        self.assertEqual(agent.calls[0]["history_len"], 1)
        self.assertEqual(agent.calls[1]["history_len"], 1)
        asyncio.run(bus.dispatch(Message(channel="web", content="msg3")))
        self.assertEqual(agent.calls[2]["history_len"], 3)
        bus.clear_session("web")
        asyncio.run(bus.dispatch(Message(channel="web", content="msg4")))
        self.assertEqual(agent.calls[3]["history_len"], 1)
        # /new clears the session via slash command as well.
        asyncio.run(bus.dispatch(Message(channel="web", content="/new")))
        asyncio.run(bus.dispatch(Message(channel="web", content="msg5")))
        self.assertEqual(agent.calls[-1]["history_len"], 1)

    def test_exit_voice_metadata(self):
        import app.config as config

        class ExitAgent(FakeAgent):
            def reply(self, user_text, history=None, channel=None):
                text, error, msgs = super().reply(user_text, history, channel)
                msgs.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": config.VOICE_CONTROL_SKILL,
                                    "arguments": '{"action": "exit", "ack": "Sure, talk to you later"}',
                                },
                            }
                        ],
                    }
                )
                return text, error, msgs

        reply = asyncio.run(
            Bus(ExitAgent(reply_text="OK")).dispatch(
                Message(channel="voice", content="you can stand down")
            )
        )
        self.assertTrue(reply.metadata.get("exit_voice"))
        self.assertEqual(reply.metadata.get("exit_ack"), "Sure, talk to you later")
        reply = asyncio.run(
            Bus(FakeAgent(reply_text="hi")).dispatch(Message(channel="voice", content="hello"))
        )
        self.assertFalse(reply.metadata.get("exit_voice"))

    def test_volume_action_keeps_conversation(self):
        import app.config as config

        class VolumeAgent(FakeAgent):
            def reply(self, user_text, history=None, channel=None):
                text, error, msgs = super().reply(user_text, history, channel)
                msgs.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": config.VOICE_CONTROL_SKILL,
                                    "arguments": '{"action": "volume", "level": 30}',
                                },
                            }
                        ],
                    }
                )
                return text, error, msgs

        reply = asyncio.run(
            Bus(VolumeAgent(reply_text="done")).dispatch(
                Message(channel="voice", content="turn the volume down")
            )
        )
        self.assertFalse(reply.metadata.get("exit_voice"))

    def test_history_bounded(self):
        import app.config as config

        agent = FakeAgent(reply_text="ok")
        bus = Bus(agent)
        for i in range(config.AGENT_HISTORY_MAX + 5):
            asyncio.run(bus.dispatch(Message(channel="web", content="m%d" % i)))
        self.assertLessEqual(agent.calls[-1]["history_len"], config.AGENT_HISTORY_MAX)

    def test_trim_keeps_tool_call_pairs(self):
        from app.bus import _trim_history

        def assistant_tc(call_id):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id, "function": {"name": "t", "arguments": "{}"}}],
            }

        # Window starting with an orphan tool result: drop it.
        hist = [assistant_tc("cA"), {"role": "tool", "tool_call_id": "cA", "content": "r"}]
        hist += [{"role": "user", "content": "u%d" % i} for i in range(12)]
        trimmed = _trim_history(hist)
        self.assertEqual(trimmed[0]["role"], "user")
        # Tool-call message whose result is inside the window stays intact.
        hist = [{"role": "user", "content": "q"}, assistant_tc("cB")]
        hist.append({"role": "tool", "tool_call_id": "cB", "content": "r"})
        self.assertEqual(len(_trim_history(hist, limit=2)), 2)
        # Tool-call message whose result was trimmed away is dropped too.
        hist = [assistant_tc("cC")] + [{"role": "user", "content": "u"} for _ in range(5)]
        self.assertEqual(_trim_history(hist, limit=4)[0]["role"], "user")


class TestBusPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp(prefix="xzbus_")
        self.chat_store = ChatStore(data_dir=self.tmp)

    def tearDown(self):
        rmtree(self.tmp)

    def test_messages_persisted_and_survive_restart(self):
        agent = FakeAgent(reply_text="hi")
        bus1 = Bus(agent, chat_store=self.chat_store)
        asyncio.run(bus1.dispatch(Message(channel="web", content="first")))
        loaded = self.chat_store.load("web:default")
        self.assertEqual(len(loaded), 2)

        agent2 = FakeAgent(reply_text="hello again")
        bus2 = Bus(agent2, chat_store=self.chat_store)
        asyncio.run(bus2.dispatch(Message(channel="web", content="second")))
        self.assertEqual(agent2.calls[0]["history_len"], 3)


class TestSlashCommands(unittest.TestCase):
    def test_slash_commands(self):
        agent = FakeAgent(reply_text="world")
        bus = Bus(agent)
        reply = asyncio.run(bus.dispatch(Message(channel="web", content="/help")))
        self.assertIn("/new", reply.content)
        self.assertEqual(len(agent.calls), 0)
        asyncio.run(bus.dispatch(Message(channel="web", content="hello")))
        reply = asyncio.run(bus.dispatch(Message(channel="web", content="/history")))
        self.assertIn("hello", reply.content)
        # Unknown commands fall through to the agent.
        asyncio.run(bus.dispatch(Message(channel="web", content="/unknown")))
        self.assertEqual(len(agent.calls), 2)


class TestSessionIdleTimeout(unittest.TestCase):
    def test_idle_timeout_renews_session(self):
        tmp = mkdtemp(prefix="xzidle_")
        try:
            store = ChatStore(data_dir=tmp)
            agent = FakeAgent(reply_text="ok")
            bus = Bus(agent, chat_store=store)
            asyncio.run(bus.dispatch(Message(channel="voice", content="m1")))
            asyncio.run(bus.dispatch(Message(channel="voice", content="m2")))
            self.assertEqual(agent.calls[-1]["history_len"], 3)
            # Within the default 15 min: conversation continues.
            bus._last_active["voice:default"] = time.ticks_add(time.ticks_ms(), -14 * 60 * 1000)
            asyncio.run(bus.dispatch(Message(channel="voice", content="m3")))
            self.assertEqual(agent.calls[-1]["history_len"], 5)
            # Idle longer than 15 min: next message starts a fresh session,
            # and the persisted history is cleared too.
            bus._last_active["voice:default"] = time.ticks_add(time.ticks_ms(), -16 * 60 * 1000)
            asyncio.run(bus.dispatch(Message(channel="voice", content="m4")))
            self.assertEqual(agent.calls[-1]["history_len"], 1)
            self.assertEqual(len(store.load("voice:default")), 2)
            # Timeout is externally controllable via session_cfg_fn.
            agent2 = FakeAgent(reply_text="ok")
            bus2 = Bus(agent2, session_cfg_fn=lambda: {"session_timeout_min": 1})
            asyncio.run(bus2.dispatch(Message(channel="web", content="x1")))
            asyncio.run(bus2.dispatch(Message(channel="web", content="x2")))
            self.assertEqual(agent2.calls[-1]["history_len"], 3)
            bus2._last_active["web:default"] = time.ticks_add(time.ticks_ms(), -61 * 1000)
            asyncio.run(bus2.dispatch(Message(channel="web", content="x3")))
            self.assertEqual(agent2.calls[-1]["history_len"], 1)

            # A session whose user never messages again is reclaimed by the
            # sweep, not only when that same key turns up again.
            bus3 = Bus(
                FakeAgent(reply_text="ok"), session_cfg_fn=lambda: {"session_timeout_min": 1}
            )
            asyncio.run(bus3.dispatch(Message(channel="weixin", chat_id="u1", content="a")))
            asyncio.run(bus3.dispatch(Message(channel="weixin", chat_id="u2", content="b")))
            self.assertEqual(len(bus3._sessions), 2)
            for key in list(bus3._last_active):
                bus3._last_active[key] = time.ticks_add(time.ticks_ms(), -61 * 1000)
            self.assertEqual(bus3.sweep_idle(), 2)
            self.assertEqual(bus3._sessions, {})
            self.assertEqual(bus3._last_active, {})
            # A zero timeout disables expiry, so the sweep is a no-op too.
            bus4 = Bus(FakeAgent(), session_cfg_fn=lambda: {"session_timeout_min": 0})
            asyncio.run(bus4.dispatch(Message(channel="web", content="x")))
            self.assertEqual(bus4.sweep_idle(), 0)
            self.assertEqual(len(bus4._sessions), 1)
        finally:
            rmtree(tmp)


class ConsolidatingAgent(FakeAgent):
    """FakeAgent with a working LLM function for consolidation."""

    def __init__(self, llm_fn, reply_text="ok"):
        super().__init__(reply_text=reply_text)
        self._llm_fn = llm_fn

    def make_llm_fn(self):
        return self._llm_fn


class TestConsolidationTiming(unittest.TestCase):
    """Consolidation runs only after a conversation ends, never mid-talk."""

    def setUp(self):
        self.tmp = mkdtemp(prefix="xzcons_")
        self.chat_store = ChatStore(data_dir=self.tmp)
        self.memory = MemoryManager(data_dir=self.tmp)
        self.llm_calls = []

    def tearDown(self):
        rmtree(self.tmp)

    def _fake_llm(self, messages, system):
        self.llm_calls.append(messages)
        return "summary%d" % len(self.llm_calls)

    def test_consolidates_only_after_conversation_ends(self):
        import app.config as config

        bus = Bus(
            ConsolidatingAgent(self._fake_llm),
            chat_store=self.chat_store,
            memory=self.memory,
        )
        turns = config.MEMORY_CONSOLIDATE_THRESHOLD // 2  # 2 stored messages per turn
        for i in range(turns):
            asyncio.run(bus.dispatch(Message(channel="web", content="m%d" % i)))
        # Past the threshold but mid-conversation: nothing consolidated.
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(self.memory.read_memory(), "")

        # /new ends the conversation: its messages go into MEMORY.md.
        asyncio.run(bus.dispatch(Message(channel="web", content="/new")))
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(self.memory.read_memory(), "summary1")

        # Idle expiry ends it too.
        for i in range(turns):
            asyncio.run(bus.dispatch(Message(channel="web", content="n%d" % i)))
        self.assertEqual(len(self.llm_calls), 1)
        bus._last_active["web:default"] = time.ticks_add(
            time.ticks_ms(), -(config.SESSION_IDLE_TIMEOUT_SEC * 1000 + 1000)
        )
        asyncio.run(bus.dispatch(Message(channel="web", content="after idle")))
        self.assertEqual(len(self.llm_calls), 2)
        self.assertEqual(self.memory.read_memory(), "summary2")

        # exit_voice ends a voice conversation, keeping the most recent
        # messages in case the user wakes the device again shortly after.
        class ExitAgent(ConsolidatingAgent):
            def reply(self, user_text, history=None, channel=None):
                text, error, msgs = super().reply(user_text, history, channel)
                if user_text == "goodbye":
                    msgs.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": config.VOICE_CONTROL_SKILL,
                                        "arguments": '{"action": "exit", "ack": "bye"}',
                                    },
                                }
                            ],
                        }
                    )
                return text, error, msgs

        bus = Bus(ExitAgent(self._fake_llm), chat_store=self.chat_store, memory=self.memory)
        turns = config.MEMORY_CONSOLIDATE_THRESHOLD // 2  # 2 messages per turn
        for i in range(turns):
            asyncio.run(bus.dispatch(Message(channel="voice", content="v%d" % i)))
        self.assertEqual(len(self.llm_calls), 2)
        reply = asyncio.run(bus.dispatch(Message(channel="voice", content="goodbye")))
        self.assertTrue(reply.metadata.get("exit_voice"))
        self.assertEqual(len(self.llm_calls), 3)
        self.assertEqual(self.memory.read_memory(), "summary3")
        self.assertEqual(len(self.chat_store.load("voice:default")), config.MEMORY_KEEP_RECENT)


if __name__ == "__main__":
    unittest.main(globals())
