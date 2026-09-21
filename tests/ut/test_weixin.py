"""WeChat channel tests: protocol, QR login, polling, sending (mocked HTTP)."""

import compat  # noqa: F401

import json
import time
import asyncio
import unittest

import helpers
import app.config as config
from app.channel import Reply
from app.channels import weixin


class FakeBus:
    def __init__(self, reply_text="ok"):
        self.reply_text = reply_text
        self.messages = []

    async def dispatch(self, message):
        self.messages.append(message)
        return Reply(content=self.reply_text)


class TestWeixin(unittest.TestCase):
    def setUp(self):
        self.reqs = []
        self.routes = {}
        self.hold = 0.0
        self.tmp_files = []
        self._max_len = config.WEIXIN_MAX_TEXT_LEN
        self._orig_post = weixin.https_post_json_async
        self._orig_req = weixin.https_request_async

        test = self

        async def fake_post(host, port, path, headers, body_bytes, **kw):
            body = json.loads(body_bytes.decode())
            test.reqs.append(("POST", path, headers, body))
            if "getupdates" in path and test.hold:
                await asyncio.sleep(test.hold)
            return test._respond(path)

        async def fake_req(host, port, method, path, headers, body_bytes, **kw):
            test.reqs.append((method, path, headers, None))
            return test._respond(path)

        weixin.https_post_json_async = fake_post
        weixin.https_request_async = fake_req

    def tearDown(self):
        weixin.https_post_json_async = self._orig_post
        weixin.https_request_async = self._orig_req
        config.WEIXIN_MAX_TEXT_LEN = self._max_len
        for p in self.tmp_files:
            try:
                import os

                os.remove(p)
            except OSError:
                pass

    def _respond(self, path):
        for key, resp in self.routes.items():
            if key in path:
                data = resp() if callable(resp) else resp
                return (200, "", json.dumps(data).encode())
        return (200, "", b"{}")

    def _path_of(self, fragment):
        return [r for r in self.reqs if fragment in r[1]]

    def make_channel(self, bus=None):
        path = helpers._unique_name("weixin") + ".json"
        self.tmp_files.append(path)
        return weixin.WeixinChannel(bus or FakeBus(), state_path=path)

    def make_msg(self, msg_id="m1", user="u1", text="hello", ctx="ctx-1", mtype=1):
        return {
            "message_id": msg_id,
            "message_type": mtype,
            "from_user_id": user,
            "context_token": ctx,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }

    # -- pure helpers -------------------------------------------------------

    def test_split_text_and_constants(self):
        self.assertEqual(weixin.split_text(""), [])
        self.assertEqual(weixin.split_text("short"), ["short"])
        long = ("para one. " * 30 + "\n\n" + "para two. " * 30).strip()
        chunks = weixin.split_text(long, max_len=100)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 100)
        # Protocol constants match the reference plugin (v2.4.6).
        self.assertEqual(weixin.CLIENT_VERSION, (2 << 16) | (4 << 8) | 6)
        self.assertEqual(weixin.BASE_INFO["channel_version"], "2.4.6")

    def test_headers(self):
        ch = self.make_channel()
        h = ch._headers()
        for key in (
            "X-WECHAT-UIN",
            "AuthorizationType",
            "iLink-App-Id",
            "iLink-App-ClientVersion",
        ):
            self.assertIn(key, h)
        self.assertNotIn("Authorization", h)  # no token yet
        ch._token = "tok"
        h = ch._headers()
        self.assertEqual(h["Authorization"], "Bearer tok")
        self.assertNotIn("Authorization", ch._headers(auth=False))

    # -- state ----------------------------------------------------------------

    def test_state_roundtrip(self):
        ch = self.make_channel()
        ch._commit_account("tok123", "https://other.example.com", "bot1", "user1")
        ch2 = weixin.WeixinChannel(FakeBus(), state_path=ch._state_path)
        self.assertEqual(ch2._token, "tok123")
        self.assertEqual(ch2._base_url, "https://other.example.com")
        self.assertEqual(ch2._user_id, "user1")
        self.assertTrue(ch2.connected)

    # -- inbound ----------------------------------------------------------------

    def test_inbound_dispatch_and_reply(self):
        bus = FakeBus(reply_text="hi there")
        ch = self.make_channel(bus)
        asyncio.run(ch._process_message(self.make_msg()))
        self.assertEqual(len(bus.messages), 1)
        m = bus.messages[0]
        self.assertEqual(m.channel, "weixin")
        self.assertEqual(m.content, "hello")
        self.assertEqual(m.chat_id, "u1")
        self.assertEqual(ch._ctx_tokens.get("u1"), "ctx-1")
        sent = self._path_of("sendmessage")
        self.assertEqual(len(sent), 1)
        msg = sent[0][3]["msg"]
        self.assertEqual(msg["to_user_id"], "u1")
        self.assertEqual(msg["message_type"], 2)
        self.assertEqual(msg["message_state"], 2)
        self.assertEqual(msg["context_token"], "ctx-1")
        self.assertEqual(msg["item_list"][0]["text_item"]["text"], "hi there")
        self.assertIn("base_info", sent[0][3])

    def test_inbound_filtering(self):
        bus = FakeBus()
        ch = self.make_channel(bus)
        msg = self.make_msg()
        asyncio.run(ch._process_message(msg))
        asyncio.run(ch._process_message(msg))  # duplicate message_id
        asyncio.run(ch._process_message(self.make_msg(msg_id="m2", mtype=2)))  # own msg
        asyncio.run(ch._process_message(self.make_msg(msg_id="m3", user="room@chatroom")))
        asyncio.run(ch._process_message(self.make_msg(msg_id="m4", text="")))
        self.assertEqual(len(bus.messages), 1)

    # -- QR login ------------------------------------------------------------------

    def test_qr_login_flow(self):
        ch = self.make_channel()
        self.routes["get_bot_qrcode"] = {"qrcode": "q1", "qrcode_img_content": "https://wx/qr/abc"}
        out = asyncio.run(ch.qr_start())
        self.assertEqual(out["status"], "pending")
        self.assertEqual(out["qrcode_id"], "q1")
        self.assertEqual(out["qr_content"], "https://wx/qr/abc")
        # Already-connected channels refuse a plain restart.
        ch._token = "tok"
        self.assertEqual(asyncio.run(ch.qr_start())["status"], "connected")
        ch._token = ""
        # Polling: wait -> confirmed stores credentials + base redirect.
        self.routes["get_qrcode_status"] = {"status": "wait"}
        self.assertEqual(asyncio.run(ch.qr_poll("q1"))["status"], "waiting")
        self.routes["get_qrcode_status"] = {
            "status": "confirmed",
            "bot_token": "tok-new",
            "baseurl": "https://ilink2.weixin.qq.com",
            "ilink_bot_id": "b1",
            "ilink_user_id": "u9",
        }
        self.assertEqual(asyncio.run(ch.qr_poll("q1"))["status"], "connected")
        self.assertTrue(ch.connected)
        self.assertEqual(ch._token, "tok-new")
        self.assertEqual(ch._base_url, "https://ilink2.weixin.qq.com")
        # Unknown session ids are rejected.
        self.assertEqual(asyncio.run(ch.qr_poll("nope"))["status"], "expired")

    # -- poll loop --------------------------------------------------------------------

    def test_poll_loop_processes_then_auth_expiry(self):
        bus = FakeBus()
        ch = self.make_channel(bus)
        ch._token = "tok"
        ch._running = True
        state = {"calls": 0}

        def updates():
            state["calls"] += 1
            if state["calls"] == 1:
                return {
                    "msgs": [self.make_msg()],
                    "get_updates_buf": "buf2",
                    "longpolling_timeout_ms": 8000,
                }
            return {"ret": -14, "errmsg": "stale token"}

        self.routes["getupdates"] = updates

        async def run():
            task = asyncio.create_task(ch._poll_loop())
            await asyncio.wait_for(task, 5)

        asyncio.run(run())
        self.assertTrue(ch._auth_expired)
        self.assertIsNone(ch._poll_task)
        self.assertEqual(ch._buf, "buf2")
        self.assertEqual(ch._poll_timeout, 8)
        self.assertEqual(len(bus.messages), 1)
        self.assertEqual(len(self._path_of("sendmessage")), 1)

    # -- outbound ------------------------------------------------------------------------

    def test_send_splits_long_reply_and_quota_stops(self):
        ch = self.make_channel()
        ch._token = "tok"
        ch._ctx_tokens["u1"] = "ctx-1"
        ch._ctx_at["u1"] = time.ticks_ms()
        config.WEIXIN_MAX_TEXT_LEN = 10
        asyncio.run(ch._send_reply("u1", "aaaa bbbb cccc dddd eeee"))
        sent = self._path_of("sendmessage")
        self.assertGreater(len(sent), 1)
        for r in sent:
            self.assertLessEqual(len(r[3]["msg"]["item_list"][0]["text_item"]["text"]), 10)
        # Quota error (-2) stops the remaining chunks without raising.
        self.reqs = []
        self.routes["sendmessage"] = {"ret": -2, "errmsg": "quota"}
        asyncio.run(ch._send_reply("u1", "ffff gggg hhhh iiii"))
        self.assertEqual(len(self._path_of("sendmessage")), 1)

    def test_context_token_refresh_when_stale(self):
        ch = self.make_channel()
        ch._token = "tok"
        ch._ctx_tokens["u1"] = "old"
        ch._ctx_at["u1"] = time.ticks_diff(
            time.ticks_ms(), config.WEIXIN_CONTEXT_TOKEN_MAX_AGE_MS + 5000
        )
        self.routes["getconfig"] = {"context_token": "new"}

        async def run():
            return await ch._fresh_context_token("u1")

        self.assertEqual(asyncio.run(run()), "new")
        self.assertEqual(ch._ctx_tokens["u1"], "new")

    # -- WebUI routes -----------------------------------------------------------------------

    def test_http_routes_status_and_logout(self):
        ch = self.make_channel()
        ch._commit_account("secret-tok", "", "b1", "u9")

        async def run():
            code, _t, _ct, body = await ch._http_status(None, "GET", "/weixin/status", None)
            data = json.loads(body)
            self.assertEqual(code, 200)
            self.assertTrue(data["connected"])
            self.assertEqual(data["user_id"], "u9")
            self.assertNotIn("token", body)  # never leak credentials to the UI
            code, _t, _ct, body = await ch._http_qr_poll(None, "POST", "/weixin/qr/poll", "{}")
            self.assertEqual(code, 400)
            code, _t, _ct, body = await ch._http_logout(None, "POST", "/weixin/logout", None)
            self.assertEqual(code, 200)

        asyncio.run(run())
        self.assertFalse(ch.connected)
        self.assertEqual(ch._token, "")

    def test_set_enabled_toggles_poll_task(self):
        ch = self.make_channel()
        ch._token = "tok"
        self.hold = 0.3  # keep getupdates busy so the loop doesn't spin

        async def run():
            ch.set_enabled(True)
            self.assertIsNotNone(ch._poll_task)
            await asyncio.sleep(0.05)
            ch.set_enabled(False)
            self.assertIsNone(ch._poll_task)
            self.assertFalse(ch._running)
            await asyncio.sleep(0.4)  # let the cancelled task finish

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(globals())
