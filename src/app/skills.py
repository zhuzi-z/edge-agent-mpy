"""Skill registry: discover, persist, and exec Python skills.

Skill folder: <name>/skill.json + <name>/code.py (defines run(args)).
Two roots: builtin (read-only) and uploaded (flash-persisted).
SECURITY: uploaded code runs unsandboxed.
"""

import os
import json
import app.config as config
import app.log as log
from app.util import ensure_dir


class SkillError(Exception):
    """Invalid skill definition or execution failure."""


def _http_helpers(httpclient):
    """Return (http_get, http_post_json, http_post) wrappers for skill scope."""

    def http_get(host, port, path, headers=None, **kw):
        return httpclient.https_get(host, port, path, headers or {}, **kw)

    def http_post_json(host, port, path, body, headers=None, **kw):
        return httpclient.https_post_json(host, port, path, headers or {}, body, **kw)

    def http_post(host, port, path, body, headers=None, **kw):
        b = body.encode("utf-8") if isinstance(body, str) else body
        return httpclient.https_request(host, port, "POST", path, headers or {}, b, **kw)

    return http_get, http_post_json, http_post


class Skill:
    """Metadata + exec'd run(args) callable."""

    def __init__(self, name, description, parameters, code, builtin=False):
        self.name = name
        self.description = description or ""
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.code = code
        self.builtin = builtin
        self._registry = None
        self._ns = None

    def compile(self):
        """Exec source code. Raises SkillError on failure."""
        ns = {}
        if self._registry is not None:
            ns.update(self._registry.builtins(self.name))
        try:
            exec(self.code, ns)
        except Exception as e:  # noqa: BLE001
            raise SkillError("compile error: {}".format(e))
        if not callable(ns.get("run")):
            raise SkillError("skill code must define a callable run(args)")
        self._ns = ns

    def run(self, args):
        return self._ns["run"](args or {})

    def meta(self):
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "builtin": self.builtin,
        }

    def tools_entry(self):
        """OpenAI tools schema entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _discover(base_dir):
    """Scan base_dir for skill folders. Returns list of (meta_dict, code_str)."""
    out = []
    try:
        entries = os.listdir(base_dir)
    except OSError:
        return out
    for entry in entries:
        d = base_dir + "/" + entry
        try:
            with open(d + "/skill.json", "r") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        try:
            with open(d + "/code.py", "r") as f:
                code = f.read()
        except OSError:
            continue
        out.append((meta, code))
    return out


class SkillRegistry:
    """Manages builtin + uploaded skills and dynamic HTTP endpoints."""

    def __init__(
        self, builtin_dir=config.BUILTIN_SKILLS_PATH, upload_dir=config.UPLOADED_SKILLS_PATH
    ):
        self._builtin_dir = builtin_dir
        self._upload_dir = upload_dir
        self._skills = {}
        self._endpoints = {}
        self._ep_owner = {}
        self._tools_cache = None  # rebuilt lazily; every LLM turn asks for it
        self._wifi = None
        self._control = None

    def attach_wifi(self, wifi):
        """Attach the WiFi manager so skills can query the device IP."""
        self._wifi = wifi

    def attach_control(self, control):
        """Attach the device control plane (app.control) for skills."""
        self._control = control

    def load(self):
        """Install builtins then uploads. Stale uploads shadowing builtins are pruned."""
        ensure_dir(config.SKILLS_DATA_DIR)
        for meta, code in _discover(self._builtin_dir):
            name = meta.get("name")
            try:
                self._install(
                    Skill(
                        name, meta.get("description"), meta.get("parameters"), code, builtin=True
                    )
                )
            except (SkillError, KeyError) as e:
                log.error("Skill", "builtin install failed for {}: {}".format(name, e))

        for meta, code in _discover(self._upload_dir):
            name = meta.get("name")
            if name in self._skills and self._skills[name].builtin:
                self._delete_upload(name)
                continue
            try:
                self._install(
                    Skill(
                        name, meta.get("description"), meta.get("parameters"), code, builtin=False
                    )
                )
            except (SkillError, KeyError) as e:
                log.error("Skill", "load failed for {}: {}".format(name, e))

    def add(self, name, description, parameters, code):
        """Install and persist an uploaded skill. Raises SkillError on failure."""
        if not name:
            raise SkillError("name is required")
        if not code:
            raise SkillError("code is required")
        if name in self._skills and self._skills[name].builtin:
            raise SkillError("cannot replace built-in skill: {}".format(name))
        self.release_endpoints(name)
        skill = Skill(name, description, parameters, code, builtin=False)
        skill._registry = self
        skill.compile()
        self._write_upload(name, description, parameters, code)
        self._skills[name] = skill
        self._tools_cache = None
        return skill

    def remove(self, name):
        """Remove an uploaded skill. Returns True if removed."""
        s = self._skills.get(name)
        if not s or s.builtin:
            return False
        self.release_endpoints(name)
        del self._skills[name]
        self._tools_cache = None
        self._delete_upload(name)
        return True

    def list(self):
        return [s.meta() for s in self._skills.values()]

    def exec(self, name, args):
        s = self._skills.get(name)
        if s is None:
            raise SkillError("unknown skill: {}".format(name))
        return s.run(args)

    def tools_schema(self):
        if self._tools_cache is None:
            self._tools_cache = [s.tools_entry() for s in self._skills.values()]
        return self._tools_cache

    def builtins(self, name):
        """Namespace injected into skill at exec time."""
        from app import httpclient

        http_get, http_post_json, http_post = _http_helpers(httpclient)
        reg = self

        # Lazy reg._control access: skills compile at load(), before the
        # control plane is attached (same pattern as local_ip/reg._wifi).
        def get_volume():
            if reg._control is not None:
                return reg._control.get_volume()
            return config.SPEAKER_VOLUME_DEFAULT

        def set_volume(level):
            if reg._control is not None:
                return reg._control.set_volume(level)
            return get_volume()

        def register_endpoint(method, path, handler):
            # Skill handlers are synchronous; wrap them so the async HTTP
            # server can always `await handler(...)`.
            async def wrapped(server, method, path, body):
                return handler(server, method, path, body)

            key = (method.upper(), path)
            reg._endpoints[key] = wrapped
            reg._ep_owner[key] = name

        def release_endpoints():
            reg.release_endpoints(name)

        def local_ip():
            try:
                if reg._wifi is not None:
                    return reg._wifi.ifconfig()[0]
            except Exception:  # noqa: BLE001
                pass
            return ""

        # Per-skill data dir, e.g. /config/skills/mijia/ (created best-effort).
        ensure_dir(config.SKILLS_DATA_DIR)
        ddir = config.SKILLS_DATA_DIR + name + "/"
        ensure_dir(ddir)

        return {
            "json": json,
            "http_get": http_get,
            "http_post_json": http_post_json,
            "http_post": http_post,
            "register_endpoint": register_endpoint,
            "release_endpoints": release_endpoints,
            "local_ip": local_ip,
            "data_dir": ddir,
            "server_port": config.HTTP_PORT,
            "get_volume": get_volume,
            "set_volume": set_volume,
        }

    def match_endpoint(self, method, path):
        return self._endpoints.get((method.upper(), path))

    def release_endpoints(self, name):
        """Drop all endpoints owned by skill name."""
        keys = [k for k, v in self._ep_owner.items() if v == name]
        for k in keys:
            self._endpoints.pop(k, None)
            self._ep_owner.pop(k, None)

    def _install(self, skill):
        skill._registry = self
        skill.compile()
        self._skills[skill.name] = skill
        self._tools_cache = None

    def _ensure_upload_root(self):
        try:
            os.mkdir(self._upload_dir)
        except OSError:
            pass

    def _write_upload(self, name, description, parameters, code):
        self._ensure_upload_root()
        d = self._upload_dir + "/" + name
        try:
            os.mkdir(d)
        except OSError:
            pass
        meta = {
            "name": name,
            "description": description or "",
            "parameters": parameters or {"type": "object", "properties": {}},
        }
        with open(d + "/skill.json", "w") as f:
            json.dump(meta, f)
        with open(d + "/code.py", "w") as f:
            f.write(code)

    def _delete_upload(self, name):
        d = self._upload_dir + "/" + name
        for fn in ("skill.json", "code.py"):
            try:
                os.remove(d + "/" + fn)
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass
