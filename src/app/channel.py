"""Channel abstraction: unified async interface for AI Agent interactions.

Architecture:
    Channel (modality ↔ text)  ←→  Bus (routing + sessions)  ←→  Agent (LLM)

- Channels own modality conversion (audio↔text, IM format↔text).
- Bus aggregates inbound messages, dispatches replies, isolates sessions.
- The Bus boundary is always plain text.
- Agent is pure LLM logic, stateless per-call.
"""


class Message:
    """Inbound text message flowing through the bus."""

    def __init__(self, channel, content="", sender_id="", chat_id="", metadata=None):
        self.channel = channel
        self.content = content
        self.sender_id = sender_id
        self.chat_id = chat_id or "default"
        self.metadata = metadata or {}

    @property
    def session_key(self):
        """Unique key for session/history isolation."""
        return "{}:{}".format(self.channel, self.chat_id)


class Reply:
    """Outbound text reply flowing through the bus."""

    def __init__(self, content="", error=None, metadata=None):
        self.content = content
        self.error = error
        self.metadata = metadata or {}

    @property
    def ok(self):
        return self.error is None


class BaseChannel:
    """Modality adapter: converts native format ↔ text Messages.

    Channels do NOT hold an agent reference. They communicate exclusively
    through the bus, which handles routing and session isolation.
    """

    name = "base"

    # Default enable state when the config has no explicit entry for the
    # channel (opt-in channels like weixin override this to False).
    enabled_default = True

    def __init__(self, bus):
        self._bus = bus
        self._running = False

    async def start(self):
        """Start the channel."""
        self._running = True

    async def stop(self):
        """Stop the channel and release resources."""
        self._running = False

    @property
    def is_running(self):
        return self._running

    async def dispatch(self, message):
        """Send a text message through the bus. Returns a text Reply."""
        return await self._bus.dispatch(message)

    def dispatch_sync(self, message):
        """Send a text message through the bus synchronously.

        For callers on non-loop threads (e.g. the voice channel), which
        must not touch the shared asyncio event loop.
        """
        return self._bus.dispatch_sync(message)
