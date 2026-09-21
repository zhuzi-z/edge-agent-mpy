# Mijia Home Assistant Skill

This skill controls Mijia smart devices through Xiaomi's OAuth2 API for Home Assistant. It uses plain HTTPS requests with a Bearer token and JSON bodies, and stores its credentials on the device.

## Run

On the device, upload the skill:

```bash
python3 skills/upload.py --base-url http://<device-ip> mijia-ha
```

For local development with the MicroPython Unix port:

```bash
micropython -X heapsize=8M skills/mijia-ha/serve.py 8911
micropython skills/mijia-ha/test.py
```

The local server uses the same registry, HTTP helpers, endpoint registration, and `code.py` execution path as the device. Data is stored under `tmp/mijia-ha-data/skills/mijia-ha/`.

## Actions

| Action | Description | Token required |
|---|---|---|
| `login` | Serves a local login page and returns `http://<device-ip>/mijia-ha/`; sign in by scanning the QR code with the Mi Home app. | No |
| `token` | Reports the saved credential, refreshes it when it is near expiry, and verifies it with a Bearer request. | No |
| `list_devices` | Lists account devices with `name`, `did`, `model`, `online`, and `room`. Pass `refresh=true` to bypass the cache. | Yes |
| `device_spec` | Returns a model's property specification (`siid`, `piid`, name, description, format, access, range, enums). Pass `filter` to narrow large specifications. | No |
| `get_prop` | Reads one property or a batch of properties for one device. | Yes |
| `set_prop` | Writes one property or a batch of properties for one device. | Yes |

Use `list_devices` to find the `did` and model, `device_spec` to determine the exact `siid` and `piid`, and then call `get_prop` or `set_prop`. For multiple properties on the same device, use the `props` array so they are sent in one request.

## System Prompt Tip

For devices you use every day, copy their `did`, `model`, and useful `siid`/`piid` values into the agent's system prompt (WebUI → settings). This lets the agent call `get_prop` or `set_prop` directly instead of spending several LLM rounds on `list_devices` → `device_spec` first, and is safer against the tool-round limit.

After `login`, ask the agent to list your devices and show the specification for a model. Copy only the lines you need, for example:

```text
Bedroom AC: skill=mijia-ha did=123456789 model=zhimi.aircondition.ma1
  power: siid=2 piid=1 (bool); target temp: siid=2 piid=4 (16-31)
```

Keep the prompt short — it is attached to every request. Do not add unverified `siid`/`piid` values; obtain them from `device_spec` first.

## OAuth Flow

The device performs the full OAuth flow itself; the browser only displays the QR code:

```text
GET  /oauth2/authorize              Start authorization without an existing session
GET  /longPolling/loginUrl          Fetch the QR code and long-polling URL
GET  <long-polling URL>             Wait for confirmation after the user scans
GET  <callback URL>                 Follow redirects and collect the authorization code
GET  /app/v2/ha/oauth/get_token     Exchange the code for access and refresh tokens
```

Xiaomi accepts the registered Home Assistant redirect origin (`http://homeassistant.local:8123`). The device does not need to resolve that host: it follows the redirect chain itself and reads the authorization code from the final `Location` header. No Home Assistant server, second listening port, or manual code entry is required.

## Cloud API

Authenticated requests use `Authorization: Bearer <token>`, `X-Client-BizId: haapi`, `X-Client-AppId: <client_id>`, and `Content-Type: application/json`.

| Endpoint | Purpose |
|---|---|
| `/app/v2/homeroom/gethome` | Fetch account homes, rooms, and device IDs. |
| `/app/v2/home/device_list_page` | Fetch device details, paginating through the account. |
| `/app/v2/miotspec/prop/get` | Read device properties in batch. |
| `/app/v2/miotspec/prop/set` | Write device properties in batch. |
| `https://home.miot-spec.com/spec/<model>` | Fetch public MIoT specifications. |

If an access token expires, the skill automatically refreshes it once and retries the failed API request. If refresh also fails, it starts a new QR login.

## Stored Files

| File | Contents |
|---|---|
| `auth.json` | `device_id`, `uid`, `access_token`, `refresh_token`, expiry timestamps, `redirect_uri`, and `client_id`. |
| `devices.json` | Device-list cache, tagged by account UID. |
| `spec.json` | Per-model property-specification cache. |

On the device, these files are stored in `/config/skills/mijia-ha/`. Xiaomi rotates the refresh token on every refresh, so the skill persists the new token pair after every successful refresh.
