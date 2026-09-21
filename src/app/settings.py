"""Device settings and utility routes: config, skills, volume."""

import json
import app.config as config
import app.log as log
from app.util import json_response as _json


def _parse_json(body):
    try:
        return json.loads(body or "{}"), None
    except ValueError:
        return None, _json(400, "Bad Request", {"error": "invalid JSON"})


def _bind(fn, *captured):
    """Adapt fn(captured..., server, method, path, body) into a route handler."""

    async def handler(server, method, path, body):
        return await fn(*(captured + (server, method, path, body)))

    return handler


def register_routes(server, control, skills, voice_channel=None, weixin_channel=None):
    """Register settings and utility routes on the HTTP server."""
    server.register_route("GET", "/config", _bind(_config_get, control))
    server.register_route(
        "POST", "/config", _bind(_config_post, control, voice_channel, weixin_channel)
    )
    server.register_route("GET", "/config/export", _bind(_config_export, control))
    server.register_route(
        "POST", "/config/import", _bind(_config_import, control, voice_channel, weixin_channel)
    )
    server.register_route("POST", "/volume/test", _bind(_volume_test, voice_channel))
    server.register_route("GET", "/skills", _bind(_skills_get, skills))
    server.register_route("POST", "/skills", _bind(_skills_post, skills))
    server.register_route("DELETE", "/skills", _bind(_skills_delete, skills))


async def _config_get(control, server, method, path, body):
    return _json(200, "OK", control.masked_config())


def _apply_config(control, voice_channel, weixin_channel, data):
    """Persist settings and re-apply the switches channels cache at start.

    Shared by the settings form and a config restore, so a backup file takes
    effect on the running channels exactly like an edit does. Returns an
    error response, or None once the settings are stored.
    """
    if "channels" in data:
        channels = data["channels"]
        if not isinstance(channels, dict):
            return _json(400, "Bad Request", {"error": "channels must be an object"})
        ch_cfg = control.channel_config()
        for name, enabled in channels.items():
            if name not in config.CHANNELS_FORCED_ON:
                ch_cfg[name] = bool(enabled)
        data["channels"] = ch_cfg
    # voice_enabled and channels.voice are two views of the same switch;
    # reconcile them up front so a single update() persists both.
    voice = None
    if "voice_enabled" in data:
        voice = bool(data["voice_enabled"])
        ch_cfg = data.get("channels") or control.channel_config()
        ch_cfg["voice"] = voice
        data["channels"] = ch_cfg
    elif "channels" in data and "voice" in data["channels"]:
        voice = bool(data["channels"]["voice"])
        data["voice_enabled"] = voice
    control.update(data)
    if voice is not None and voice_channel:
        voice_channel.set_enabled(voice)
    if "channels" in data and "weixin" in data["channels"] and weixin_channel:
        weixin_channel.set_enabled(bool(data["channels"]["weixin"]))
    return None


async def _config_post(control, voice_channel, weixin_channel, server, method, path, body):
    data, err = _parse_json(body)
    if err:
        return err
    if not isinstance(data, dict):
        return _json(400, "Bad Request", {"error": "config must be a JSON object"})
    err = _apply_config(control, voice_channel, weixin_channel, data)
    if err:
        return err
    return _json(200, "OK", {"status": "ok"})


async def _config_export(control, server, method, path, body):
    """GET /config/export -- whole config as a backup file, keys included."""
    return _json(200, "OK", control.export_config())


async def _config_import(control, voice_channel, weixin_channel, server, method, path, body):
    """POST /config/import -- restore settings from a backup file.

    Merge, not replace: only the settings the file carries are written, so a
    partial backup never clears what it does not mention.
    """
    data, err = _parse_json(body)
    if err:
        return err
    settings, why = control.parse_backup(data)
    if why:
        return _json(400, "Bad Request", {"error": why})
    err = _apply_config(control, voice_channel, weixin_channel, settings)
    if err:
        return err
    log.info("Config", "restored {}: {}".format(len(settings), ", ".join(settings)))
    return _json(200, "OK", {"status": "ok", "applied": list(settings)})


async def _volume_test(voice_channel, server, method, path, body):
    if voice_channel is None:
        return _json(503, "Service Unavailable", {"error": "voice channel unavailable"})
    if voice_channel.play_test_tone():
        return _json(200, "OK", {"status": "ok"})
    return _json(409, "Conflict", {"error": "voice channel busy"})


async def _skills_get(skills, server, method, path, body):
    return _json(200, "OK", skills.list())


async def _skills_post(skills, server, method, path, body):
    data, err = _parse_json(body)
    if err:
        return err
    try:
        skills.add(
            data.get("name"),
            data.get("description"),
            data.get("parameters"),
            data.get("code"),
        )
    except Exception as e:
        return _json(400, "Bad Request", {"error": str(e)})
    return _json(201, "Created", {"status": "ok"})


async def _skills_delete(skills, server, method, path, body):
    data, err = _parse_json(body)
    if err:
        return err
    name = data.get("name")
    if not name:
        return _json(400, "Bad Request", {"error": "name required"})
    if skills.remove(name):
        return _json(200, "OK", {"status": "deleted"})
    return _json(404, "Not Found", {"error": "no such skill"})
