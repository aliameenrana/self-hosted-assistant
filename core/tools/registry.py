import ast
import operator
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from .artifacts import create_page


class ToolError(Exception):
    pass


FETCH_ALLOWLIST = {
    "en.wikipedia.org",
    "duckduckgo.com",
    "api.duckduckgo.com",
}

MAX_FETCH_BYTES = 200_000


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
    if len(query) > 300:
        raise ToolError("query too long")
    resp = httpx.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    results = [
        {"title": t.get("Text", ""), "url": t.get("FirstURL", "")}
        for t in data.get("RelatedTopics", [])[:5]
        if t.get("FirstURL")
    ]
    return {"query": query, "abstract": data.get("AbstractText", ""), "results": results}


def _fetch_url(url: str) -> dict[str, Any]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ToolError("https only")
    if parsed.hostname not in FETCH_ALLOWLIST:
        raise ToolError(f"domain not allowed: {parsed.hostname}")
    resp = httpx.get(url, timeout=10.0, follow_redirects=False)
    resp.raise_for_status()
    return {"url": url, "content": resp.text[:MAX_FETCH_BYTES]}


class Tool:
    def __init__(self, name: str, description: str, params: dict, fn: Callable):
        self.name = name
        self.description = description
        self.params = params
        self.fn = fn

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params,
            },
        }


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
             "Current date and time in UTC. Use when the answer depends on "
             "today's date, the current time, or how long until something. "
             "Do not use for historical dates you already know.",
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
             "current events, recent releases, or anything after your training "
             "data. Do not use for general knowledge you already have, or for "
             "opinions and reasoning.",
             _obj({"query": {
                 "type": "string",
                 "description": "Search keywords, not a full sentence."}},
                  ["query"]), _web_search),
        Tool("read_url",
             "Fetch the text of a specific web page. Use only when the user "
             "gives a URL or a search result needs reading in full. Only "
             "allowlisted domains work. Do not guess URLs.",
             _obj({"url": {"type": "string",
                           "description": "Full https URL."}},
                  ["url"]), _fetch_url),
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
                  ["title", "html"]), _create_webpage),
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
