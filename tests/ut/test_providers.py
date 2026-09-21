"""Provider factory tests: config-driven dispatch and errors."""

import compat  # noqa: F401

import unittest

from app.providers import ProviderError, get_asr, get_llm, get_tts
from app.providers.asr_dashscope import DashScopeASR
from app.providers.llm import OpenAICompatLLM
from app.providers.tts_dashscope import DashScopeTTS


class TestFactory(unittest.TestCase):
    def test_default_dispatch_and_caching(self):
        self.assertIsInstance(get_llm({}), OpenAICompatLLM)
        self.assertIsInstance(get_asr({}), DashScopeASR)
        self.assertIsInstance(get_tts({}), DashScopeTTS)
        # Explicit default name resolves to the same cached instance.
        self.assertIs(get_asr({}), get_asr({"asr_provider": "dashscope"}))
        self.assertIs(get_tts({}), get_tts({"tts_provider": "dashscope"}))
        self.assertIs(get_llm({}), get_llm({"llm_provider": "openai_compat"}))

    def test_unknown_provider_rejected(self):
        for getter, key in (
            (get_llm, "llm_provider"),
            (get_asr, "asr_provider"),
            (get_tts, "tts_provider"),
        ):
            with self.assertRaises(ProviderError):
                getter({key: "nope"})


if __name__ == "__main__":
    unittest.main(globals())
