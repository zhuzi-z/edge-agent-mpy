"""Agent tool-call loop tests."""

import compat  # noqa: F401
from helpers import mkdtemp, rmtree

import os
import unittest
import asyncio

import app.providers.llm as llm_client
from app.storage.store import Store
from app.skills import SkillRegistry
from app.agent import Agent
from app.control import DeviceControl
import app.config as config


def _mk_reply(content=None, tool_calls=None):
    msg = {}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


ADD_SKILL_CODE = 'def run(args):\n    return int(args.get("a", 0)) + int(args.get("b", 0))\n'


class TestAgentReply(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp(prefix="xzagent_")
        os.mkdir(self.tmp + "/builtin")
        os.mkdir(self.tmp + "/skills")
        store = Store(self.tmp + "/cfg.json")
        store.save_config(
            {
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
                "model": "m",
                "ca_path": "",
            }
        )
        self.skills = SkillRegistry(
            builtin_dir=self.tmp + "/builtin", upload_dir=self.tmp + "/skills"
        )
        self.skills.add(
            "add",
            "add two numbers",
            {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
            ADD_SKILL_CODE,
        )
        self.agent = Agent(DeviceControl(Store(self.tmp + "/cfg.json")), self.skills)
        self._orig_chat = llm_client.chat
        self._orig_chat_async = llm_client.chat_async

    def tearDown(self):
        llm_client.chat = self._orig_chat
        llm_client.chat_async = self._orig_chat_async
        rmtree(self.tmp)

    def _fake_chat(self, *responses):
        it = iter(responses)
        llm_client.chat = lambda cfg, msgs, tools=None, timeout=None: next(it)

    def test_plain_reply(self):
        self._fake_chat(_mk_reply(content="hello"))
        text, err, new_msgs = self.agent.reply("hi", [{"role": "user", "content": "hi"}])
        self.assertIsNone(err)
        self.assertEqual(text, "hello")

    def test_tool_call_then_answer(self):
        self._fake_chat(
            _mk_reply(
                tool_calls=[
                    {
                        "id": "c1",
                        "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                    }
                ]
            ),
            _mk_reply(content="2 + 3 = 5"),
        )
        text, err, new_msgs = self.agent.reply(
            "what is 2+3?", [{"role": "user", "content": "what is 2+3?"}]
        )
        self.assertIsNone(err)
        self.assertEqual(text, "2 + 3 = 5")
        tool_msgs = [m for m in new_msgs if m.get("role") == "tool"]
        self.assertEqual(tool_msgs[0]["content"], "5")

    def test_async_tool_call_runs_off_event_loop(self):
        responses = [
            _mk_reply(
                tool_calls=[
                    {
                        "id": "c1",
                        "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                    }
                ]
            ),
            _mk_reply(content="async result is 5"),
        ]

        async def fake_chat_async(cfg, messages, tools=None, timeout=None):
            return responses.pop(0)

        llm_client.chat_async = fake_chat_async
        text, error, new_msgs = asyncio.run(
            self.agent.reply_async("what is 2+3?", [{"role": "user", "content": "what?"}])
        )
        self.assertIsNone(error)
        self.assertEqual(text, "async result is 5")
        self.assertEqual([m for m in new_msgs if m.get("role") == "tool"][0]["content"], "5")

    def test_not_configured(self):
        agent = Agent(DeviceControl(Store(self.tmp + "/absent.json")), self.skills)
        text, err, _ = agent.reply("hi", [])
        self.assertIn("not configured", err)

    def test_loop_limit(self):
        tc = _mk_reply(
            tool_calls=[{"id": "c1", "function": {"name": "add", "arguments": '{"a":1,"b":1}'}}]
        )
        self._fake_chat(*[tc for _ in range(config.LLM_MAX_TOOL_ROUNDS)])
        text, err, _ = self.agent.reply("loop", [{"role": "user", "content": "loop"}])
        self.assertIn("loop limit", err)

    def test_voice_channel_gets_spoken_style_prompt(self):
        captured = []
        llm_client.chat = lambda cfg, msgs, tools=None, timeout=None: (
            captured.append(msgs),
            _mk_reply(content="ok"),
        )[1]
        history = [{"role": "user", "content": "hi"}]
        self.agent.reply("hi", history, channel="voice")
        self.agent.reply("hi", history, channel="web")
        voice_sys = captured[0][0]["content"]
        web_sys = captured[1][0]["content"]
        self.assertIn(config.VOICE_PROMPT_SUFFIX, voice_sys)
        self.assertNotIn(config.VOICE_PROMPT_SUFFIX, web_sys)


if __name__ == "__main__":
    unittest.main(globals())
