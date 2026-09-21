"""OpenAI-compatible chat completions client."""

import json
import app.config as config
from app.httpclient import https_post_json, https_post_json_async, HttpError
from app.providers.base import LLMProvider, ProviderError


class LLMError(ProviderError):
    """LLM endpoint failure or unusable response."""


def parse_base_url(base_url):
    """Split base URL into (host, port, path_prefix)."""
    scheme_https = True
    b = base_url or ""
    if b.startswith("https://"):
        b = b[8:]
    elif b.startswith("http://"):
        b = b[7:]
        scheme_https = False
    if "/" in b:
        host, rest = b.split("/", 1)
        prefix = "/" + rest
    else:
        host, prefix = b, ""
    port = 443 if scheme_https else 80
    if ":" in host:
        host, p = host.split(":", 1)
        try:
            port = int(p)
        except ValueError:
            pass
    return host, port, prefix.rstrip("/")


def api_path(prefix, suffix):
    """Join prefix + suffix, avoiding duplicate /v1."""
    if prefix.endswith("/v1"):
        return prefix + suffix
    return prefix + "/v1" + suffix


def _build_request(cfg, messages, tools):
    """Build (host, port, path, headers, body_bytes) for a chat request."""
    host, port, prefix = parse_base_url(cfg.get("base_url", ""))
    path = api_path(prefix, "/chat/completions")
    body = {
        "model": cfg.get("model", ""),
        "messages": messages,
    }
    effort = cfg.get("reasoning_effort")
    if effort:
        body["reasoning_effort"] = effort
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {
        "Authorization": "Bearer " + cfg.get("api_key", ""),
    }
    return host, port, path, headers, json.dumps(body).encode("utf-8")


def _parse_response(code, resp):
    """Validate the status and decode the JSON response body."""
    if code != 200:
        try:
            snippet = resp[:200].decode("utf-8")
        except UnicodeError:
            snippet = str(resp[:200])
        raise LLMError("HTTP {}: {}".format(code, snippet))
    try:
        return json.loads(resp.decode("utf-8"))
    except (ValueError, UnicodeError) as e:
        raise LLMError("bad JSON from LLM: {}".format(e))


def chat(cfg, messages, tools=None, timeout=None):
    """Call /v1/chat/completions (blocking). Returns parsed JSON dict."""
    host, port, path, headers, body_bytes = _build_request(cfg, messages, tools)
    ca_path = cfg.get("ca_path") or None
    try:
        code, _hdr, resp = https_post_json(
            host,
            port,
            path,
            headers,
            body_bytes,
            timeout=timeout or config.LLM_REQUEST_TIMEOUT_SEC,
            ca_path=ca_path,
            # Reuse the connection: dns+tcp+tls measured 621ms on device (514ms
            # of it the mbedTLS handshake), paid once per turn *and* once per
            # tool-call round. A socket the gateway reaped while idle is
            # detected and replaced inside https_request.
            keep_alive=config.HTTP_KEEPALIVE,
        )
    except HttpError as e:
        raise LLMError(str(e))
    return _parse_response(code, resp)


async def chat_async(cfg, messages, tools=None, timeout=None):
    """Call /v1/chat/completions (async). Returns parsed JSON dict."""
    host, port, path, headers, body_bytes = _build_request(cfg, messages, tools)
    ca_path = cfg.get("ca_path") or None
    try:
        code, _hdr, resp = await https_post_json_async(
            host,
            port,
            path,
            headers,
            body_bytes,
            timeout=timeout or config.LLM_REQUEST_TIMEOUT_SEC,
            ca_path=ca_path,
            keep_alive=config.HTTP_KEEPALIVE,
        )
    except HttpError as e:
        raise LLMError(str(e))
    return _parse_response(code, resp)


class OpenAICompatLLM(LLMProvider):
    """Any endpoint speaking OpenAI /v1/chat/completions."""

    name = "openai_compat"

    def chat(self, cfg, messages, tools=None, timeout=None):
        return chat(cfg, messages, tools, timeout)

    async def chat_async(self, cfg, messages, tools=None, timeout=None):
        return await chat_async(cfg, messages, tools, timeout)
