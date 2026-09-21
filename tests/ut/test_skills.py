"""Skill registry tests: discovery, exec, schema, persistence, builtins."""

import compat  # noqa: F401

import json
import os
import unittest
import asyncio

import app.skills
from app.skills import SkillRegistry, SkillError
from helpers import path_join, path_dirname, path_isdir, mkdtemp, rmtree

_BUILTIN_DIR = path_join(path_dirname(path_dirname(app.skills.__file__)), "app", "builtin_skills")

SIMPLE_SKILL = "def run(args):\n    return args.get('x', 0) + 1"


class TestSkillRegistry(unittest.TestCase):
    def setUp(self):
        self._dirs = []

    def tearDown(self):
        for d in self._dirs:
            if d != _BUILTIN_DIR:
                rmtree(d)

    def _reg(self, builtin=False):
        up = mkdtemp(prefix="skills_up_")
        bi = _BUILTIN_DIR if builtin else mkdtemp(prefix="skills_bi_")
        self._dirs.extend([bi, up])
        return SkillRegistry(builtin_dir=bi, upload_dir=up)

    def test_add_exec_list_remove(self):
        reg = self._reg()
        reg.add("inc", "increment x", {"type": "object", "properties": {}}, SIMPLE_SKILL)
        self.assertEqual(reg.exec("inc", {"x": 41}), 42)
        self.assertEqual(reg.list()[0]["name"], "inc")
        self.assertEqual(reg.tools_schema()[0]["function"]["name"], "inc")
        self.assertTrue(reg.remove("inc"))
        self.assertFalse(reg.remove("inc"))

    def test_bad_code_raises(self):
        reg = self._reg()
        with self.assertRaises(SkillError):
            reg.add("bad", "no run fn", {}, "x = 1")

    def test_persistence_across_instances(self):
        reg = self._reg(builtin=True)
        reg.add("inc", "increment x", {"type": "object"}, SIMPLE_SKILL)
        reg2 = SkillRegistry(builtin_dir=_BUILTIN_DIR, upload_dir=reg._upload_dir)
        reg2.load()
        self.assertEqual(reg2.exec("inc", {"x": 10}), 11)

    def test_dynamic_endpoints(self):
        reg = self._reg()
        code = (
            "def run(args):\n"
            "    register_endpoint('GET', '/x', handler)\n"
            "    return 'registered'\n"
            "def handler(server, method, path, body):\n"
            "    return (200, 'OK', 'text/plain', 'from skill')\n"
        )
        reg.add("ep", "d", None, code)
        reg.exec("ep", {})
        h = reg.match_endpoint("GET", "/x")
        resp = asyncio.run(h(None, "GET", "/x", ""))
        self.assertEqual(resp[3], "from skill")
        reg.remove("ep")
        self.assertIsNone(reg.match_endpoint("GET", "/x"))

    def test_data_dir_per_skill(self):
        import app.config as config

        reg = self._reg()
        old = config.SKILLS_DATA_DIR
        d = mkdtemp(prefix="skills_data_")
        self._dirs.append(d)
        config.SKILLS_DATA_DIR = d + "/"
        try:
            reg.add("dd", "d", None, "def run(args):\n    return data_dir")
            self.assertEqual(reg.exec("dd", {}), d + "/dd/")
            self.assertTrue(path_isdir(d + "/dd"))
        finally:
            config.SKILLS_DATA_DIR = old


class TestBuiltinSkills(unittest.TestCase):
    def setUp(self):
        self._dirs = []
        import machine

        if hasattr(machine, "reset_pin_states"):
            machine.reset_pin_states()

    def tearDown(self):
        for d in self._dirs:
            rmtree(d)

    def _reg(self):
        up = mkdtemp(prefix="skills_bup_")
        self._dirs.append(up)
        reg = SkillRegistry(builtin_dir=_BUILTIN_DIR, upload_dir=up)
        reg.load()
        return reg

    def test_builtin_loaded_and_protected(self):
        reg = self._reg()
        names = {s["name"] for s in self._reg().list()}
        self.assertEqual(names, {"gpio", "http_get", "web_search", "voice_control"})
        self.assertIn("OK", reg.exec("voice_control", {"action": "exit", "ack": "OK"}))
        # Original exit-only schema (no action key) still exits.
        self.assertIn("OK", reg.exec("voice_control", {"ack": "OK"}))
        with self.assertRaises(SkillError):
            reg.add("gpio", "x", {}, "def run(args):\n    return 1")
        self.assertFalse(reg.remove("gpio"))

    def test_voice_control_volume(self):
        from app.control import DeviceControl
        from app.storage.store import Store

        d = mkdtemp(prefix="skills_vol_")
        self._dirs.append(d)
        store = Store(d + "/agent.json")
        store.save_config({"volume": 40})
        reg = self._reg()
        reg.attach_control(DeviceControl(store))
        self.assertEqual(reg.exec("voice_control", {"action": "volume"}), "current volume: 40")
        self.assertIn("40 -> 60", reg.exec("voice_control", {"action": "volume", "level": 60}))
        self.assertIn("60 -> 50", reg.exec("voice_control", {"action": "volume", "adjust": -10}))
        # Relative steps clamp at the 0-100 range edges and persist.
        self.assertIn("-> 100", reg.exec("voice_control", {"action": "volume", "adjust": 999}))
        self.assertIn("-> 0", reg.exec("voice_control", {"action": "volume", "adjust": -999}))
        self.assertEqual(Store(d + "/agent.json").load_config()["volume"], 0)

    def test_gpio_on_off_toggle(self):
        import machine

        reg = self._reg()
        reg.exec("gpio", {"pin": 6, "action": "on"})
        self.assertEqual(machine.Pin(6).value(), 1)
        reg.exec("gpio", {"pin": 6, "action": "off"})
        self.assertEqual(machine.Pin(6).value(), 0)
        reg.exec("gpio", {"pin": 6, "action": "toggle"})
        self.assertEqual(machine.Pin(6).value(), 1)


if __name__ == "__main__":
    unittest.main(globals())
