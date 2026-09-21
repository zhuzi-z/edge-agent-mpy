"""Provider registry: config-driven selection of vendor implementations.

Adding a new vendor (two steps):
  1. Add a module in this package implementing an interface from base.py.
  2. Register its class in the matching table below.

agent.json selects the vendor via the optional "llm_provider" /
"asr_provider" / "tts_provider" keys; the first table entry of each
kind is the default.
"""

from app.providers.asr_dashscope import DashScopeASR
from app.providers.base import ProviderError
from app.providers.llm import OpenAICompatLLM
from app.providers.tts_dashscope import DashScopeTTS

_LLM_PROVIDERS = {"openai_compat": OpenAICompatLLM}
_ASR_PROVIDERS = {"dashscope": DashScopeASR}
_TTS_PROVIDERS = {"dashscope": DashScopeTTS}

_llm_cache = {}
_asr_cache = {}
_tts_cache = {}


def _get(table, cache, kind, name):
    provider = cache.get(name)
    if provider is None:
        cls = table.get(name)
        if cls is None:
            raise ProviderError("unknown {} provider: {}".format(kind, name))
        provider = cls()
        cache[name] = provider
    return provider


def get_llm(cfg):
    """LLM provider selected by cfg['llm_provider'] (default openai_compat)."""
    return _get(_LLM_PROVIDERS, _llm_cache, "LLM", cfg.get("llm_provider") or "openai_compat")


def get_asr(cfg):
    """ASR provider selected by cfg['asr_provider'] (default dashscope)."""
    return _get(_ASR_PROVIDERS, _asr_cache, "ASR", cfg.get("asr_provider") or "dashscope")


def get_tts(cfg):
    """TTS provider selected by cfg['tts_provider'] (default dashscope)."""
    return _get(_TTS_PROVIDERS, _tts_cache, "TTS", cfg.get("tts_provider") or "dashscope")
