"""Built-in web search skill via Keenable API.

Uses the free public endpoint (no API key required) at
https://api.keenable.ai/v1/search/public which returns JSON results.
"""


def run(args):
    query = args.get("query", "")
    if not query:
        return "missing required arg: query"
    count = args.get("count", 5)
    if count < 1:
        count = 1
    if count > 10:
        count = 10

    body = json.dumps({"query": query}).encode("utf-8")
    try:
        code, headers, resp = http_post_json(
            "api.keenable.ai",
            443,
            "/v1/search/public",
            body,
            headers={"X-Keenable-Title": "edge-agent"},
            max_bytes=32768,
        )
    except Exception as e:
        return "web_search error: " + str(e)

    if code == 429:
        return "web_search error: rate limited, try again later"
    if code != 200:
        return "web_search error: HTTP {}".format(code)

    try:
        data = json.loads(resp.decode("utf-8", "ignore"))
    except ValueError:
        return "web_search error: invalid JSON response"

    raw_results = data.get("results", [])
    if not raw_results:
        return "No results for: " + query

    lines = ["Results for: " + query, ""]
    for i, item in enumerate(raw_results[:count], 1):
        title = item.get("title", "")
        url = item.get("url", "")
        snippet = item.get("snippet") or item.get("description", "")
        lines.append("{}. {}".format(i, title))
        if url:
            lines.append("   " + url)
        if snippet:
            lines.append("   " + snippet)
    return "\n".join(lines)
