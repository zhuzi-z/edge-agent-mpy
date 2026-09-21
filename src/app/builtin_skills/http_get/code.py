"""Built-in HTTP GET skill: fetch remote data over HTTPS.

A generic data-fetching tool. Use it to integrate weather (e.g. wttr.in) or any
small HTTPS endpoint. Only HTTPS is supported (the injected ``http_get`` helper
wraps ``app.httpclient.https_get`` over Mbed TLS).
"""


def run(args):
    host = args.get("host")
    if not host:
        return "missing required arg: host"
    path = args.get("path", "/")
    port = args.get("port", 443)
    try:
        _code, _hdr, body = http_get(host, port, path)
    except Exception as e:  # noqa: BLE001  network / TLS / oversized-response errors
        return "http_get error: " + str(e)
    text = body.decode("utf-8", "ignore").strip()
    # Keep the tool result small so it does not blow up the LLM context.
    if len(text) > 4000:
        text = text[:4000] + "...(truncated)"
    return text
