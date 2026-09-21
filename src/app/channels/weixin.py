"""WeChat (weixin) channel: personal WeChat text chat over the iLink API.

Minimal text-only implementation of the protocol used by the
openclaw-weixin / nanobot reference plugin (ilinkai.weixin.qq.com):

- Login is a QR-code flow driven from the WebUI (/weixin/qr/*) which
  yields a long-lived bot token.
- A long-poll loop (ilink/bot/getupdates) receives user messages.
- Replies go back via ilink/bot/sendmessage carrying the context_token
  from the inbound message (the server requires it for replies).

Credentials live in a separate state file (config.WEIXIN_STATE_PATH);
the enable flag lives in the shared channels config map (channels.weixin).
Group chats and non-text items are ignored on purpose (text DMs only).
"""

import os
import json
import time
import binascii
import asyncio
import app.config as config
import app.log as log
from app.channel import BaseChannel, Message
from app.util import json_response as _json, ensure_parent_dir
from app.httpclient import https_request_async, https_post_json_async

# Protocol constants (aligned with openclaw-weixin v2.4.6)
ITEM_TEXT = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
CHANNEL_VERSION = "2.4.6"
CLIENT_VERSION = (2 << 16) | (4 << 8) | 6
BASE_INFO = {
    "channel_version": CHANNEL_VERSION,
    "bot_agent": "edge-agent/1.0 (micropython)",
}
ERR_CONTEXT_RESTRICTED = -2
ERR_INVALID_ARGUMENT = -3
ERR_STALE_TOKEN = -14

_SEEN_MAX = 128
_client_seq = 0


class WeixinAPIError(Exception):
    """iLink API or transport failure."""

    def __init__(self, endpoint, message, errcode=0, retryable=False):
        self.endpoint = endpoint
        self.errcode = errcode
        self.retryable = retryable
        super().__init__("WeChat {} failed: {}".format(endpoint, message))


class WeixinAuthError(WeixinAPIError):
    """The bot token is stale; a new QR scan is required."""


def _as_int(v):
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    try:
        return int(str(v or "0"))
    except ValueError:
        return 0


def _json_body(body):
    """Parse a JSON request body. Returns (data, error_response)."""
    try:
        return json.loads(body or "{}"), None
    except ValueError:
        return None, _json(400, "Bad Request", {"error": "invalid JSON"})


def _uin():
    """X-WECHAT-UIN: base64(decimal(random uint32)), fresh per request."""
    n = 0
    for b in os.urandom(4):
        n = n * 256 + b
    return binascii.b2a_base64(str(n).encode()).strip().decode()


def split_text(content, max_len=None):
    """Split long replies into WeChat-sized chunks at natural boundaries."""
    if max_len is None:
        max_len = config.WEIXIN_MAX_TEXT_LEN
    content = (content or "").strip()
    if not content:
        return []
    if max_len <= 0 or len(content) <= max_len:
        return [content]
    chunks = []
    rest = content
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        piece = rest[:max_len]
        cut = piece.rfind("\n\n")
        if cut <= 0:
            cut = piece.rfind("\n")
        if cut <= 0:
            cut = max_len
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [c for c in chunks if c]


class WeixinChannel(BaseChannel):
    """Personal WeChat text channel via the iLink HTTP long-poll API."""

    name = "weixin"
    enabled_default = False

    def __init__(self, bus, state_path=None):
        super().__init__(bus)
        self._state_path = state_path or config.WEIXIN_STATE_PATH
        self._token = ""
        self._base_url = config.WEIXIN_BASE_URL
        self._buf = ""
        self._bot_id = ""
        self._user_id = ""
        self._ctx_tokens = {}  # user_id -> context_token
        self._ctx_at = {}  # user_id -> ticks_ms when token was cached
        self._seen = {}
        self._seen_order = []
        self._poll_task = None
        self._poll_timeout = config.WEIXIN_POLL_TIMEOUT_SEC
        self._auth_expired = False
        self._qr = None  # active QR login session
        self._load_state()

    # -- Lifecycle -------------------------------------------------------

    async def start(self):
        self._running = True
        self._start_polling()

    async def stop(self):
        self._running = False
        await self._stop_polling()

    def set_enabled(self, enabled):
        """Runtime toggle from the settings UI (like the voice channel)."""
        if enabled:
            self._running = True
            self._start_polling()
        else:
            self._running = False
            task = self._poll_task
            self._poll_task = None
            if task:
                task.cancel()

    def _start_polling(self):
        if self._running and self._token and not self._auth_expired and self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())
            log.info("Weixin", "long-poll loop started")

    async def _stop_polling(self):
        task = self._poll_task
        self._poll_task = None
        if task:
            task.cancel()
            try:
                await task
            except Exception:
                pass

    @property
    def connected(self):
        return bool(self._token) and not self._auth_expired

    def status(self):
        """WebUI-facing state. Never includes the bot token."""
        return {
            "connected": self.connected,
            "auth_expired": self._auth_expired,
            "polling": self._poll_task is not None,
            "bot_id": self._bot_id,
            "user_id": self._user_id,
        }

    # -- State persistence -----------------------------------------------

    def _load_state(self):
        try:
            with open(self._state_path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self._token = str(data.get("token") or "")
        self._base_url = str(data.get("base_url") or "") or config.WEIXIN_BASE_URL
        self._buf = str(data.get("buf") or "")
        self._bot_id = str(data.get("bot_id") or "")
        self._user_id = str(data.get("user_id") or "")

    def _save_state(self):
        try:
            ensure_parent_dir(self._state_path)
            with open(self._state_path, "w") as f:
                json.dump(
                    {
                        "token": self._token,
                        "base_url": self._base_url,
                        "buf": self._buf,
                        "bot_id": self._bot_id,
                        "user_id": self._user_id,
                    },
                    f,
                )
        except OSError as e:
            log.error("Weixin", "save state failed: {}".format(e))

    def _commit_account(self, token, base_url, bot_id, user_id):
        self._token = token
        self._base_url = base_url or self._base_url
        self._bot_id = bot_id
        self._user_id = user_id
        self._buf = ""
        self._auth_expired = False
        self._save_state()
        self._start_polling()
        log.info("Weixin", "logged in, bot_id={} user_id={}".format(bot_id, user_id))

    def logout(self):
        """Drop credentials and stop polling (called by the WebUI)."""
        self._token = ""
        self._buf = ""
        self._bot_id = ""
        self._user_id = ""
        self._auth_expired = False
        self._ctx_tokens = {}
        self._ctx_at = {}
        task = self._poll_task
        self._poll_task = None
        if task:
            task.cancel()
        try:
            os.remove(self._state_path)
        except OSError:
            pass
        log.info("Weixin", "logged out")

    # -- HTTP helpers ------------------------------------------------------

    def _headers(self, auth=True):
        h = {
            "X-WECHAT-UIN": _uin(),
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(CLIENT_VERSION),
        }
        if auth and self._token:
            h["Authorization"] = "Bearer " + self._token
        return h

    @staticmethod
    def _parse_base(base_url):
        """'https://host[:port]' -> (host, port)."""
        rest = base_url.split("://", 1)[-1].split("/", 1)[0]
        if ":" in rest:
            host, port = rest.split(":", 1)
            return host, int(port)
        return rest, 443

    @staticmethod
    def _check_error(endpoint, data):
        ret = _as_int(data.get("ret"))
        errcode = _as_int(data.get("errcode"))
        if ret == 0 and errcode == 0:
            return
        errmsg = str(data.get("errmsg") or "")
        code = errcode or ret
        if ERR_STALE_TOKEN in (ret, errcode):
            raise WeixinAuthError(endpoint, errmsg or "bot token stale", errcode=code)
        raise WeixinAPIError(
            endpoint, errmsg or "ret={} errcode={}".format(ret, errcode), errcode=code
        )

    async def _api_post(self, endpoint, body, auth=True, base_url=None, timeout=15):
        host, port = self._parse_base(base_url or self._base_url)
        payload = json.dumps(body).encode()
        code, _hdr, resp = await https_post_json_async(
            host, port, "/" + endpoint, self._headers(auth), payload, timeout=timeout
        )
        return self._parse_response(endpoint, code, resp)

    async def _api_get(self, endpoint, auth=False, base_url=None, timeout=15):
        host, port = self._parse_base(base_url or self._base_url)
        code, _hdr, resp = await https_request_async(
            host, port, "GET", "/" + endpoint, self._headers(auth), None, timeout=timeout
        )
        return self._parse_response(endpoint, code, resp)

    def _parse_response(self, endpoint, code, resp):
        if code != 200:
            raise WeixinAPIError(
                endpoint, "HTTP {}".format(code), retryable=code >= 500 or code == 429
            )
        try:
            data = json.loads(resp)
        except ValueError:
            raise WeixinAPIError(endpoint, "invalid JSON", retryable=True)
        if not isinstance(data, dict):
            raise WeixinAPIError(endpoint, "unexpected JSON payload", retryable=True)
        self._check_error(endpoint, data)
        return data

    # -- QR login (driven by the WebUI) ------------------------------------

    async def qr_start(self, force=False):
        """Fetch a fresh login QR code. Returns a payload for the WebUI."""
        if self.connected and not force:
            return {"status": "connected"}
        tokens = [] if force or not self._token else [self._token]
        try:
            data = await self._api_post(
                "ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": tokens}, auth=False
            )
        except WeixinAPIError as e:
            if tokens and e.errcode == ERR_INVALID_ARGUMENT:
                data = await self._api_post(
                    "ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []}, auth=False
                )
            else:
                raise
        qrcode_id = str(data.get("qrcode") or "")
        if not qrcode_id:
            raise WeixinAPIError("get_bot_qrcode", "no qrcode in response")
        self._qr = {"id": qrcode_id, "base": self._base_url, "at": time.ticks_ms()}
        return {
            "status": "pending",
            "qrcode_id": qrcode_id,
            "qr_content": str(data.get("qrcode_img_content") or qrcode_id),
        }

    async def qr_poll(self, qrcode_id, verify_code=""):
        """Poll one step of the QR login. Returns a payload for the WebUI."""
        sess = self._qr
        if not sess or sess["id"] != qrcode_id:
            return {"status": "expired", "message": "no active QR session"}
        if time.ticks_diff(time.ticks_ms(), sess["at"]) > config.WEIXIN_QR_SESSION_MS:
            self._qr = None
            return {"status": "expired", "message": "QR session timed out"}
        path = "ilink/bot/get_qrcode_status?qrcode=" + qrcode_id
        if verify_code:
            path += "&verify_code=" + verify_code
        data = await self._api_get(path, base_url=sess["base"], timeout=65)
        status = str(data.get("status") or "")
        if status == "confirmed":
            token = str(data.get("bot_token") or "")
            if not token:
                self._qr = None
                return {"status": "expired", "message": "login confirmed but no token"}
            self._qr = None
            self._commit_account(
                token,
                str(data.get("baseurl") or "") or sess["base"],
                str(data.get("ilink_bot_id") or ""),
                str(data.get("ilink_user_id") or ""),
            )
            return {"status": "connected"}
        if status == "scaned_but_redirect":
            host = str(data.get("redirect_host") or "").strip()
            if host:
                sess["base"] = host if host.startswith("http") else "https://" + host
            return {"status": "scanned"}
        if status == "need_verifycode":
            return {"status": "need_verifycode"}
        if status == "expired":
            self._qr = None
            return {"status": "expired", "message": "QR code expired"}
        if status == "verify_code_blocked":
            self._qr = None
            return {"status": "expired", "message": "verification failed, start again"}
        if status == "binded_redirect":
            self._qr = None
            if self._token:
                return {"status": "connected"}
            return {"status": "expired", "message": "account already bound elsewhere"}
        return {"status": "waiting"}

    # -- Long-poll loop ------------------------------------------------------

    async def _poll_loop(self):
        try:
            failures = 0
            while self._running and self._token and not self._auth_expired:
                try:
                    data = await self._api_post(
                        "ilink/bot/getupdates",
                        {"get_updates_buf": self._buf, "base_info": BASE_INFO},
                        timeout=self._poll_timeout + 10,
                    )
                    failures = 0
                except WeixinAuthError:
                    self._auth_expired = True
                    log.warn("Weixin", "bot token stale, scan a new QR code to reconnect")
                    break
                except Exception:
                    failures += 1
                    delay = min(30, 2 ** min(failures, 4))
                    log.warn(
                        "Weixin", "getupdates failed ({}), retry in {}s".format(failures, delay)
                    )
                    await asyncio.sleep(delay)
                    continue
                ms = _as_int(data.get("longpolling_timeout_ms"))
                if ms > 0:
                    self._poll_timeout = max(5, ms // 1000)
                msgs = data.get("msgs") or []
                new_buf = data.get("get_updates_buf") or ""
                if new_buf:
                    self._buf = new_buf
                    if msgs:
                        self._save_state()
                for msg in msgs:
                    try:
                        await self._process_message(msg)
                    except Exception as e:
                        log.error("Weixin", "process message failed: {}".format(e))
        finally:
            self._poll_task = None

    # -- Inbound processing ---------------------------------------------------

    async def _process_message(self, msg):
        if _as_int(msg.get("message_type")) == MESSAGE_TYPE_BOT:
            return
        user = str(msg.get("from_user_id") or "")
        if not user or user.endswith("@chatroom"):
            return  # text-only MVP: direct messages only
        msg_id = str(msg.get("message_id") or msg.get("seq") or "")
        if not msg_id:
            msg_id = "{}_{}".format(user, msg.get("create_time_ms", ""))
        if msg_id in self._seen:
            return
        self._seen[msg_id] = True
        self._seen_order.append(msg_id)
        while len(self._seen_order) > _SEEN_MAX:
            self._seen.pop(self._seen_order.pop(0), None)
        ctx = str(msg.get("context_token") or "")
        if ctx:
            self._ctx_tokens[user] = ctx
            self._ctx_at[user] = time.ticks_ms()
        parts = []
        for item in msg.get("item_list") or []:
            if _as_int(item.get("type")) == ITEM_TEXT:
                t = str((item.get("text_item") or {}).get("text") or "")
                if t:
                    parts.append(t)
        text = "\n".join(parts).strip()
        if not text:
            return
        log.info("Weixin", "message from {}".format(user))
        reply = await self.dispatch(
            Message(channel=self.name, content=text, sender_id=user, chat_id=user)
        )
        if reply.ok:
            await self._send_reply(user, reply.content)
        else:
            log.warn("Weixin", "dispatch error: {}".format(reply.error))
            await self._send_reply(user, "⚠️ {}".format(reply.error))

    # -- Outbound ---------------------------------------------------------------

    async def _fresh_context_token(self, user):
        """Return the cached context_token, refreshing it via getconfig when old.

        iLink expires context tokens server-side after ~90-160s of silence,
        so a slow LLM turn could otherwise lose the reply.
        """
        ctx = self._ctx_tokens.get(user, "")
        if not ctx:
            return ""
        at = self._ctx_at.get(user)
        if (
            at is None
            or time.ticks_diff(time.ticks_ms(), at) < config.WEIXIN_CONTEXT_TOKEN_MAX_AGE_MS
        ):
            return ctx
        try:
            data = await self._api_post(
                "ilink/bot/getconfig",
                {"ilink_user_id": user, "context_token": ctx, "base_info": BASE_INFO},
            )
            new = str(data.get("context_token") or "")
            if new:
                self._ctx_tokens[user] = new
                self._ctx_at[user] = time.ticks_ms()
                return new
        except Exception as e:
            log.warn("Weixin", "context refresh failed: {}".format(e))
        return ctx

    async def _send_reply(self, user, text):
        chunks = split_text(text)
        if not chunks:
            return
        ctx = await self._fresh_context_token(user)
        for chunk in chunks:
            try:
                await self._send_text(user, chunk, ctx)
            except WeixinAPIError as e:
                if e.errcode == ERR_CONTEXT_RESTRICTED:
                    log.warn("Weixin", "send quota exhausted for {}".format(user))
                else:
                    log.warn("Weixin", "send failed: {}".format(e))
                return

    async def _send_text(self, user, text, ctx):
        global _client_seq
        _client_seq += 1
        msg = {
            "from_user_id": "",
            "to_user_id": user,
            "client_id": "edgeagent-{}-{}".format(time.ticks_ms(), _client_seq),
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if ctx:
            msg["context_token"] = ctx
        await self._api_post("ilink/bot/sendmessage", {"msg": msg, "base_info": BASE_INFO})

    # -- WebUI HTTP routes --------------------------------------------------------

    def routes(self):
        return [
            ("GET", "/weixin/status", self._http_status),
            ("POST", "/weixin/qr/start", self._http_qr_start),
            ("POST", "/weixin/qr/poll", self._http_qr_poll),
            ("POST", "/weixin/logout", self._http_logout),
        ]

    async def _http_status(self, server, method, path, body):
        return _json(200, "OK", self.status())

    async def _http_qr_start(self, server, method, path, body):
        data, err = _json_body(body)
        if err:
            return err
        try:
            out = await self.qr_start(force=bool(data.get("force")))
        except WeixinAPIError as e:
            return _json(502, "Bad Gateway", {"error": str(e)})
        if out["status"] == "connected":
            return _json(409, "Conflict", out)
        return _json(200, "OK", out)

    async def _http_qr_poll(self, server, method, path, body):
        data, err = _json_body(body)
        if err:
            return err
        qrcode_id = str(data.get("qrcode_id") or "")
        if not qrcode_id:
            return _json(400, "Bad Request", {"error": "qrcode_id required"})
        try:
            out = await self.qr_poll(qrcode_id, str(data.get("verify_code") or ""))
        except WeixinAPIError as e:
            return _json(502, "Bad Gateway", {"error": str(e)})
        return _json(200, "OK", out)

    async def _http_logout(self, server, method, path, body):
        self.logout()
        return _json(200, "OK", {"status": "ok"})
