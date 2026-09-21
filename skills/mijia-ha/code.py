"""Mijia skill on Xiaomi's HA OAuth2 API (ESP32 / MicroPython).

Same user-facing shape as the old `mijia` skill -- the device hosts a login
page and the user scans a QR with the Mi Home app -- but the credential it ends
up with is a Home Assistant OAuth2 `access_token` instead of an RC4-signed APP
session:

    GET  /oauth2/authorize (no session)  -> 302 serviceLogin?sid=oauth2.0
    GET  /longPolling/loginUrl           -> {qr, lp}          (user scans)
    GET  lp (6s timeout, polled)         -> session cookies + callback
    follow the callback chain            -> 302 redirect_uri?code=...
    GET  /app/v2/ha/oauth/get_token      -> access_token + refresh_token
    POST /app/v2/homeroom/gethome        -> Bearer only, ZERO crypto

Xiaomi whitelists `redirect_uri` by origin, and the only origin accepted for
this client_id is http://homeassistant.local:8123 -- a host the device can
neither resolve nor needs to: it follows the chain itself and reads the `code`
out of the Location header, stopping before that hop. So no mDNS name, no
second listening port and no copy/paste (the paste box on the page is only a
fallback for the one-time consent page).

Actions: `login` starts the QR and serves the page, `token` shows / verifies /
refreshes what is on disk, and `list_devices` / `device_spec` / `get_prop` /
`set_prop` are the control surface -- the same four actions the signed skill
had, with the same output, so this replaces it action for action.

Control is four endpoints on ha.api.io.mi.com, each a POST of plain JSON
wearing _bearer_headers and answered with plain JSON:

    /app/v2/homeroom/gethome         homes: uid, dids, rooms
    /app/v2/home/device_list_page    every device the account can see (paged)
    /app/v2/miotspec/prop/get        read properties (batched)
    /app/v2/miotspec/prop/set        write properties (batched)

`device_spec` stays on the public home.miot-spec.com and needs no auth at all.
"""

import binascii
import hashlib
import json
import os
import time

CLIENT_ID = "2882303761520251711"  # the client_id Xiaomi registered for HA
AUTH_HOST = "account.xiaomi.com"
API_HOST = "ha.api.io.mi.com"
LOGIN_URL = "https://" + AUTH_HOST + "/longPolling/loginUrl"
TOKEN_PATH = "/app/v2/ha/oauth/get_token"
# Whitelisted by ORIGIN (the path is free), and it has to be byte-identical in
# authorize, get_token and every refresh -- hence a constant, never derived.
REDIRECT_URI = "http://homeassistant.local:8123/api/webhook/edge-agent"
CB_HOST = "homeassistant.local"  # where the code lands; unreachable for us
HOME_PAYLOAD = {
    "limit": 150,
    "fetch_share": True,
    "fetch_share_dev": True,
    "plat_form": 0,
    "app_ver": 9,
}
UA = (
    "Mozilla/5.0 (Linux; Android 10; MI 9) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
)
# This (account, client_id, device_id) triple already went through the one-time
# consent click, so a scan lands straight on `code`. A fresh random device_id
# works too, it just shows the consent page once (README explains the escape).
DEFAULT_DEVICE_ID = "ha.cd743d403b2b18bb4d84da18e51fe3f8"

QR_TTL = 300  # loginUrl reports timeout=300 for sid=oauth2.0
LP_TIMEOUT = 6  # lp is a ~120s long-poll; 6s keeps the device server responsive
AUTH_FILE = data_dir + "auth.json"
CACHE_FILE = data_dir + "devices.json"
SPEC_CACHE_FILE = data_dir + "spec.json"
SPEC_HOST = "home.miot-spec.com"  # public MIoT spec pages, no auth

_S = {}  # the login in progress (QR, cookie jar, result)
_AUTH = {}  # the persisted credential
# RAM cache for device_spec results (model -> formatted text). Specs are static
# per model; cleared wholesale once the cap is reached.
_SPEC_CACHE = {}


class _Err(Exception):
    """A login/token step failure worth showing to the user."""


# ============================ small utils ===================================
# MicroPython has no urllib: build and parse query strings by hand.


def _pct(s):
    out = []
    for c in s:
        if ("A" <= c <= "Z") or ("a" <= c <= "z") or ("0" <= c <= "9") or c in "-_.~":
            out.append(c)
        else:
            for b in c.encode("utf-8"):
                out.append("%%%02X" % b)
    return "".join(out)


def _qs(d):
    return "&".join("{}={}".format(k, _pct(str(v))) for k, v in d.items())


def _unpct(s):
    out = bytearray()
    bs = s.encode("utf-8")
    i = 0
    n = len(bs)
    while i < n:
        if bs[i] == 0x25 and i + 2 < n:  # '%'
            try:
                out.append(int(bs[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(bs[i])
        i += 1
    return out.decode("utf-8", "ignore")


def _qparams(url):
    q = url.split("?", 1)
    if len(q) < 2:
        return {}
    out = {}
    for pair in q[1].split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[_unpct(k)] = _unpct(v)
    return out


def _split_url(url):
    """https://host[:port]/path?query -> (host, port, '/path?query')."""
    rest = url.split("://", 1)[-1]
    slash = rest.find("/")
    hostport = rest[:slash] if slash >= 0 else rest
    path = rest[slash:] if slash >= 0 else "/"
    host, _sep, p = hostport.partition(":")
    try:
        port = int(p) if p else 443
    except ValueError:
        port = 443
    return host, port, path


def _resolve(base, loc):
    if loc.startswith("http://") or loc.startswith("https://"):
        return loc
    if loc.startswith("/"):
        return "https://" + _split_url(base)[0] + loc
    return base.rsplit("/", 1)[0] + "/" + loc


def _hdr(hb, name):
    nl = name.lower() + ":"
    for line in hb.split("\r\n"):
        if line.lower().startswith(nl):
            return line.split(":", 1)[1].strip()
    return ""


def _set_cookies(hb):
    """The device httpclient keeps no cookie jar, so harvest Set-Cookie here."""
    out = {}
    for line in hb.split("\r\n"):
        if line.lower().startswith("set-cookie:"):
            pair = line.split(":", 1)[1].strip().split(";")[0].strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _cookie_hdr(jar):
    return "; ".join("{}={}".format(k, v) for k, v in jar.items())


def _svc(text):
    """Strip the ``&&&START&&&`` anti-JSON-hijack prefix Xiaomi prepends."""
    return text[11:] if text.startswith("&&&START&&&") else text


def _sha1hex(s):
    # this MicroPython build has no hashlib .hexdigest()
    return binascii.hexlify(hashlib.sha1(s.encode("utf-8")).digest()).decode()


def _get(url, jar=None, timeout=15, max_bytes=20000):
    h = {"User-Agent": UA}
    if jar:
        h["Cookie"] = _cookie_hdr(jar)
    host, port, path = _split_url(url)
    return http_get(host, port, path, h, timeout=timeout, max_bytes=max_bytes)


# ============================ credential on disk =============================


def _auth_load():
    """Read auth.json, seeding a stable device_id on first run.

    The consent the user clicks once is bound to (account, client_id,
    device_id), so device_id must survive reboots -- a new one means a new
    consent page.
    """
    global _AUTH
    if _AUTH:
        return _AUTH
    try:
        with open(AUTH_FILE, "r") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _AUTH = d
    except (OSError, ValueError):
        _AUTH = {}
    if not _AUTH.get("device_id"):
        _AUTH["device_id"] = DEFAULT_DEVICE_ID or (
            "ha." + binascii.hexlify(os.urandom(16)).decode()
        )
        _auth_save()
    return _AUTH


def _auth_save():
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump(_AUTH, f)
    except OSError as e:
        print("[mijia-ha] save auth failed: {}".format(e))


def _authorize_url(device_id):
    """The authorize URL, shaped exactly like the official HA integration's."""
    return "https://{}/oauth2/authorize?{}".format(
        AUTH_HOST,
        _qs(
            {
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "response_type": "code",
                "device_id": device_id,
                "state": _sha1hex("d=" + device_id),
                "skip_confirm": "true",
            }
        ),
    )


def _follow(url, jar, max_hops=12, max_bytes=60000):
    """GET, following 3xx ourselves and accumulating cookies across hops.

    Returns (urls, status, body) where ``urls`` is every Location seen, in
    order. A hop to ``redirect_uri`` is recorded but NOT requested: that host
    does not resolve here, and its query is where the `code` lives. The 60 KB
    cap is for the consent page (23 KB); a 20 KB cap truncates it into an
    unreadable error.
    """
    urls = []
    cur = url
    status = 0
    body = b""
    for _ in range(max_hops):
        status, hb, body = _get(cur, jar, max_bytes=max_bytes)
        for k, v in _set_cookies(hb).items():
            jar[k] = v
        loc = _hdr(hb, "location")
        if status not in (301, 302, 303, 307, 308) or not loc:
            break
        cur = _resolve(cur, loc)
        urls.append(cur)
        if _split_url(cur)[0] == CB_HOST:
            break
    return urls, status, body


def _qr_expired():
    return time.time() - _S.get("created", 0) > _S.get("ttl", QR_TTL)


def _find_code(urls):
    for u in urls:
        c = _qparams(u).get("code")
        if c:
            return c
    return None


# ============================ token exchange ================================


def _err_text(obj):
    """Flatten Xiaomi's nested OAuth failure, e.g.
    {"code":-6,"message":"{\"error\":96013,\"error_description\":\"invalid...\"}"}.
    """
    m = obj.get("message")
    if isinstance(m, str) and m.startswith("{"):
        try:
            m = json.loads(m)
        except ValueError:
            return m[:120]
    if isinstance(m, dict):
        return "{} {}".format(m.get("error", ""), m.get("error_description", ""))
    return m or obj.get("error_description") or str(obj)[:120]


def _oauth(data):
    """One get_token call (code -> token, or refresh_token -> token)."""
    st, _hb, body = _get(
        "https://{}{}?{}".format(API_HOST, TOKEN_PATH, _qs({"data": json.dumps(data)})),
        timeout=20,
        max_bytes=8000,
    )
    try:
        obj = json.loads(body.decode("utf-8", "ignore"))
    except ValueError:
        raise _Err("get_token HTTP {}: {}".format(st, str(body)[:120]))
    res = obj.get("result") or {}
    if obj.get("code") != 0 or not res.get("access_token"):
        raise _Err("get_token code={} {}".format(obj.get("code"), _err_text(obj)))
    return res


def _store_token(res):
    a = _auth_load()
    exp = int(res.get("expires_in") or 0)
    a["access_token"] = res["access_token"]
    a["refresh_token"] = res.get("refresh_token", "")
    a["expires_in"] = exp
    a["obtained_ts"] = int(time.time())
    a["expires_ts"] = a["obtained_ts"] + exp
    a["redirect_uri"] = REDIRECT_URI
    _auth_save()


def _refresh():
    """refresh_token -> a new pair. Xiaomi rotates it, so always persist both."""
    rt = _auth_load().get("refresh_token")
    if not rt:
        raise _Err("no refresh_token on disk -- login again")
    _store_token(
        _oauth({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "refresh_token": rt})
    )


def _bearer_headers(token):
    """The entire auth story of this API: four headers, no signing, no crypto."""
    return {
        "X-Client-BizId": "haapi",
        "Content-Type": "application/json",
        # the reference implementation sends "Bearer<token>" with no space
        "Authorization": "Bearer" + token,
        "X-Client-AppId": CLIENT_ID,
    }


def _probe():
    """Prove the token with one Bearer call. Never raises: the page shows this."""
    if not _auth_load().get("access_token"):
        return {"error": "no token on disk"}
    try:
        homes = _api("/app/v2/homeroom/gethome", HOME_PAYLOAD)
    except _ApiErr as e:
        return {"error": "code={} {}".format(e.code, e.msg)}
    return {
        "homes": [
            {"name": h.get("name"), "dids": len(h.get("dids") or [])}
            for h in (homes.get("homelist") or [])
        ]
    }


# ============================ QR login ======================================


def _start_login():
    """Kick authorize (no session) to learn the right serviceLogin, then get a QR.

    A session for sid=mijia is NOT accepted by authorize: its 302 names the
    sid=oauth2.0 login entry, and only a session minted there carries the
    oauth2.0 serviceToken the chain needs.
    """
    a = _auth_load()
    did = a["device_id"]
    st, hb, _b = _get(_authorize_url(did), timeout=15, max_bytes=8000)
    entry = _hdr(hb, "location")
    if st not in (301, 302, 303, 307, 308) or "serviceLogin" not in entry:
        raise _Err("authorize gave no serviceLogin redirect (HTTP {})".format(st))

    p = _qparams(entry)
    p["theme"] = ""
    p["bizDeviceType"] = ""
    p["_hasLogo"] = "false"
    p["_qrsize"] = "240"
    p["_dc"] = str(int(time.time() * 1000))
    st2, _hb2, b2 = _get(LOGIN_URL + "?" + _qs(p), timeout=15, max_bytes=20000)
    try:
        ld = json.loads(_svc(b2.decode("utf-8", "ignore")))
    except ValueError:
        raise _Err("loginUrl bad JSON (HTTP {}): {}".format(st2, str(b2)[:120]))
    if ld.get("code", -1) != 0 or not ld.get("lp"):
        raise _Err("loginUrl code={} {}".format(ld.get("code"), ld.get("desc", "")))

    _S.clear()
    _S.update(
        device_id=did,
        lp=ld["lp"],
        qr=ld.get("qr", ""),
        jar={},
        created=time.time(),
        ttl=int(ld.get("timeout") or QR_TTL),
        done=False,
        step="qr",
    )
    return _S


def _poll():
    """One short-timeout poll of lp.

    Returns the status dict the page polls for. lp blocks until the user scans
    or ~120s pass, so a socket timeout -- not a reply -- is what "still waiting"
    looks like.
    """
    if not _S.get("lp"):
        return {"status": "idle"}
    if _qr_expired():
        return {"status": "expired"}
    jar = _S.setdefault("jar", {})
    try:
        st, hb, body = _get(_S["lp"], jar, timeout=LP_TIMEOUT, max_bytes=8000)
    except Exception:  # noqa: BLE001  timeout -> the page polls again
        return {"status": "waiting"}
    for k, v in _set_cookies(hb).items():
        jar[k] = v
    if st != 200 or not body:
        return {"status": "waiting"}
    try:
        obj = json.loads(_svc(body.decode("utf-8", "ignore")))
    except ValueError:
        return {"status": "waiting"}
    code = obj.get("code", -1)
    if code != 0:
        if code in (70016, 70017):  # QR expired / consumed
            return {"status": "expired"}
        return {"status": "waiting"}
    if not obj.get("passToken") and not obj.get("userId"):
        return {"status": "waiting"}

    # Scanned and confirmed. It is the cookie jar (already updated in place)
    # that carries the session into the next authorize hop; from the lp payload
    # the only field worth keeping is the account id, which tags the device cache.
    if obj.get("userId"):
        _S["userId"] = str(obj["userId"])
    return _finish(obj.get("location"))


def _finish(callback):
    """callback -> ... -> redirect_uri?code=... -> get_token."""
    jar = _S.setdefault("jar", {})
    urls, status, body = [], 0, b""
    if callback:
        urls, status, body = _follow(callback, jar)
    code = _find_code(urls)
    if not code:
        # The callback chain can stop one hop short of the OAuth client (it
        # ends on a page for the login service). Ask authorize again now that
        # the jar holds the oauth2.0 serviceToken -- with a consented device_id
        # this is the hop that hands out the code.
        urls2, status, body = _follow(_authorize_url(_S["device_id"]), jar)
        urls += urls2
        code = _find_code(urls2)
    if not code:
        det = {
            "step": "code",
            "hops": [u[:160] for u in urls],
            "status": status,
            "page_bytes": len(body),
            "cookies": sorted(jar.keys()),
        }
        _S.update(step="code", detail=det)
        return {"status": "consent", "detail": det}
    try:
        return {"status": "ok", "detail": _finish_code(code)}
    except Exception as e:  # noqa: BLE001  surfaced on the page
        det = {"step": "token", "msg": str(e)}
        _S.update(step="token", detail=det)
        return {"status": "error", "detail": det, "msg": str(e)}


def _finish_code(code):
    """code -> token on disk -> probe. Returns the detail dict the page shows."""
    a = _auth_load()
    _store_token(
        _oauth(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": code,
                "device_id": a["device_id"],
            }
        )
    )
    if _S.get("userId"):
        # the account id, used to tag the device cache (a different account
        # logging in must not inherit the previous one's device list)
        a["uid"] = str(_S["userId"])
        _auth_save()
    det = {
        "step": "ok",
        "code": code[:16] + "...",
        "expires_in": a.get("expires_in"),
        "access_token": (a.get("access_token") or "")[:16] + "...",
    }
    try:
        det["probe"] = _probe()
    except Exception as e:  # noqa: BLE001  the token is saved either way
        det["probe"] = {"error": str(e)}
    _S.update(step="ok", done=True, detail=det)
    return det


# ============================ control API (Bearer) ===========================
# Every control call is a plain HTTPS POST of JSON wearing _bearer_headers, and
# the reply is plain JSON. This is what replaces the RC4/SHA1 signed APP API.


class _ApiErr(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
        super().__init__("code={} {}".format(code, msg))


# invalid/expired credential, as reported by the miot cloud
_AUTH_CODES = {-10030, -10020, -704010000, -704090001}

# miot per-item error codes (from Do1e/mijia-api errors.py), annotated into
# get/set results so the caller learns that a piid does not exist instead of
# blind-probing neighbouring ids.
_MIOT_ERR = {
    -704010000: "unauthorized, device may have been removed",
    -704030013: "property not readable",
    -704030023: "property not writable",
    -704040002: "service does not exist",
    -704040003: "property does not exist",
    -704040004: "event does not exist",
    -704040005: "action does not exist",
    -704042001: "device does not exist",
    -704042011: "device offline",
    -704053036: "device operation timeout",
    -704053100: "operation not allowed in current state",
    -704220043: "property value out of range",
}


def _is_auth(code, msg):
    if code in _AUTH_CODES:
        return True
    m = (msg or "").lower()
    return ("token" in m) or ("auth" in m) or ("login" in m)


def _api_once(uri, payload, max_bytes):
    """One Bearer POST, no retry. Returns the reply's `result`."""
    tok = _auth_load().get("access_token")
    if not tok:
        raise _ApiErr(0, "no token on disk -- run login first")
    st, _hb, raw = http_post_json(
        API_HOST,
        443,
        uri,
        json.dumps(payload).encode(),
        _bearer_headers(tok),
        timeout=20,
        max_bytes=max_bytes,
    )
    if st == 401:
        raise _ApiErr(401, "unauthorized (HTTP 401)")
    try:
        obj = json.loads(raw.decode("utf-8", "ignore"))
    except ValueError:
        raise _ApiErr(st, "bad JSON (HTTP {}): {}".format(st, str(raw)[:120]))
    code = obj.get("code")
    if code == 0 and "result" in obj:
        return obj["result"]
    raise _ApiErr(code, _err_text(obj))


def _api(uri, payload, max_bytes=60000):
    """`_api_once`, retried once behind a token refresh.

    That is all the session upkeep this API needs -- the signed one had to send
    the user back to the QR page instead. When the refresh fails too, the
    original auth error propagates, and that is what re-opens the QR page.
    """
    try:
        return _api_once(uri, payload, max_bytes)
    except _ApiErr as e:
        if not _is_auth(e.code, e.msg):
            raise
        try:
            _refresh()
        except Exception:  # noqa: BLE001  the auth error says it all
            raise e
        return _api_once(uri, payload, max_bytes)


# ---- device list -----------------------------------------------------------
# list_devices runs before nearly every get_prop/set_prop, so its result is
# cached in CACHE_FILE: refreshed after a login, cleared when a control call
# fails (a did may have moved), rebuilt by list_devices with refresh=true.


def _fetch_devices(limit=300):
    """(uid, devices): gethome for the account and rooms, device_list_page for
    the rest.

    device_list_page with an empty `dids` returns everything the account can
    see, so the per-home iteration the signed API needed is gone. Only compact
    dicts are kept (name/did/model/online/room) to keep the cache small.
    """
    homes = _api("/app/v2/homeroom/gethome", HOME_PAYLOAD)
    uid = ""
    room = {}
    for home in (homes.get("homelist") or []) + (homes.get("share_home_list") or []):
        if not uid and home.get("uid"):
            uid = str(home["uid"])
        for r in home.get("roomlist") or []:
            for did in r.get("dids") or []:
                room[did] = r.get("name")
    out = []
    start = ""
    for _ in range(10):  # pagination cap
        req = {"limit": 200, "get_split_device": True, "get_third_device": True, "dids": []}
        if start:
            req["start_did"] = start
        page = _api("/app/v2/home/device_list_page", req)
        for d in page.get("list") or []:
            did = d.get("did")
            if not did:
                continue
            item = {
                "name": d.get("name"),
                "did": did,
                "model": d.get("model"),
                "online": bool(d.get("isOnline")),
            }
            if room.get(did):
                item["room"] = room[did]
            out.append(item)
            if len(out) >= limit:
                return uid, out
        if page.get("has_more") and page.get("next_start_did"):
            start = page["next_start_did"]
        else:
            break
    return uid, out


def _cache_load(uid):
    """Cached device list, or None when missing / corrupt / another account's."""
    try:
        with open(CACHE_FILE, "r") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict) or not isinstance(c.get("devices"), list):
        return None
    if uid and c.get("uid") != uid:
        return None
    return c["devices"]


def _cache_save(uid, devices):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"uid": uid, "ts": int(time.time()), "devices": devices}, f)
    except OSError as e:
        print("[mijia-ha] save device cache failed: {}".format(e))


def _cache_clear():
    try:
        os.remove(CACHE_FILE)
    except OSError:
        pass


def _truncate(s, limit=4000):
    return s[:limit] + "...(truncated)" if len(s) > limit else s


def _format_devices(devices):
    lines = []
    for d in devices:
        line = "{} | did={} | model={} | online={}".format(
            d.get("name"), d.get("did"), d.get("model"), d.get("online", False)
        )
        if d.get("room"):
            line += " | room={}".format(d["room"])
        lines.append(line)
    return _truncate("\n".join(lines)) or "no devices"


def _list_devices(refresh=False):
    a = _auth_load()
    uid = str(a.get("uid") or "")
    if not refresh:
        cached = _cache_load(uid)
        if cached is not None:
            return _format_devices(cached)
    got, devices = _fetch_devices()
    if got and got != uid:
        # the paste path never sees the lp userId, so learn the account here
        a["uid"] = got
        _auth_save()
        uid = got
    _cache_save(uid, devices)
    return _format_devices(devices)


# ---- device_spec (public, no auth) -----------------------------------------


def _spec_cache_load():
    """Load persisted per-model device_spec results (survives reboot)."""
    try:
        with open(SPEC_CACHE_FILE, "r") as f:
            c = json.load(f)
        if isinstance(c, dict):
            _SPEC_CACHE.update(c)
    except (OSError, ValueError):
        pass


def _spec_cache_save():
    try:
        with open(SPEC_CACHE_FILE, "w") as f:
            json.dump(_SPEC_CACHE, f)
    except OSError as e:
        print("[mijia-ha] save spec cache failed: {}".format(e))


_spec_cache_load()


def _pad3(n):
    s = str(n)
    while len(s) < 3:
        s = "0" + s
    return s


def _spec_filter(text, flt):
    """Narrow a spec listing to lines matching the keyword (case-insensitive).

    Big devices (vacuums, AC partners) have long specs that eat LLM tokens; the
    filter lets the caller fetch only the relevant properties.
    """
    flt = (flt or "").strip().lower()
    if not flt:
        return text
    lines = [ln for ln in text.split("\n") if flt in ln.lower()]
    return "\n".join(lines) or "no property matches filter '{}'".format(flt)


def _device_spec(model, flt=None):
    """Public MIoT spec lookup (no auth), cached per model. Returns a compact
    property list with the Chinese descriptions from the page's i18n table."""
    hit = _SPEC_CACHE.get(model)
    if hit is not None:
        return _spec_filter(hit, flt) if flt else hit
    headers = {"User-Agent": "mijia-esp32", "Accept-Encoding": "identity"}
    # Spec pages embed the full JSON in HTML and big devices (robot vacuums) can
    # exceed 200KB, so allow up to 1MB.
    status, _hb, body = http_get(SPEC_HOST, 443, "/spec/" + model, headers, max_bytes=1048576)
    if status != 200:
        return "device_spec HTTP " + str(status)
    text = body.decode("utf-8", "ignore")
    key = '<script data-page="app" type="application/json">'
    i = text.find(key)
    if i < 0:
        return "spec not found for " + model
    i += len(key)
    j = text.find("</script>", i)
    if j < 0:
        return "spec parse error for " + model
    try:
        content = json.loads(text[i:j])
        props = content["props"]
        services = props["tree"]["services"]
    except (KeyError, ValueError):
        return "spec structure error for " + model
    zh = (props.get("i18n") or {}).get("zh_cn") or {}
    lines = []
    for svc in services:
        siid = svc.get("iid")
        for prop in svc.get("properties", []) or []:
            piid = prop.get("iid")
            access = prop.get("access", []) or []
            rw = ("r" if "read" in access else "") + ("w" if "write" in access else "")
            line = "{}.{} name={}".format(siid, piid, prop.get("type", ""))
            desc = zh.get("service:{}:property:{}".format(_pad3(siid), _pad3(piid))) or prop.get(
                "description", ""
            )
            if desc:
                line += " ({})".format(desc)
            line += " fmt={} rw={}".format(prop.get("format", ""), rw)
            vr = prop.get("valueRange")
            if vr:
                line += " range={}".format(vr)
            vl = prop.get("valueList")
            if vl:
                parts = []
                for x in vl:
                    vdesc = zh.get(x.get("i18nKey", ""), "") or x.get("description", "")
                    parts.append("{}={}".format(x.get("value"), vdesc))
                line += " values=" + "/".join(parts)
            lines.append(line)
    out = "\n".join(lines)
    result = _truncate(out) or "no properties for " + model
    if len(_SPEC_CACHE) >= 8:  # crude cap: drop everything when full
        _SPEC_CACHE.clear()
    _SPEC_CACHE[model] = result
    _spec_cache_save()
    return _spec_filter(result, flt) if flt else result


# ---- properties (read/write, batched) --------------------------------------


def _coerce(v):
    if isinstance(v, (bool, int, float)):
        return v
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_props(args, need_value):
    """Extract did + the properties to read/write.

    The batch form props=[{"siid":..,"piid":..(,"value":..)}, ...] puts several
    properties of one device into a SINGLE cloud request; the single form keeps
    did+siid+piid(+value). Returns (did, [(siid, piid, value), ...]) or raises
    ValueError with a user-facing message.
    """
    did = args.get("did")
    want = "siid, piid and value" if need_value else "siid and piid"
    props = args.get("props")
    items = []
    if isinstance(props, list) and props:
        if not did:
            raise ValueError("missing did")
        for p in props:
            if not isinstance(p, dict):
                raise ValueError("each props item must be an object with " + want)
            if p.get("siid") is None or p.get("piid") is None:
                raise ValueError("each props item must have " + want)
            value = p.get("value")
            if need_value and value is None:
                raise ValueError("each props item must have " + want)
            items.append((int(p.get("siid")), int(p.get("piid")), value))
        return did, items
    siid = args.get("siid")
    piid = args.get("piid")
    if not did or siid is None or piid is None:
        raise ValueError("missing did/siid/piid")
    value = args.get("value")
    if need_value and value is None:
        raise ValueError("missing value")
    return did, [(int(siid), int(piid), value)]


def _annotate(params, result, ok_codes, ok_text, hint_default):
    """One line per requested property, with miot error codes annotated.

    ``ok_text(param, item)`` renders a success line (without the siid/piid tag,
    which is added for batches so the LLM can match values to properties)."""

    def hint_for(item, code):
        return _MIOT_ERR.get(code) or item.get("message") or hint_default

    if len(params) == 1:
        item = result[0]
        code = item.get("code")
        if code in ok_codes:
            return ok_text(params[0], item)
        return "code={} ({})".format(code, hint_for(item, code))
    lines = []
    for param, item in zip(params, result):
        tag = "siid={} piid={}".format(param["siid"], param["piid"])
        code = item.get("code")
        if code in ok_codes:
            lines.append("{} {}".format(tag, ok_text(param, item)))
        else:
            lines.append("{} code={} ({})".format(tag, code, hint_for(item, code)))
    return "\n".join(lines)


def _get_prop(args):
    try:
        did, items = _parse_props(args, need_value=False)
    except ValueError as e:
        return str(e)
    params = [{"did": did, "siid": siid, "piid": piid} for siid, piid, _v in items]
    r = _api("/app/v2/miotspec/prop/get", {"datasource": 1, "params": params})
    if not (isinstance(r, list) and r):
        return str(r)
    return _annotate(
        params,
        r,
        (0,),
        lambda _param, item: "code=0 value={}".format(item.get("value")),
        "check siid/piid via device_spec",
    )


def _set_prop(args):
    try:
        did, items = _parse_props(args, need_value=True)
    except ValueError as e:
        return str(e)
    params = [
        {"did": did, "siid": siid, "piid": piid, "value": _coerce(value)}
        for siid, piid, value in items
    ]
    r = _api("/app/v2/miotspec/prop/set", {"params": params})
    if not (isinstance(r, list) and r):
        return str(r)
    return _annotate(
        params,
        r,
        (0, 1),
        lambda _param, item: "code={} ok".format(item.get("code")),
        "check siid/piid/value via device_spec",
    )


# ============================ web handlers ==================================

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mijia Sign In</title><style>
body{font:16px/1.55 system-ui,-apple-system,"Segoe UI",Arial;max-width:440px;margin:40px auto;padding:0 20px;color:#111}
h1{font-size:22px;margin:0 0 6px}
.lead{color:#555;margin:0 0 20px}
.card{border:1px solid #e5e5e5;border-radius:14px;padding:22px;text-align:center;background:#fafafa}
img{width:230px;height:230px;border:1px solid #ddd;border-radius:10px;background:#fff}
#s{min-height:25px;margin:14px 0 10px;font-weight:600}
.ok{color:#0a7d28}.warn{color:#b45309}.bad{color:#b91c1c}
button{background:#2563eb;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:15px;cursor:pointer}
details{margin-top:24px;font-size:14px;color:#555}
summary{cursor:pointer;color:#2563eb}
input{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccc;border-radius:8px;font-size:14px;margin:10px 0}
pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:11px;max-height:300px;text-align:left}
a{color:#2563eb}
</style></head><body>
<h1>Mijia Sign In</h1>
<p class="lead">Open the <b>Mi Home</b> app and scan this code.</p>
<div class="card">
<img id="q" src="{QR}">
<p id="s">Waiting for you to scan&hellip;</p>
<button onclick="reqr()">New code</button>
</div>
<details id="help"><summary>Scanning isn&#39;t working?</summary>
<p>Open the <a href="{LINK}" target="_blank">sign-in page</a> in a browser instead and approve the
request. The page it lands on will not load &mdash; that is expected. Copy the code from its address
bar and paste it here.</p>
<input id="c" placeholder="Paste the code">
<p><button onclick="paste()">Finish sign-in</button></p>
</details>
<details id="diag"><summary>Details</summary><pre id="out">Nothing yet.</pre></details>
<script>
var t=null;
function say(m,c){var s=document.getElementById('s');s.textContent=m;s.className=c||'';}
function diag(j){document.getElementById('out').textContent=JSON.stringify(j,null,2);}
function qr(u){document.getElementById('q').src=u+'&_dc='+Date.now();}
function reqr(){clearTimeout(t);say('Getting a new code...');
 fetch('/mijia-ha/refresh').then(function(r){return r.json();}).then(function(j){
  if(j.status==='error'){say('Could not get a new code.','bad');diag(j);return;}
  qr(j.qr);say('Waiting for you to scan...');poll();
 }).catch(function(){say('Could not get a new code.','bad');});}
function paste(){var c=document.getElementById('c').value.trim();if(!c)return;
 say('Signing in...');
 fetch('/mijia-ha/code',{method:'POST',body:c}).then(function(r){return r.json();})
 .then(function(j){
  if(j.status==='ok'){say('Signed in. You can close this page.','ok');}
  else{say('That code did not work.','bad');}
  diag(j);}).catch(function(){say('That code did not work.','bad');});}
function poll(){fetch('/mijia-ha/status').then(function(r){return r.json();}).then(function(j){
 if(j.status==='ok'){say('Signed in. You can close this page.','ok');diag(j.detail||j);return;}
 if(j.status==='refresh'){qr(j.qr);say('That code expired. Here is a new one.','warn');}
 else if(j.status==='waiting'){say('Waiting for you to scan...');}
 else if(j.status==='consent'){say('One more step is needed. See the help below.','warn');
  document.getElementById('help').open=true;diag(j.detail||j);}
 else if(j.status==='error'){say('Sign-in failed. Open Details below and send it to me.','bad');
  diag(j.detail||j);}
 t=setTimeout(poll,2500);}).catch(function(){t=setTimeout(poll,2500);});}
poll();
</script></body></html>"""


def _json(obj):
    return (200, "OK", "application/json", json.dumps(obj))


def _h_page(server, method, path, body):
    if not _S.get("done") and (not _S.get("lp") or _qr_expired()):
        # Opening the page is enough to start a login: no agent round-trip.
        try:
            _start_login()
        except Exception as e:  # noqa: BLE001  say so instead of showing a dead QR
            return (
                200,
                "OK",
                "text/plain; charset=utf-8",
                "Sign-in could not start: {}".format(e),
            )
    html = _PAGE.replace("{QR}", _S.get("qr", "")).replace(
        "{LINK}", _authorize_url(_auth_load().get("device_id", ""))
    )
    return (200, "OK", "text/html; charset=utf-8", html)


def _h_refresh(server, method, path, body):
    try:
        _start_login()
    except Exception as e:  # noqa: BLE001
        return _json({"status": "error", "msg": str(e)})
    return _json({"status": "refresh", "qr": _S.get("qr", "")})


def _h_status(server, method, path, body):
    if _S.get("done"):
        return _json({"status": "ok", "detail": _S.get("detail")})
    r = _poll()
    if r["status"] in ("expired", "idle"):
        return _h_refresh(server, method, path, body)
    return _json(r)


def _h_code(server, method, path, body):
    """Manual fallback: a pasted code (or the whole callback URL)."""
    # the server hands handlers a decoded str body (app.util.safe_decode)
    txt = (body or "").strip()
    code = _qparams(txt).get("code") or txt
    if not code:
        return _json({"status": "error", "msg": "empty code"})
    try:
        return _json({"status": "ok", "detail": _finish_code(code)})
    except Exception as e:  # noqa: BLE001
        return _json({"status": "error", "msg": str(e)})


# ============================ entry point ===================================


def _login():
    try:
        release_endpoints()  # clear endpoints from an earlier attempt
    except Exception:  # noqa: BLE001
        pass
    try:
        _start_login()
    except Exception as e:  # noqa: BLE001
        return "Login start failed: " + str(e)
    register_endpoint("GET", "/mijia-ha/", _h_page)
    register_endpoint("GET", "/mijia-ha", _h_page)
    register_endpoint("GET", "/mijia-ha/status", _h_status)
    register_endpoint("GET", "/mijia-ha/refresh", _h_refresh)
    register_endpoint("POST", "/mijia-ha/code", _h_code)
    try:
        ip = local_ip()
    except Exception:  # noqa: BLE001
        ip = ""
    if not ip:
        return (
            "QR ready. Open /mijia-ha/ on the device (its IP is on /info) in a browser "
            "and scan it with the Mi Home app; I save the HA token automatically."
        )
    port = "" if server_port == 80 else ":{}".format(server_port)
    return (
        "Open the link below in a browser on the same WiFi and scan the QR with the "
        "**Mi Home** app (the page detects success by itself and I save the HA token "
        "automatically):\n```\nhttp://{}{}/mijia-ha/\n```"
    ).format(ip, port)


def _token():
    """Report the stored credential, refreshing it first when it is near expiry."""
    a = _auth_load()
    if not a.get("access_token"):
        return _login_prompt()
    lines = [
        "device_id: {}".format(a.get("device_id")),
        "access_token: {}... ({} chars)".format(
            a.get("access_token", "")[:16], len(a.get("access_token", ""))
        ),
        "refresh_token: {}...".format((a.get("refresh_token") or "")[:16]),
    ]
    if int(a.get("expires_ts", 0) - time.time()) < 3600:
        try:
            _refresh()
            lines.append("near expiry: refreshed")
        except Exception as e:  # noqa: BLE001  the probe below says whether it mattered
            lines.append("near expiry: refresh failed: {}".format(e))
    a = _auth_load()
    lines.append("expires in {}s".format(int(a.get("expires_ts", 0) - time.time())))
    try:
        p = _probe()
    except Exception as e:  # noqa: BLE001  a network error is not an _ApiErr
        p = {"error": str(e)}
    # no retry loop here: _api already retries once behind a refresh
    lines.append("gethome: {}".format(p.get("error") or p.get("homes")))
    return "\n".join(lines)


def _login_prompt():
    """Asked to control something while no token is on disk."""
    return (
        "Not logged in yet (no token on disk). Say 'login mijia' and I will give you a web "
        "link to scan with the Mi Home app; after that the token keeps itself fresh."
    )


def run(args):
    args = args or {}
    action = args.get("action") or "login"

    # login, token and device_spec need no access_token
    if action == "login":
        return _login()
    if action == "token":
        return _token()
    if action == "device_spec":
        model = args.get("model")
        if not model:
            return "missing model"
        return _device_spec(model, args.get("filter"))

    if not _auth_load().get("access_token"):
        return _login_prompt()
    try:
        if action == "list_devices":
            r = args.get("refresh")
            return _list_devices(refresh=(r is True or str(r).lower() == "true"))
        if action == "get_prop":
            return _get_prop(args)
        if action == "set_prop":
            return _set_prop(args)
        return (
            "unknown action: {} (action must be one of: login, token, list_devices, "
            "device_spec, get_prop, set_prop)".format(action)
        )
    except _ApiErr as e:
        # A failed control call means the cached device list may be stale (a
        # device moved or was removed), so drop it and let list_devices re-query.
        _cache_clear()
        if _is_auth(e.code, e.msg):
            # _api already refreshed once and retried, so the credential is
            # really gone: hand back the QR page instead of a bare error.
            return "Login expired ({}).\n".format(e.msg) + _login()
        return (
            "mijia error: code={} {} (device cache cleared; run list_devices to re-check)".format(
                e.code, e.msg
            )
        )
    except _Err as e:
        return "mijia auth error: " + str(e)
    except OSError as e:
        return "mijia network error: " + str(e)
    except Exception as e:  # noqa: BLE001  surface any failure to the LLM
        return "mijia error: " + str(e)
