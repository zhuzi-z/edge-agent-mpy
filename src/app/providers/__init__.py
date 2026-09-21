"""Cloud provider integrations (LLM / ASR / TTS).

All vendor-specific protocols live in this package; the audio pipeline
(app.audio) stays vendor-neutral. Provider selection is config-driven,
see factory.py.
"""

from app.providers.asr_dashscope import ASRWSError
from app.providers.base import ProviderError, TTSWSError
from app.providers.factory import get_asr, get_llm, get_tts
from app.providers.llm import LLMError
