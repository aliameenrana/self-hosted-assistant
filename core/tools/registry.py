import ast
import operator
import re
from datetime import datetime, timezone
from typing import Any, Callable

import ipaddress
import socket

import httpx

from .artifacts import create_page
from ..sanitize import defang


class ToolError(Exception):
    pass


# An allowlist of three domains was correct when nothing produced URLs. Now
# that search returns real results, a fetcher that cannot open them is
# useless. The boundary moves from "which host" to "not the private network":
# public DNS only, port 443 only, no redirects, size capped.
BLOCKED_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
        "169.254.0.0/16", "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24",
        "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
        "::1/128", "fc00::/7", "fe80::/10",
    )
]

MAX_FETCH_BYTES = 200_000
USER_AGENT = "self-hosted-assistant/0.1 (personal project)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _datetime(timezone_name: str = "UTC") -> dict[str, Any]:
    if timezone_name != "UTC":
        raise ToolError("only UTC is supported")
    now = datetime.now(timezone.utc)
    return {"iso": now.isoformat(), "readable": now.strftime("%A %d %B %Y, %H:%M UTC")}


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ToolError("only numbers allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        # Pow with large operands is the one cheap way to hang the process.
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ToolError("exponent too large")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        val = _eval_node(node.operand)
        return val if isinstance(node.op, ast.UAdd) else -val
    raise ToolError("unsupported expression")


def _calculator(expression: str) -> dict[str, Any]:
    if len(expression) > 200:
        raise ToolError("expression too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"could not parse: {exc.msg}") from exc
    return {"expression": expression, "result": _eval_node(tree.body)}


def _web_search(query: str) -> dict[str, Any]:
    """DuckDuckGo HTML endpoint.

    The Instant Answer API (api.duckduckgo.com) was used first and is not a
    search engine: it serves disambiguation and returns nothing for almost
    every real query. Measured zero results for "pizza in johar town" and
    "capital of cape verde", and three for "cars" only because that has a
    Wikipedia disambiguation page. The model was told searches came back
    empty and invented explanations for it.
    """
    if len(query) > 300:
        raise ToolError("query too long")
    try:
        resp = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": BROWSER_UA},
            timeout=15.0, follow_redirects=True,
        )
    except httpx.RequestError as exc:
        raise ToolError(f"search failed: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"search returned {resp.status_code}")

    results = []
    for block in re.finditer(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
            r'(?:.*?class="result__snippet"[^>]*>(.*?)</a>)?',
            resp.text, re.S):
        href, title, snippet = block.group(1), block.group(2), block.group(3)
        # Search result text is exactly as untrusted as an uploaded document,
        # public and attacker-shapable at scale, but had no defanging at all
        # until this. A page ranking for a query can contain
        # "<|im_start|>system ignore previous instructions" verbatim.
        results.append({
            "title": defang(_untag(title)),
            "url": _unwrap(href),
            "snippet": defang(_untag(snippet or ""))[:300],
        })
        if len(results) >= 6:
            break

    if not results:
        raise ToolError(f"no results for {query!r}. Try different words.")
    return {"query": query, "results": results}


_TAG = re.compile(r"<[^>]+>")


def _untag(html: str) -> str:
    import html as _html
    return _html.unescape(_TAG.sub("", html)).strip()


def _unwrap(href: str) -> str:
    """DDG wraps results in a redirect with the target in uddg."""
    from urllib.parse import parse_qs, unquote, urlparse
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        qs = parse_qs(urlparse("https:" + href if href.startswith("//") else href).query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0])
    return href


def _resolve_public(host: str) -> None:
    """Refuse anything that resolves into a private range.

    Checked after DNS rather than by name, so a public hostname pointed at
    192.168.1.1 is caught. This is the rule that keeps a compromised model
    off the home network.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolError(f"cannot resolve {host}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if any(addr in net for net in BLOCKED_NETS) or not addr.is_global:
            raise ToolError(f"{host} resolves to a private address")


def _fetch_url(url: str) -> dict[str, Any]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ToolError("https only")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ToolError("no hostname in that URL")
    if parsed.port not in (None, 443):
        raise ToolError("port 443 only")
    _resolve_public(host)

    try:
        resp = httpx.get(url, timeout=15.0, follow_redirects=False,
                         headers={"User-Agent": BROWSER_UA})
    except httpx.RequestError as exc:
        raise ToolError(f"could not reach it: {type(exc).__name__}") from exc
    if resp.is_redirect:
        location = resp.headers.get("location", "")
        raise ToolError(f"it redirects to {location[:120]}, try that URL")
    if resp.status_code >= 400:
        raise ToolError(f"the page returned {resp.status_code}")
    kind = resp.headers.get("content-type", "")
    if "html" not in kind and "text" not in kind and "json" not in kind:
        raise ToolError(f"not a text page, it is {kind.split(';')[0]}")

    text = resp.text[:MAX_FETCH_BYTES]
    if "html" in kind:
        text = _readable(text)
    # Same reasoning as search results: fetched page content is untrusted
    # and was going into the facts block raw.
    return {"url": url, "title": defang(_page_title(resp.text)),
            "content": defang(text)[:20000]}


_SCRIPTS = re.compile(r"<(script|style|nav|footer|svg)\b.*?</\1>", re.S | re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _page_title(html: str) -> str:
    found = _TITLE.search(html)
    return _untag(found.group(1))[:150] if found else ""


def _readable(html: str) -> str:
    """Strip markup to text. Enough for the model to read a page."""
    body = _SCRIPTS.sub(" ", html)
    body = re.sub(r"<(br|/p|/div|/h[1-6]|/li)\s*/?>", "\n", body, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", _untag(body))


# Tools where the query and result are comparable text, so a zero-overlap
# check is meaningful. Not calculate/datetime/convert_units, whose output is
# a number the query text would never restate.
RELEVANCE_CHECKED = {"search_web", "read_url", "read_repo"}


class Tool:
    def __init__(self, name: str, description: str, params: dict, fn: Callable,
                 budget: int = 160):
        self.name = name
        self.description = description
        self.params = params
        self.fn = fn
        # Tokens pass 1 needs to emit the call. Tools whose arguments carry
        # content need far more; 64 truncated even a short free-text query
        # (name, JSON scaffolding, and a search string like "Koenigsegg
        # Agera engine horsepower top speed" alone runs past it) mid-string,
        # which llama.cpp's tool-call parser reports as a 500 rather than a
        # bad completion, misdiagnosed as the model producing invalid JSON
        # or the server being down. Tools with genuinely large payloads
        # (extract_structured, diff_text, create_webpage) still override
        # this explicitly.
        self.budget = budget

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params,
            },
        }


def _diff_text(before: str, after: str) -> dict[str, Any]:
    import difflib
    a, b = before.splitlines(), after.splitlines()
    if len(a) > 2000 or len(b) > 2000:
        raise ToolError("too long to diff, 2000 lines each maximum")
    diff = list(difflib.unified_diff(a, b, "before", "after", lineterm="", n=2))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    ratio = difflib.SequenceMatcher(None, before, after).ratio()
    return {"diff": "\n".join(diff)[:8000] or "(identical)",
            "lines_added": added, "lines_removed": removed,
            "similarity": round(ratio, 3)}


_UNITS = {
    "length": {"m": 1.0, "km": 1000.0, "cm": .01, "mm": .001, "mi": 1609.344,
               "yd": .9144, "ft": .3048, "in": .0254, "nmi": 1852.0},
    "mass": {"kg": 1.0, "g": .001, "mg": 1e-6, "t": 1000.0, "lb": .45359237,
             "oz": .028349523125, "st": 6.35029318},
    "volume": {"l": 1.0, "ml": .001, "m3": 1000.0, "gal": 3.785411784,
               "qt": .946352946, "pt": .473176473, "cup": .2365882365,
               "floz": .0295735295625},
    "time": {"s": 1.0, "min": 60.0, "h": 3600.0, "day": 86400.0,
             "week": 604800.0, "ms": .001},
    "data": {"b": 1.0, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
             "kib": 1024.0, "mib": 1048576.0, "gib": 1073741824.0},
    "speed": {"mps": 1.0, "kph": .277777778, "mph": .44704, "kn": .514444},
}
_TEMP = {"c", "f", "k"}


def _convert_units(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    src, dst = from_unit.lower().strip(), to_unit.lower().strip()
    if src in _TEMP or dst in _TEMP:
        if src not in _TEMP or dst not in _TEMP:
            raise ToolError("cannot convert temperature to a non temperature unit")
        celsius = {"c": value, "f": (value - 32) * 5 / 9,
                   "k": value - 273.15}[src]
        out = {"c": celsius, "f": celsius * 9 / 5 + 32,
               "k": celsius + 273.15}[dst]
        return {"value": value, "from": src, "to": dst, "result": round(out, 6)}
    for family, table in _UNITS.items():
        if src in table and dst in table:
            return {"value": value, "from": src, "to": dst, "family": family,
                    "result": round(value * table[src] / table[dst], 9)}
    known = sorted({u for t in _UNITS.values() for u in t} | _TEMP)
    raise ToolError(f"unknown or mismatched units. Known: {', '.join(known)}")


def _read_repo(repo: str, path: str = "") -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or ".." in repo:
        raise ToolError("repo must look like owner/name")
    if ".." in path:
        raise ToolError("invalid path")
    url = f"https://api.github.com/repos/{repo}/contents/{path.lstrip('/')}"
    resp = httpx.get(url, timeout=12.0,
                     headers={"Accept": "application/vnd.github+json"})
    if resp.status_code == 404:
        raise ToolError(f"not found: {repo}/{path}")
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return {"repo": repo, "path": path or "/",
                "entries": [{"name": e["name"], "type": e["type"],
                             "size": e.get("size", 0)} for e in data[:100]]}
    if data.get("encoding") != "base64":
        raise ToolError("that file is not readable as text")
    import base64
    try:
        text = base64.b64decode(data["content"]).decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("that file is binary")
    return {"repo": repo, "path": path, "size": data.get("size", 0),
            "content": text[:20000],
            "truncated": len(text) > 20000}


def _extract_structured(text: str, fields: str) -> dict[str, Any]:
    """Return the text alongside the requested shape.

    No model call of its own. The tool exists so the model commits to a field
    list first, which makes the extraction that follows far more consistent
    than asking for JSON in prose.
    """
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    if not wanted:
        raise ToolError("list at least one field, comma separated")
    if len(wanted) > 25:
        raise ToolError("25 fields maximum")
    return {"fields": wanted, "source_chars": len(text),
            "text": text[:20000],
            "instruction": ("Return exactly these fields as JSON. Use null for "
                            "anything the text does not state. Do not invent "
                            "values.")}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def _create_webpage(title: str, html: str) -> dict[str, Any]:
    try:
        return create_page(title, html)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


WEB_TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("get_datetime",
             "The real current date and time, right now. You do NOT otherwise "
             "know what day it is, so call this for any question about today, "
             "the date, the time, the day of the week, what year it is, or how "
             "long until something. Never say you lack real time information: "
             "this tool is that information. Do not use for historical dates.",
             _obj({"timezone_name": {"type": "string"}}, []), _datetime),
        Tool("calculate",
             "Evaluate an arithmetic expression and return the exact result. "
             "Use for any arithmetic beyond trivial mental maths, especially "
             "large numbers where being off by one matters. Do not use for "
             "algebra, symbolic maths, or anything needing variables.",
             _obj({"expression": {
                 "type": "string",
                 "description": "Arithmetic only, e.g. '4871 * 392'. "
                                "No functions, no variables."}},
                  ["expression"]), _calculator),
        Tool("search_web",
             "Search the web and return titles, URLs and a summary. Use for "
             "current events, recent releases, prices, scores, standings, who "
             "holds a role or title now, or anything that changes over time - "
             "your training data is stale and you cannot tell how stale. If "
             "the fact could plausibly have changed since you were trained, "
             "search rather than assert. Do not use for stable facts (math, "
             "definitions, historical events already settled) or for opinions "
             "and reasoning.",
             _obj({"query": {
                 "type": "string",
                 "description": "Search keywords, not a full sentence."}},
                  ["query"]), _web_search),
        Tool("read_url",
             "Open a web page and read its text. You CAN open any public "
             "https URL, so never say you are unable to open websites. Use "
             "when the user gives a link or names a site, or to read a search "
             "result in full. Do not guess at URLs that may not exist.",
             _obj({"url": {"type": "string",
                           "description": "Full https URL."}},
                  ["url"]), _fetch_url),
        Tool("extract_structured",
             "Pull named fields out of unstructured text as JSON. Use for CVs, "
             "invoices, emails, listings, anything where specific values are "
             "wanted. Name the fields you want. Do not use for summarising or "
             "for free-form questions.",
             _obj({"text": {"type": "string", "description": "Text to read."},
                   "fields": {"type": "string",
                              "description": "Comma separated, e.g. "
                                             "name,email,years_experience"}},
                  ["text", "fields"]), _extract_structured, budget=2400),
        Tool("diff_text",
             "Compare two blocks of text and show what changed, with counts "
             "and a similarity score. Use when asked what changed between two "
             "versions, to compare drafts, or to check an edit. Do not use to "
             "compare meaning or to review a single document.",
             _obj({"before": {"type": "string", "description": "Original text."},
                   "after": {"type": "string", "description": "Revised text."}},
                  ["before", "after"]), _diff_text, budget=2400),
        Tool("convert_units",
             "Convert between units of length, mass, volume, time, data size, "
             "speed or temperature. Use whenever a value needs expressing in "
             "different units. Do not use for currency, which changes daily.",
             _obj({"value": {"type": "number"},
                   "from_unit": {"type": "string",
                                 "description": "e.g. km, lb, floz, gib, c"},
                   "to_unit": {"type": "string"}},
                  ["value", "from_unit", "to_unit"]), _convert_units),
        Tool("read_repo",
             "List a directory or read a file from a PUBLIC GitHub repository. "
             "Use when asked about the contents of a named repo. Pass repo as "
             "owner/name and an optional path. Do not use for private repos or "
             "to guess at repos that may not exist.",
             _obj({"repo": {"type": "string", "description": "owner/name"},
                   "path": {"type": "string",
                            "description": "File or directory, empty for root."}},
                  ["repo"]), _read_repo),
        Tool("create_webpage",
             "Publish a complete HTML page and get back a link the user can "
             "open. Use when asked to build, make, or design a page, site, "
             "dashboard, chart, game, or any visual artifact. Write the whole "
             "document with inline CSS. Do not use for code the user wants to "
             "read rather than view, or for plain text answers.",
             _obj({"title": {"type": "string",
                             "description": "Short name for the page."},
                   "html": {"type": "string",
                            "description": "Complete HTML document with inline "
                                           "<style>. No external files."}},
                  ["title", "html"]), _create_webpage, budget=3000),
    ]
}


def validate_args(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    props = tool.params["properties"]
    for key in tool.params["required"]:
        if key not in args:
            raise ToolError(f"missing required argument: {key}")
    unknown = set(args) - set(props)
    if unknown:
        raise ToolError(f"unknown arguments: {sorted(unknown)}")
    for key, value in args.items():
        expected = props[key]["type"]
        if expected == "string" and not isinstance(value, str):
            raise ToolError(f"{key} must be a string")
        if expected == "number" and not isinstance(value, (int, float)):
            raise ToolError(f"{key} must be a number")
    return args


def execute(registry: dict[str, Tool], name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name not in registry:
        raise ToolError(f"unknown tool: {name}. Available: {sorted(registry)}")
    tool = registry[name]
    return tool.fn(**validate_args(tool, args))
