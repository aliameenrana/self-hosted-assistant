import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .personas import PERSONAS, TOOL_PROMPT, voice_prompt
from .tools import ToolError
from .tools.registry import Tool, execute


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    outcome: str
    result: Any = None
    error: str | None = None
    retries: int = 0
    ms: int = 0


@dataclass
class Telemetry:
    model: str = ""
    ttft_ms: int = 0
    total_ms: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0

    @property
    def fabrication_possible(self) -> bool:
        """Always False by construction. The voice pass only ever sees results
        produced by execute(); it has no tool access of its own."""
        return False


class Harness:
    def __init__(self, base_url: str, model: str, registry: dict[str, Tool],
                 max_turns: int = 6, max_retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.registry = registry
        self.max_turns = max_turns
        self.max_retries = max_retries

    async def _chat(self, client: httpx.AsyncClient, messages: list[dict],
                    tools: list[dict] | None = None, stream: bool = False) -> Any:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "stream": stream, "cache_prompt": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        resp = await client.post(f"{self.base_url}/v1/chat/completions", json=body,
                                 timeout=180.0)
        resp.raise_for_status()
        return resp.json()

    async def _tool_pass(self, client: httpx.AsyncClient, question: str,
                         history: list[dict], tel: Telemetry) -> list[dict]:
        """Pass 1. Characterless. Returns verified tool results only."""
        messages = [{"role": "system", "content": TOOL_PROMPT}, *history,
                    {"role": "user", "content": question}]
        schemas = [t.schema() for t in self.registry.values()]
        last_signature = None

        for turn in range(self.max_turns):
            tel.turns = turn + 1
            data = await self._chat(client, messages, tools=schemas)
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return messages
            messages.append(msg)

            for call in calls:
                fn = call["function"]
                name = fn["name"]
                signature = f"{name}:{fn.get('arguments','')}"
                if signature == last_signature:
                    tel.tool_calls.append(ToolCall(name, {}, "loop_broken",
                                                   error="identical repeat call"))
                    return messages
                last_signature = signature

                record = await self._run_tool(call, messages, tel)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "name": name,
                                 "content": json.dumps(record, default=str)[:8000]})
        return messages

    async def _run_tool(self, call: dict, messages: list[dict],
                        tel: Telemetry) -> dict:
        fn = call["function"]
        name = fn["name"]
        started = time.monotonic()
        for attempt in range(self.max_retries + 1):
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                if attempt < self.max_retries:
                    continue
                rec = ToolCall(name, {}, "malformed", error=f"bad JSON: {exc}",
                               retries=attempt)
                tel.tool_calls.append(rec)
                return {"error": rec.error}
            try:
                result = execute(self.registry, name, args)
                rec = ToolCall(name, args, "ok", result=result, retries=attempt,
                               ms=int((time.monotonic() - started) * 1000))
                tel.tool_calls.append(rec)
                return {"result": result}
            except ToolError as exc:
                if attempt < self.max_retries:
                    continue
                rec = ToolCall(name, args, "error", error=str(exc), retries=attempt,
                               ms=int((time.monotonic() - started) * 1000))
                tel.tool_calls.append(rec)
                return {"error": str(exc)}
            except Exception as exc:
                rec = ToolCall(name, args, "error", error=f"{type(exc).__name__}",
                               retries=attempt)
                tel.tool_calls.append(rec)
                return {"error": "tool failed"}
        return {"error": "exhausted retries"}

    async def answer(self, question: str, persona: str,
                     history: list[dict] | None = None) -> AsyncIterator[dict]:
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona: {persona}")
        tel = Telemetry(model=self.model)
        started = time.monotonic()

        async with httpx.AsyncClient() as client:
            await self._tool_pass(client, question, history or [], tel)

            facts = self._render_facts(tel)
            messages = [
                {"role": "system", "content": voice_prompt(persona)},
                *(history or []),
                {"role": "user", "content": f"{question}\n\n{facts}"},
            ]

            body = {"model": self.model, "messages": messages, "stream": True,
                    "cache_prompt": True}
            first = True
            async with client.stream("POST", f"{self.base_url}/v1/chat/completions",
                                     json=body, timeout=180.0) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk["choices"][0].get("delta", {}).get("content")
                    if not delta:
                        continue
                    if first:
                        tel.ttft_ms = int((time.monotonic() - started) * 1000)
                        first = False
                    yield {"type": "token", "text": delta}

        tel.total_ms = int((time.monotonic() - started) * 1000)
        yield {"type": "done", "telemetry": tel}

    @staticmethod
    def _render_facts(tel: Telemetry) -> str:
        if not tel.tool_calls:
            return "[No tools were used. Answer from your own knowledge, and say so if unsure.]"
        lines = ["[Verified tool results. Use ONLY these. Do not invent others.]"]
        for call in tel.tool_calls:
            if call.outcome == "ok":
                lines.append(f"{call.name}: {json.dumps(call.result, default=str)[:2000]}")
            else:
                lines.append(f"{call.name}: FAILED ({call.error}). Say so briefly.")
        return "\n".join(lines)


FABRICATION_MARKERS = ("i searched", "i ran a web search", "i looked it up",
                       "i fetched", "according to my search", "i found sources")


def detect_fabrication(text: str, tel: Telemetry) -> list[str]:
    """Claims of tool use that telemetry contradicts.

    The voice pass cannot inject a fake result, but it can still assert one in
    prose. Telemetry is ground truth, so the assertion is checkable.
    """
    ran = {c.name for c in tel.tool_calls if c.outcome == "ok"}
    lowered = text.lower()
    flags = []
    if any(m in lowered for m in FABRICATION_MARKERS) and not (
            {"web_search", "fetch_url"} & ran):
        flags.append("claimed_search_without_call")
    return flags
