"""Provider interfaces for LLM / ASR / TTS backends.

Each vendor implementation lives in its own module in this package and
implements the interface(s) for the services it offers. factory.py maps
config names to implementations; callers only see these interfaces.
"""


class ProviderError(Exception):
    """Provider failure: missing config, transport error, or protocol error."""


class TTSWSError(ProviderError):
    """TTS provider failure: missing config, transport, or protocol error."""


class LLMProvider:
    """Chat-completion backend (OpenAI-style messages/tool_calls)."""

    name = ""

    def chat(self, cfg, messages, tools=None, timeout=None):
        """POST a chat completion. Returns the parsed JSON response dict."""
        raise NotImplementedError

    async def chat_async(self, cfg, messages, tools=None, timeout=None):
        """Async POST a chat completion (event-loop thread)."""
        raise NotImplementedError


class ASRProvider:
    """Speech-to-text backend."""

    name = ""

    def transcribe(self, cfg, pcm_bytes, timeout=None):
        """Recognize PCM16-LE mono audio. Returns the recognized text."""
        raise NotImplementedError

    def stream(self, cfg, timeout=None):
        """Open a session fed while the user talks. Returns a stream handle.

        The handle exposes ``feed(pcm)``, ``finish() -> text`` and ``close()``.
        Backends without a duplex mode leave this raising NotImplementedError
        and callers fall back to :meth:`transcribe`.
        """
        raise NotImplementedError


class TTSProvider:
    """Text-to-speech backend producing PCM16-LE mono audio."""

    name = ""

    def sample_rate(self, cfg):
        """Output sample rate in Hz selected by cfg."""
        raise NotImplementedError

    def connect(self, cfg, timeout=None):
        """Open the transport without starting a task.

        Lets a caller pay the handshake while something else (an LLM
        round-trip) is still running. The connection must then be passed to
        :meth:`synthesize` or closed by the caller.
        """
        raise NotImplementedError

    def synthesize(self, cfg, text, sink=None, timeout=None, ws=None):
        """Synthesize text to PCM.

        Returns PCM bytes, or when ``sink`` is given, passes chunks to
        ``sink(chunk)`` as they arrive and returns the total byte count
        (memory stays bounded regardless of reply length). ``ws`` is an
        optional connection already opened by :meth:`connect`.
        """
        raise NotImplementedError
