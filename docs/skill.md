# Skill Specification

A **skill** is a small Python module that the LLM can call as a tool
(OpenAI function calling). The agent exposes every registered skill in the
LLM `tools` schema; when the model calls one, the agent executes the skill's
`run(args)` and feeds the stringified result back into the conversation.

## Layout

A skill is a folder whose two required files are:

```
<name>/
  skill.json   # metadata: name, description, parameters schema
  code.py      # implementation: must define run(args)
```

Local-only files such as `README.md`, `test.py`, and `serve.py` are useful
during development; the upload helper sends only the two required files.

Two roots are scanned at boot:

| Root | Path | Purpose |
|------|------|---------|
| Builtin | `/app/builtin_skills` | Shipped with the app, read-only, deployed with `make deploy` |
| Uploaded | `/skills` | Uploaded at runtime, persisted on flash |

Uploaded skills cannot replace builtin skills (an upload with a builtin name
is rejected; stale uploads shadowing builtins are pruned at boot).

## skill.json

```json
{
  "name": "gpio",
  "description": "Control the output level of a GPIO pin (on/off/toggle) ...",
  "parameters": {
    "type": "object",
    "properties": {
      "pin": {"type": "integer", "description": "GPIO pin number, e.g. 38"},
      "action": {
        "type": "string",
        "enum": ["on", "off", "toggle"],
        "description": "on = drive high, off = drive low, toggle = flip"
      }
    },
    "required": ["pin", "action"]
  }
}
```

- `name` — tool name seen by the LLM; must match the folder name.
- `description` — read **by the LLM** to decide when/how to call the tool.
  Write it like usage instructions: when to call, what each argument means,
  output conventions, pitfalls. This is the most important field.
- `parameters` — JSON Schema of the tool arguments (OpenAI function format).
  Defaults to an empty object schema when omitted.

## code.py

Must define a callable `run(args)`; `args` is the dict parsed by the LLM
(may be `{}`). The return value is converted with `str()` and returned to the
LLM as the tool result — return compact, informative strings. Exceptions are
caught and reported to the LLM as `ERROR: <message>`.

```python
def run(args):
    import machine

    pin_no = args.get("pin")
    if pin_no is None:
        return "missing required arg: pin"
    pin = machine.Pin(pin_no, machine.Pin.OUT)
    if args.get("action") == "on":
        pin.value(1)
    return "GPIO" + str(pin_no) + " on"
```

Constraints:

- Code runs on **MicroPython** on the device — no CPython-only stdlib.
- It executes **unsandboxed**: a skill can drive any hardware and read
  everything stored on the device (WiFi credentials, account tokens, chat
  history). Only install skills you trust.
- Keep memory footprint small; avoid large buffers and long-blocking calls
  (the tool loop has a round budget, `max_tool_rounds`, default 6).

## Injected Builtins

When a skill is compiled, these names are injected into its global namespace:

| Name | Description |
|------|-------------|
| `json` | The `json` module |
| `http_get(host, port, path, headers=None, **kw)` | HTTPS GET → response |
| `http_post_json(host, port, path, body, headers=None, **kw)` | HTTPS POST with JSON body |
| `http_post(host, port, path, body, headers=None, **kw)` | HTTPS POST with raw body |
| `register_endpoint(method, path, handler)` | Register an HTTP route on the device server |
| `release_endpoints()` | Remove all endpoints registered by this skill |
| `local_ip()` | Device IP address (`""` if unavailable) |
| `data_dir` | Per-skill persistent directory, `/config/skills/<name>/` (created at load) |
| `server_port` | Device HTTP port (80) |
| `get_volume()` / `set_volume(level)` | Speaker volume (0–100) |

### HTTP endpoints

`register_endpoint(method, path, handler)` lets a skill serve its own web
pages/APIs on the device's HTTP server (e.g. the `mijia` skill's QR login
page at `/mijia/`). The handler is synchronous and returns a tuple:

```python
def page(server, method, path, body):
    return (200, "OK", "text/html; charset=utf-8", "<h1>hello</h1>")

register_endpoint("GET", "/mypath/", page)
```

Skill endpoints are matched only when no built-in route matches. All
endpoints owned by a skill are released automatically when the skill is
replaced or removed.

## Management API

Skills are managed over the device's HTTP API (also used by the WebUI):

| Method | Path | Body | Effect |
|--------|------|------|--------|
| `GET` | `/skills` | — | List installed skills (name/description/parameters/builtin) |
| `POST` | `/skills` | `{"name", "description", "parameters", "code"}` | Install/replace an uploaded skill (compile-checked first) |
| `DELETE` | `/skills` | `{"name": "<skill>"}` | Remove an uploaded skill |

Upload from this repo with the helper script:

```bash
python3 skills/upload.py --base-url http://<device-ip>          # all skills in skills/
python3 skills/upload.py --base-url http://<device-ip> mijia     # one skill
```

It scans `skills/<name>/` for `skill.json` + `code.py` and POSTs them to the
device. Uploaded skills are written to `/skills/<name>/` on flash and
reloaded on every boot.

## Examples

- `src/app/builtin_skills/gpio` — minimal single-action skill
- `src/app/builtin_skills/http_get` — HTTPS access skill
- `skills/mijia` — large skill: QR-login web page via `register_endpoint`,
  credential + device caching in `data_dir`, cloud API calls via the HTTP
  helpers
