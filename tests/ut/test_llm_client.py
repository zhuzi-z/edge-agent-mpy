"""LLM client tests: URL parsing + chat with mocked transport."""

import compat  # noqa: F401

import json
import unittest

import app.providers.llm as llm_client
from app.providers.llm import parse_base_url, api_path, chat, LLMError


class TestParseBaseUrl(unittest.TestCase):
    def test_parse_base_url_and_api_path(self):
        self.assertEqual(parse_base_url("https://api.deepseek.com"), ("api.deepseek.com", 443, ""))
        self.assertEqual(
            parse_base_url("https://dashscope.aliyuncs.com/compatible-mode"),
            ("dashscope.aliyuncs.com", 443, "/compatible-mode"),
        )
        self.assertEqual(parse_base_url("http://localhost:11434"), ("localhost", 11434, ""))
        self.assertEqual(api_path("", "/chat/completions"), "/v1/chat/completions")
        self.assertEqual(api_path("/v1", "/chat/completions"), "/v1/chat/completions")
        self.assertEqual(api_path("/compatible-mode", "/x"), "/compatible-mode/v1/x")


class TestChat(unittest.TestCase):
    def setUp(self):
        self._orig_post = llm_client.https_post_json
        self.captured = {}
        self.respond = (200, "", b'{"choices":[{"message":{"content":"hi"}}]}')

        def fake_post(host, port, path, headers, body_bytes, **kw):
            self.captured = {
                "host": host,
                "port": port,
                "path": path,
                "headers": headers,
                "body": json.loads(body_bytes.decode("utf-8")),
            }
            return self.respond

        llm_client.https_post_json = fake_post

    def tearDown(self):
        llm_client.https_post_json = self._orig_post

    def test_chat_success_paths(self):
        cfg = {"base_url": "https://api.deepseek.com", "api_key": "sk-x", "model": "m"}
        out = chat(cfg, [{"role": "user", "content": "hi"}], [])
        self.assertEqual(out["choices"][0]["message"]["content"], "hi")
        self.assertEqual(self.captured["path"], "/v1/chat/completions")
        # /v1 in the base URL must not be duplicated in the request path.
        chat(
            {"base_url": "https://api-inference.modelscope.cn/v1", "api_key": "k", "model": "m"},
            [],
            [],
        )
        self.assertEqual(self.captured["path"], "/v1/chat/completions")
        # reasoning_effort is only sent when configured.
        self.assertNotIn("reasoning_effort", self.captured["body"])
        chat(
            {
                "base_url": "https://x.io",
                "api_key": "k",
                "model": "m",
                "reasoning_effort": "high",
            },
            [],
            [],
        )
        self.assertEqual(self.captured["body"]["reasoning_effort"], "high")

    def test_chat_errors(self):
        self.respond = (401, "", b'{"error":"bad key"}')
        with self.assertRaises(LLMError) as ctx:
            chat({"base_url": "https://x.io", "api_key": "k", "model": "m"}, [], [])
        self.assertIn("401", str(ctx.exception))
        self.respond = (200, "", b"not json")
        with self.assertRaises(LLMError):
            chat({"base_url": "https://x.io", "api_key": "k", "model": "m"}, [], [])


if __name__ == "__main__":
    unittest.main(globals())
