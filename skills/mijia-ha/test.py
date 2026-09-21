#!/usr/bin/env micropython
"""Offline tests for the mijia-ha skill.

    micropython skills/mijia-ha/test.py

No network, no device, no agent: code.py is exec'd with a fake transport that
replays the wire as observed (authorize 302 -> loginUrl -> lp -> callback ->
redirect_uri?code= -> get_token -> Bearer gethome / device_list_page /
prop.get|set), so the redirect, cookie, token and control logic all run. The
skill owns its tests -- keep them here, not in tests/ut.
"""

import json
import os
import sys
import time


# ---- minimal fs helpers (MicroPython-safe, no CPython stdlib) --------------


def _here():
    try:
        p = __file__
    except NameError:
        p = sys.argv[0]
    return (p.rsplit("/", 1)[0] if "/" in p else "") or "."


def _rmtree(path):
    try:
        for entry in os.listdir(path):
            p = path + "/" + entry
            try:
                os.remove(p)
            except OSError:
                _rmtree(p)
        os.rmdir(path)
    except OSError:
        pass


_counter = 0


def _tmpdir():
    global _counter
    _counter += 1
    d = "/tmp/mijia_ha_test_%d_%d" % (time.ticks_ms(), _counter)
    os.mkdir(d)
    return d


# ---- the wire, as the real chain was observed to look -----------------------

CLIENT_ID = "2882303761520251711"
AUTHORIZE = "https://account.xiaomi.com/oauth2/authorize?client_id=" + CLIENT_ID
ENTRY = (
    "https://account.xiaomi.com/pass/serviceLogin?callback=https%3A%2F%2F"
    "account.xiaomi.com%2Fsts%2Foauth%3Fsign%3Dabc&sid=oauth2.0&client_id=" + CLIENT_ID
)
CALLBACK = "https://account.xiaomi.com/sts/oauth?sign=abc&followup=x"
REDIRECT = "http://homeassistant.local:8123/api/webhook/edge-agent?code=AKSRV_testcode&state=s"
LP_URL = "https://ak.lp.account.xiaomi.com/lp/s?k=ticket1"
QR_URL = "https://account.xiaomi.com/pass/qr/login?ticket=ticket1&sid=oauth2.0"
TOKEN_JSON = json.dumps(
    {
        "code": 0,
        "result": {
            "access_token": "AT_" + "a" * 30,
            "refresh_token": "RT_" + "r" * 30,
            "expires_in": 259200,
        },
    }
)
# the wire shape: `message` is itself a JSON string
TOKEN_ERR = json.dumps(
    {
        "code": -6,
        "message": json.dumps(
            {"error": 96013, "error_description": "invalid authorization code", "traceId": "t"}
        ),
    }
)
SCANNED = json.dumps(
    {"code": 0, "userId": 2164169862, "passToken": "V1:pt", "ssecurity": "s", "location": CALLBACK}
)
HOMES = {
    "homelist": [
        {
            "id": "1",
            "name": "Home",
            "uid": 2164169862,
            "dids": ["d0"],
            "roomlist": [{"name": "卧室", "dids": ["d1"]}, {"name": "客厅", "dids": ["d2"]}],
        }
    ]
}
DEVICES = [
    {"did": "d1", "name": "卧室空调", "model": "lumi.acpartner.mcn02", "isOnline": True},
    {"did": "d2", "name": "台灯", "model": "yeelink.light.lamp2", "isOnline": True},
    {"did": "d0", "name": "手环", "model": "miwear.watch.n67cn", "isOnline": False},
]
SPEC_PAGE = (
    '<html><script data-page="app" type="application/json">'
    '{"props":{"tree":{"services":[{"iid":2,"properties":['
    '{"iid":1,"type":"on","format":"bool","access":["read","write"]},'
    '{"iid":3,"type":"target-temperature","format":"float","access":["read","write"],'
    '"valueRange":[16,30,1]}]}]},'
    '"i18n":{"zh_cn":{"service:002:property:003":"设定温度"}}}}'
    "</script></html>"
)


def _hdrs(*lines):
    return "".join(line + "\r\n" for line in lines)


def _ok(result):
    return (200, "", json.dumps({"code": 0, "message": "ok", "result": result}).encode())


class Fake:
    """Stands in for the injected http_get / http_post_json helpers."""

    def __init__(self):
        self.gets = []
        self.posts = []
        self.lp = "timeout"  # timeout | waiting | scanned | expired
        self.chain = "code"  # code | consent
        self.token_reply = TOKEN_JSON
        self.homes = HOMES
        self.devices = DEVICES
        self.page2 = None  # second device_list_page (pagination)
        self.get_result = None
        self.set_result = None
        self.spec_page = SPEC_PAGE
        self.fail_once = False  # next control call answers 401
        self.api_code = 0  # non-zero: every control call fails with this code
        self.api_msg = ""

    @staticmethod
    def _session(headers):
        return "serviceToken" in (headers or {}).get("Cookie", "")

    def _ctrl(self, result):
        """A control reply, honouring the failure knobs."""
        if self.fail_once:
            self.fail_once = False
            return (401, "", b"")
        if self.api_code:
            return (200, "", json.dumps({"code": self.api_code, "message": self.api_msg}).encode())
        return _ok(result)

    def http_get(self, host, port, path, headers=None, **kw):
        self.gets.append((host, path, dict(headers or {})))
        if host == "home.miot-spec.com":
            return (200, "", self.spec_page.encode())
        if path.startswith("/oauth2/authorize"):
            if self._session(headers) and self.chain == "code":
                return (302, _hdrs("Location: " + REDIRECT), b"")
            if self._session(headers):  # first authorized hop: the consent page
                return (200, "", b"<html>confirm</html>" + b"x" * 200)
            return (302, _hdrs("Location: " + ENTRY), b"")
        if path.startswith("/longPolling/loginUrl"):
            body = "&&&START&&&" + json.dumps(
                {"code": 0, "lp": LP_URL, "qr": QR_URL, "timeout": 300}
            )
            return (200, "", body.encode())
        if path.startswith("/lp/s"):
            if self.lp == "timeout":
                raise OSError("timed out")
            if self.lp == "waiting":
                return (200, "", b"&&&START&&&{}")
            if self.lp == "expired":
                return (200, "", b'&&&START&&&{"code":70016}')
            return (
                200,
                _hdrs(
                    "Set-Cookie: oauth2.0_serviceToken=ST1; Path=/",
                    "Set-Cookie: userId=9; Path=/",
                ),
                ("&&&START&&&" + SCANNED).encode(),
            )
        if path.startswith("/sts/oauth"):
            return (302, _hdrs("Location: " + AUTHORIZE), b"")
        if path.startswith("/app/v2/ha/oauth/get_token"):
            return (200, "", self.token_reply.encode())
        raise AssertionError("unexpected GET {}{}".format(host, path))

    def http_post_json(self, host, port, path, body, headers=None, **kw):
        self.posts.append((host, path, dict(headers or {}), body))
        if path == "/app/v2/homeroom/gethome":
            return self._ctrl(self.homes)
        if path == "/app/v2/home/device_list_page":
            req = json.loads(body)
            if req.get("start_did") == "next1":
                return self._ctrl(
                    {"has_more": False, "list": self.page2 or [], "next_start_did": ""}
                )
            more = self.page2 is not None
            return self._ctrl(
                {
                    "has_more": more,
                    "list": self.devices,
                    "next_start_did": "next1" if more else "",
                }
            )
        if path == "/app/v2/miotspec/prop/get":
            return self._ctrl(self.get_result or [{"code": 0, "value": True}])
        if path == "/app/v2/miotspec/prop/set":
            return self._ctrl(self.set_result or [{"code": 0}])
        raise AssertionError("unexpected POST {}".format(path))

    # ---- views the tests assert on ----

    def hosts(self):
        return [g[0] for g in self.gets]

    def paths(self):
        return [g[1] for g in self.gets]

    def post_paths(self):
        return [p[1] for p in self.posts]

    def token_paths(self):
        return [p for p in self.paths() if p.startswith("/app/v2/ha/oauth/get_token")]

    def bodies(self, path):
        return [json.loads(p[3]) for p in self.posts if p[1] == path]


def load(tmp, **fake_kw):
    """Exec code.py with the fake injected. Returns (ns, fake)."""
    with open(_here() + "/code.py") as f:
        code = f.read()
    fake = Fake()
    ns = {
        "json": json,
        "http_get": fake.http_get,
        "http_post_json": fake.http_post_json,
        "http_post": lambda *a, **kw: (200, "", b"{}"),
        "register_endpoint": lambda *a: None,
        "release_endpoints": lambda: None,
        "local_ip": lambda: "192.168.2.216",
        "data_dir": tmp + "/",
        "server_port": 80,
    }
    exec(code, ns)
    for k, v in fake_kw.items():
        setattr(fake, k, v)
    return ns, fake


def _text(html):
    """The page's visible copy: the script block and every tag removed."""
    html = html.split("<script>")[0] + html.split("</script>")[-1]
    out = []
    for chunk in html.split("<"):
        if ">" in chunk:
            out.append(chunk.split(">", 1)[1])
    return " ".join(out)


def _read(tmp, name):
    try:
        with open(tmp + "/" + name) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _seed_token(tmp, ns, **over):
    """An auth.json that is already logged in, so control calls can run."""
    a = {
        "device_id": ns["DEFAULT_DEVICE_ID"],
        "uid": "2164169862",
        "access_token": "AT_seed",
        "refresh_token": "RT_seed",
        "expires_in": 259200,
        "obtained_ts": int(time.time()),
        "expires_ts": int(time.time()) + 200000,
        "redirect_uri": ns["REDIRECT_URI"],
    }
    a.update(over)
    with open(tmp + "/auth.json", "w") as f:
        json.dump(a, f)
    ns["_AUTH"].clear()
    return a


# ---- tests: login / token ---------------------------------------------------


def test_start_login_qr_and_page(tmp):
    ns, fake = load(tmp)
    s = ns["_start_login"]()
    # authorize first (to learn the sid=oauth2.0 entry), then loginUrl
    assert fake.paths()[0].startswith("/oauth2/authorize")
    assert fake.paths()[1].startswith("/longPolling/loginUrl")
    assert "sid=oauth2.0" in fake.paths()[1] and "client_id=" + CLIENT_ID in fake.paths()[1]
    assert s["qr"] == QR_URL and s["lp"] == LP_URL and s["ttl"] == 300
    # device_id is persisted (consent is bound to it) and drives state=sha1('d='+id)
    a = _read(tmp, "auth.json")
    assert a["device_id"] == s["device_id"] == ns["DEFAULT_DEVICE_ID"]
    assert ns["_sha1hex"]("d=" + a["device_id"]) in fake.paths()[0]
    # the page carries the QR plus the browser-side sign-in link as a fallback
    st, _r, ct, html = ns["_h_page"](None, "GET", "/mijia-ha/", b"")
    assert st == 200 and ct.startswith("text/html")
    assert QR_URL in html and ns["DEFAULT_DEVICE_ID"] in html and "homeassistant.local" in html
    # ...but the copy the user reads is plain English with no protocol in it
    assert not any(ord(c) > 0x2E7F for c in html)  # no CJK anywhere
    copy = _text(html)
    assert "Mijia Sign In" in copy and "Mi Home" in copy
    for jargon in ("oauth", "Bearer", "token", "cookie", "device_id", "homeassistant", "sid="):
        assert jargon not in copy, jargon
    assert json.loads(ns["_h_refresh"](None, "GET", "/mijia-ha/refresh", b"")[3])["qr"] == QR_URL
    assert json.loads(ns["_h_status"](None, "GET", "/mijia-ha/status", b"")[3]) == {
        "status": "waiting"
    }
    fake.lp = "expired"  # an expired QR is answered with a fresh one
    assert (
        json.loads(ns["_h_status"](None, "GET", "/mijia-ha/status", b"")[3])["status"] == "refresh"
    )
    assert "http://192.168.2.216/mijia-ha/" in ns["run"]({"action": "login"})


def test_scan_completes_chain_and_saves_token(tmp):
    ns, fake = load(tmp)
    ns["_start_login"]()
    assert ns["_poll"]()["status"] == "waiting"  # lp socket timeout == still waiting
    fake.lp = "scanned"
    r = ns["_poll"]()
    assert r["status"] == "ok", r
    det = r["detail"]
    assert det["code"].startswith("AKSRV_testcode")
    assert det["probe"]["homes"][0]["name"] == "Home"
    a = _read(tmp, "auth.json")
    assert det["access_token"] == a["access_token"][:16] + "..."  # no full secret on the page
    assert a["access_token"].startswith("AT_") and a["refresh_token"].startswith("RT_")
    assert a["uid"] == "2164169862"  # the lp userId, tagging the device cache
    assert a["expires_ts"] > a["obtained_ts"] and a["redirect_uri"] == ns["REDIRECT_URI"]
    # the code was read out of the Location, never by requesting that host
    assert "homeassistant.local" not in fake.hosts()
    order = [p.split("?")[0] for p in fake.paths()]
    assert order[-4:] == ["/lp/s", "/sts/oauth", "/oauth2/authorize", "/app/v2/ha/oauth/get_token"]
    # one Bearer call, no signing anywhere
    host, path, headers, body = fake.posts[0]
    assert path == "/app/v2/homeroom/gethome" and headers["X-Client-BizId"] == "haapi"
    assert headers["Authorization"] == "Bearer" + a["access_token"]
    assert headers["X-Client-AppId"] == CLIENT_ID and json.loads(body)["limit"] == 150


def test_consent_page_is_reported_not_swallowed(tmp):
    ns, fake = load(tmp, chain="consent")
    ns["_start_login"]()
    fake.lp = "scanned"
    r = ns["_poll"]()
    assert r["status"] == "consent", r
    det = r["detail"]
    assert det["step"] == "code" and det["page_bytes"] > 200
    assert det["cookies"] and any("/oauth2/authorize" in h for h in det["hops"])
    assert (_read(tmp, "auth.json") or {}).get("access_token") is None  # nothing half-saved


def test_manual_code_paste_and_token_error(tmp):
    ns, fake = load(tmp)
    r = json.loads(ns["_h_code"](None, "POST", "/mijia-ha/code", "AKSRV_manual")[3])
    assert r["status"] == "ok"
    # the exchange is a code exchange (not a refresh) carrying exactly that code
    assert "AKSRV_manual" in fake.token_paths()[0] and "refresh_token" not in fake.token_paths()[0]
    # a pasted callback URL yields the same call, with +/= round-tripped intact
    body = "http://homeassistant.local:8123/api/webhook/edge-agent?code=AKSRV_a%2Bb%3D&state=s"
    r2 = json.loads(ns["_h_code"](None, "POST", "/mijia-ha/code", body)[3])
    assert r2["status"] == "ok" and "AKSRV_a%2Bb%3D" in fake.token_paths()[1]
    assert r2["detail"]["code"].startswith("AKSRV_a+b=")  # decoded once, re-encoded once
    assert ns["_auth_load"]()["device_id"] in fake.token_paths()[1]
    # a rejected code surfaces Xiaomi's own error text, and saves nothing
    kept = _read(tmp, "auth.json")["access_token"]
    fake.token_reply = TOKEN_ERR
    r3 = json.loads(ns["_h_code"](None, "POST", "/mijia-ha/code", "AKSRV_bad")[3])
    assert r3["status"] == "error" and "96013 invalid authorization code" in r3["msg"]
    assert _read(tmp, "auth.json")["access_token"] == kept  # a failure overwrites nothing


def test_refresh_rotates_and_persists(tmp):
    ns, fake = load(tmp)
    _seed_token(
        tmp, ns, access_token="OLD", refresh_token="OLDRT", expires_ts=int(time.time()) - 5
    )
    ns["_refresh"]()
    a = _read(tmp, "auth.json")
    assert a["access_token"].startswith("AT_") and a["refresh_token"].startswith("RT_")
    q = fake.token_paths()[0]
    assert "OLDRT" in q and "code" not in q  # a refresh, not a code exchange
    # action=token then reports the fresh pair and re-probes with it
    out = ns["run"]({"action": "token"})
    assert "gethome: [{" in out and "Home" in out and "AT_" in out


def test_token_action_refreshes_near_expiry_and_degrades(tmp):
    ns, fake = load(tmp, token_reply=TOKEN_ERR)
    # inside the 1h refresh window, with a refresh_token that was already rotated
    _seed_token(
        tmp, ns, access_token="STILL_GOOD", refresh_token="DEAD", expires_ts=int(time.time()) + 60
    )
    out = ns["run"]({"action": "token"})
    assert "refresh failed" in out and "96013" in out
    assert "gethome: [{" in out  # the old access_token still works
    assert ns["run"]({"action": "nope"}).startswith("unknown action")
    assert ns["run"]({}).startswith("Open the link below")  # no action == login


# ---- tests: control ---------------------------------------------------------


def test_list_devices_rooms_cache_and_pagination(tmp):
    ns, fake = load(tmp)
    _seed_token(tmp, ns)
    out = ns["run"]({"action": "list_devices"})
    assert fake.post_paths() == ["/app/v2/homeroom/gethome", "/app/v2/home/device_list_page"]
    assert "卧室空调 | did=d1 | model=lumi.acpartner.mcn02 | online=True | room=卧室" in out
    assert "台灯 | did=d2 | model=yeelink.light.lamp2 | online=True | room=客厅" in out
    assert "手环 | did=d0 | model=miwear.watch.n67cn | online=False\n" in out + "\n"  # no room
    c = _read(tmp, "devices.json")
    assert c["uid"] == "2164169862" and len(c["devices"]) == 3
    del fake.posts[:]
    assert ns["run"]({"action": "list_devices"}) == out and fake.posts == []  # cache hit
    ns["run"]({"action": "list_devices", "refresh": "true"})
    assert len(fake.post_paths()) == 2  # forced re-query
    # a second account must not inherit this cache
    _seed_token(tmp, ns, uid="999")
    del fake.posts[:]
    ns["run"]({"action": "list_devices"})
    assert len(fake.post_paths()) == 2
    # pagination: has_more + next_start_did is followed once more
    _seed_token(tmp, ns)
    fake.page2 = [{"did": "d9", "name": "共享灯", "model": "yeelink.light.x", "isOnline": True}]
    out2 = ns["run"]({"action": "list_devices", "refresh": True})
    assert "did=d9" in out2 and len(_read(tmp, "devices.json")["devices"]) == 4
    assert fake.bodies("/app/v2/home/device_list_page")[-1].get("start_did") == "next1"


def test_prop_get_and_set_batched_and_annotated(tmp):
    ns, fake = load(tmp)
    _seed_token(tmp, ns)
    assert (
        ns["run"]({"action": "get_prop", "did": "d1", "siid": 2, "piid": 1}) == "code=0 value=True"
    )
    req = fake.bodies("/app/v2/miotspec/prop/get")[-1]
    assert req["datasource"] == 1 and req["params"] == [{"did": "d1", "siid": 2, "piid": 1}]
    # four properties of one device go out as ONE request, one line each
    fake.get_result = [
        {"code": 0, "value": False},
        {"code": 0, "value": 1},
        {"code": -704040003},
        {"code": -704042011},
    ]
    out = ns["run"](
        {
            "action": "get_prop",
            "did": "d1",
            "props": [
                {"siid": 2, "piid": 1},
                {"siid": 2, "piid": 2},
                {"siid": 2, "piid": 3},
                {"siid": 9, "piid": 1},
            ],
        }
    )
    assert out.split("\n") == [
        "siid=2 piid=1 code=0 value=False",
        "siid=2 piid=2 code=0 value=1",
        "siid=2 piid=3 code=-704040003 (property does not exist)",
        "siid=9 piid=1 code=-704042011 (device offline)",
    ]
    assert len(fake.bodies("/app/v2/miotspec/prop/get")) == 2
    # writes coerce strings and accept code 1 as success
    fake.set_result = [{"code": 1}, {"code": -704220043}]
    out = ns["run"](
        {
            "action": "set_prop",
            "did": "d1",
            "props": [
                {"siid": 2, "piid": 1, "value": "true"},
                {"siid": 2, "piid": 3, "value": "35"},
            ],
        }
    )
    assert out.split("\n") == [
        "siid=2 piid=1 code=1 ok",
        "siid=2 piid=3 code=-704220043 (property value out of range)",
    ]
    sent = fake.bodies("/app/v2/miotspec/prop/set")[-1]["params"]
    assert sent[0]["value"] is True and sent[1]["value"] == 35
    # missing arguments are answered without a cloud call
    del fake.posts[:]
    assert ns["run"]({"action": "set_prop", "did": "d1", "siid": 2, "piid": 1}) == "missing value"
    assert ns["run"]({"action": "get_prop", "siid": 2, "piid": 1}) == "missing did/siid/piid"
    assert fake.posts == []


def test_expired_token_refreshes_retries_then_falls_back_to_qr(tmp):
    ns, fake = load(tmp)
    _seed_token(tmp, ns)
    ns["run"]({"action": "list_devices"})  # warm the cache so the drop is visible
    assert _read(tmp, "devices.json") is not None
    # one 401 -> refresh -> the retried call succeeds, transparently
    fake.fail_once = True
    out = ns["run"]({"action": "get_prop", "did": "d1", "siid": 2, "piid": 1})
    assert out == "code=0 value=True"
    assert fake.token_paths() and "RT_seed" in fake.token_paths()[0]
    assert _read(tmp, "auth.json")["access_token"].startswith("AT_")
    # refresh also dead -> the cache is dropped and the QR page comes back
    fake.fail_once = True
    fake.token_reply = TOKEN_ERR
    out = ns["run"]({"action": "get_prop", "did": "d1", "siid": 2, "piid": 1})
    assert out.startswith("Login expired (unauthorized (HTTP 401)")
    assert "http://192.168.2.216/mijia-ha/" in out
    assert _read(tmp, "devices.json") is None
    # a plain cloud error is reported as itself (no refresh loop), cache dropped
    _seed_token(tmp, ns)
    ns["run"]({"action": "list_devices"})
    assert _read(tmp, "devices.json") is not None
    fake.api_code = -704042001
    fake.api_msg = "device not found"
    out = ns["run"]({"action": "get_prop", "did": "gone", "siid": 2, "piid": 1})
    assert out.startswith("mijia error: code=-704042001 device not found"), out
    assert "token refresh failed" not in out  # not mistaken for an auth failure
    assert _read(tmp, "devices.json") is None


def test_device_spec_cached_filtered_and_persisted(tmp):
    ns, fake = load(tmp)
    out = ns["run"]({"action": "device_spec", "model": "lumi.acpartner.mcn02"})
    assert fake.hosts()[-1] == "home.miot-spec.com"
    assert out.split("\n") == [
        "2.1 name=on fmt=bool rw=rw",
        "2.3 name=target-temperature (设定温度) fmt=float rw=rw range=[16, 30, 1]",
    ]
    assert _read(tmp, "spec.json")["lumi.acpartner.mcn02"] == out  # survives a reboot
    del fake.gets[:]
    assert ns["run"]({"action": "device_spec", "model": "lumi.acpartner.mcn02"}) == out
    assert fake.gets == []  # RAM cache hit
    # a filter narrows a big spec to the lines the LLM asked about
    only = ns["run"]({"action": "device_spec", "model": "lumi.acpartner.mcn02", "filter": "温度"})
    assert only == "2.3 name=target-temperature (设定温度) fmt=float rw=rw range=[16, 30, 1]"
    assert ns["run"]({"action": "device_spec", "model": "m", "filter": "nope"}).startswith(
        "no property matches filter"
    )
    assert ns["run"]({"action": "device_spec"}) == "missing model"
    fake.spec_page = "<html>nothing</html>"
    assert (
        ns["run"]({"action": "device_spec", "model": "other.model"})
        == "spec not found for other.model"
    )


_TESTS = [
    test_start_login_qr_and_page,
    test_scan_completes_chain_and_saves_token,
    test_consent_page_is_reported_not_swallowed,
    test_manual_code_paste_and_token_error,
    test_refresh_rotates_and_persists,
    test_token_action_refreshes_near_expiry_and_degrades,
    test_list_devices_rooms_cache_and_pagination,
    test_prop_get_and_set_batched_and_annotated,
    test_expired_token_refreshes_retries_then_falls_back_to_qr,
    test_device_spec_cached_filtered_and_persisted,
]


def main():
    failed = 0
    for t in _TESTS:
        tmp = _tmpdir()
        try:
            t(tmp)
            print("PASS %s" % t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL %s: %s" % (t.__name__, e))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("ERROR %s: %s: %s" % (t.__name__, type(e).__name__, e))
        finally:
            _rmtree(tmp)
    print("%d/%d tests passed" % (len(_TESTS) - failed, len(_TESTS)))
    if failed:
        sys.exit(1)


main()
