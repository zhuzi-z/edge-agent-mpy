"""Agent HTTP route integration tests (WebChannel via Bus + settings)."""

import compat  # noqa: F401

import json
import unittest

import app.skills
import app.providers.llm as llm_client
from app.api.server import HTTPServer
from app.controllers import WiFiManager
from app.storage.store import Store
from app.skills import SkillRegistry
from app.agent import Agent
from app.control import DeviceControl
from app.bus import Bus
from app.settings import register_routes as register_settings
from app.channels.web import WebChannel
from helpers import ServerHarness, path_join, path_dirname

_BUILTIN_DIR = path_join(path_dirname(path_dirname(app.skills.__file__)), "app", "builtin_skills")


class TestAgentRoutes(ServerHarness, unittest.TestCase):
    def _build_server(self):
        cfg_path = self._make_temp_file()
        upload_dir = self._make_temp_dir("up_")
        wifi = WiFiManager(ssid="t", password="t")
        server = HTTPServer(wifi, port=self.port)
        store = Store(cfg_path=cfg_path)
        control = DeviceControl(store)
        skills = SkillRegistry(builtin_dir=_BUILTIN_DIR, upload_dir=upload_dir)
        skills.load()
        skills.attach_control(control)
        agent = Agent(control, skills)
        bus = Bus(agent)
        server._skills = skills
        register_settings(server, control, skills)
        for method, path, handler in WebChannel(bus).routes():
            server.register_route(method, path, handler)
        self._orig_chat = llm_client.chat_async
        return server

    def tearDown(self):
        llm_client.chat_async = self._orig_chat
        super().tearDown()

    def test_config_post_then_get_masks_key(self):
        self._start()
        self._post(
            "/config",
            json.dumps(
                {"base_url": "https://x.io", "api_key": "sk-secret", "model": "m", "volume": 250}
            ),
        )
        status, resp = self._get("/config")
        data = json.loads(resp)
        self.assertEqual(data["api_key"], "***")
        self.assertEqual(data["volume"], 100)  # clamped to range
        self._post("/config", json.dumps({"volume": 30}))
        data = json.loads(self._get("/config")[1])
        self.assertEqual(data["volume"], 30)
        self._post("/config", json.dumps({"reasoning_effort": "high"}))
        data = json.loads(self._get("/config")[1])
        self.assertEqual(data["reasoning_effort"], "high")
        self._post("/config", json.dumps({"reasoning_effort": "  "}))
        data = json.loads(self._get("/config")[1])
        self.assertNotIn("reasoning_effort", data)

    def test_voice_enabled_syncs_channels(self):
        self._start()
        self._post("/config", json.dumps({"voice_enabled": False}))
        data = json.loads(self._get("/config")[1])
        self.assertEqual(data["voice_enabled"], False)
        self.assertEqual(data["channels"]["voice"], False)
        self._post("/config", json.dumps({"channels": {"voice": True}}))
        data = json.loads(self._get("/config")[1])
        self.assertEqual(data["voice_enabled"], True)
        self.assertEqual(data["channels"]["voice"], True)

    def test_config_backup_roundtrip(self):
        self._start()
        self._post(
            "/config",
            json.dumps(
                {
                    "base_url": "https://a.io",
                    "api_key": "sk-secret",
                    "model": "m1",
                    "voice_enabled": False,
                }
            ),
        )
        status, resp = self._get("/config/export")
        self.assertEqual(status, 200)
        backup = json.loads(resp)
        self.assertEqual(backup["kind"], "edge-agent-config")
        self.assertEqual(backup["version"], 1)
        # The point of a backup: unlike GET /config it carries the real keys,
        # and nothing that is only there for display.
        self.assertEqual(backup["config"]["api_key"], "sk-secret")
        self.assertNotIn("system_prompt_default", backup["config"])

        self._post("/config", json.dumps({"model": "m2", "voice_enabled": True}))
        status, data = self._post_json("/config/import", backup)
        self.assertEqual(status, 200)
        self.assertIn("model", data["applied"])
        cfg = json.loads(self._get("/config")[1])
        self.assertEqual(cfg["model"], "m1")
        self.assertEqual(cfg["api_key"], "***")  # the display stays masked
        # voice_enabled and channels.voice come back as the one switch they are
        self.assertEqual(cfg["voice_enabled"], False)
        self.assertEqual(cfg["channels"]["voice"], False)

    def test_config_import_from_masked_dump(self):
        self._start()
        self._post("/config", json.dumps({"api_key": "sk-real", "model": "m1"}))
        dump = json.loads(self._get("/config")[1])  # what the WebUI shows
        self.assertEqual(dump["api_key"], "***")
        dump["model"] = "m2"
        status, data = self._post_json("/config/import", dump)
        self.assertEqual(status, 200)
        # A bare dict restores as well as the envelope; a mask is not a value
        # and a display-only field is not a setting.
        self.assertNotIn("api_key", data["applied"])
        self.assertNotIn("system_prompt_default", data["applied"])
        cfg = json.loads(self._get("/config/export")[1])["config"]
        self.assertEqual(cfg["api_key"], "sk-real")
        self.assertEqual(cfg["model"], "m2")

    def test_config_import_rejects_bad_files(self):
        self._start()
        for bad, why in (
            ([], "must be a JSON object"),
            ({"kind": "other", "config": {"model": "m"}}, "unsupported backup kind"),
            ({"config": {"channels": "voice"}}, "channels must be an object"),
            ({"config": {"nope": 1}}, "no known settings"),
        ):
            status, data = self._post_json("/config/import", bad)
            self.assertEqual(status, 400)
            self.assertIn(why, data["error"])
        # Nothing the rejected files carried was written.
        self.assertEqual(json.loads(self._get("/config")[1])["model"], "")

    def test_skills_crud(self):
        self._start()
        names = [s["name"] for s in json.loads(self._get("/skills")[1])]
        self.assertIn("gpio", names)
        self._post(
            "/skills",
            json.dumps(
                {
                    "name": "inc",
                    "description": "inc",
                    "parameters": {"type": "object"},
                    "code": "def run(args):\n    return args.get('x', 0) + 1",
                }
            ),
        )
        names = [s["name"] for s in json.loads(self._get("/skills")[1])]
        self.assertIn("inc", names)
        self._request("DELETE", "/skills", json.dumps({"name": "inc"}))
        names = [s["name"] for s in json.loads(self._get("/skills")[1])]
        self.assertNotIn("inc", names)

    def test_agent_reply_with_tool_call(self):
        self._start()
        self._post(
            "/config", json.dumps({"base_url": "https://x.io", "api_key": "sk", "model": "m"})
        )
        self._post(
            "/skills",
            json.dumps(
                {
                    "name": "inc",
                    "description": "inc",
                    "parameters": {"type": "object"},
                    "code": "def run(args):\n    return args.get('x', 0) + 1",
                }
            ),
        )
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "inc", "arguments": '{"x": 41}'},
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "result is 42"}}]},
        ]

        async def fake_chat(cfg, messages, tools=None, timeout=None):
            return responses.pop(0)

        llm_client.chat_async = fake_chat
        status, resp = self._post("/agent", json.dumps({"message": "inc 41"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(resp)["reply"], "result is 42")

    def test_agent_validation(self):
        self._start()
        # Without LLM config the agent returns 502.
        status, _ = self._post("/agent", json.dumps({"message": "hi"}))
        self.assertEqual(status, 502)
        # A body without a message is a 400.
        status, _ = self._post("/agent", "{}")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(globals())
