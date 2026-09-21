"""Web channel: text → Bus → text."""

import json
from app.channel import BaseChannel, Message
from app.util import json_response as _json


class WebChannel(BaseChannel):
    """Text-based conversation channel. No modality conversion needed."""

    name = "web"

    def routes(self):
        """HTTP transport adapter."""
        return [("POST", "/agent", self._http_chat)]

    async def _http_chat(self, server, method, path, body):
        try:
            data = json.loads(body or "{}")
        except ValueError:
            return _json(400, "Bad Request", {"error": "invalid JSON"})
        msg_text = data.get("message")
        if not msg_text:
            return _json(400, "Bad Request", {"error": "message required"})
        message = Message(
            channel=self.name,
            content=msg_text,
            sender_id=data.get("sender_id", ""),
            chat_id=data.get("chat_id", ""),
        )
        reply = await self.dispatch(message)
        if not reply.ok:
            return _json(502, "Bad Gateway", {"error": reply.error})
        return _json(200, "OK", {"reply": reply.content})
