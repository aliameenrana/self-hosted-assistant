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


REPAIR = {
    "malformed": "Your arguments were not valid JSON. Call it again with "
                 "correctly formed JSON arguments.",
    "unknown_tool": "That tool does not exist. Pick one from the list, or "
                    "answer without a tool.",
    "bad_args": "The arguments were rejected. Read the error, fix them, and "
                "call it once more.",
    "tool_error": "The tool itself failed. Do not repeat the same call. Try a "
                  "different tool or answer without one.",
    "empty": "That returned nothing useful. A different query might work, or "
             "answer from your own knowledge and say the lookup came back empty.",
}


def _is_empty(result: Any) -> bool:
    """Structurally valid but semantically useless. Agents over-trust these."""
    if result is None:
        return True
    if isinstance(result, dict):
        meaningful = [v for k, v in result.items()
                      if k not in ("query", "url", "expression")]
        return not any(v not in (None, "", [], {}, 0) for v in meaningful)
    return result in ("", [], {})


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
                 max_turns: int = 6, max_retries: int = 2,
                 decide_tokens: int = 64):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.registry = registry
        self.max_turns = max_turns
        self.max_retries = max_retries
        self.decide_tokens = decide_tokens

    async def _chat(self, client: httpx.AsyncClient, messages: list[dict],
                    tools: list[dict] | None = None, stream: bool = False,
                    max_tokens: int | None = None) -> Any:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "stream": stream, "cache_prompt": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
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
        attempts: dict[str, int] = {}

        for turn in range(self.max_turns):
            tel.turns = turn + 1
            data = await self._chat(client, messages, tools=schemas,
                                    max_tokens=self.decide_tokens)
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

                record = await self._run_tool(call, tel)
                payload = dict(record)
                failure = payload.pop("failure", None)
                if failure:
                    payload["next_step"] = REPAIR[failure]
                    attempts[name] = attempts.get(name, 0) + 1
                    if attempts[name] > self.max_retries:
                        payload["next_step"] = (
                            f"{name} has failed {attempts[name]} times. Stop calling "
                            "it. Answer without it and say plainly that it failed.")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "name": name,
                                 "content": json.dumps(payload, default=str)[:8000]})
        return messages

    async def _run_tool(self, call: dict, tel: Telemetry) -> dict:
        """Execute once and classify. Recovery is the caller's job, because a
        retry that resends identical arguments produces an identical failure.
        """
        fn = call["function"]
        name = fn["name"]
        started = time.monotonic()
        elapsed = lambda: int((time.monotonic() - started) * 1000)

        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            tel.tool_calls.append(ToolCall(name, {}, "malformed",
                                           error=f"arguments were not valid JSON: {exc}"))
            return {"failure": "malformed", "error": f"invalid JSON: {exc}"}

        if name not in self.registry:
            tel.tool_calls.append(ToolCall(name, args, "unknown_tool",
                                           error=f"no such tool: {name}"))
            return {"failure": "unknown_tool",
                    "error": f"no tool named {name}. Available: {sorted(self.registry)}"}

        try:
            result = execute(self.registry, name, args)
        except ToolError as exc:
            tel.tool_calls.append(ToolCall(name, args, "bad_args", error=str(exc),
                                           ms=elapsed()))
            return {"failure": "bad_args", "error": str(exc)}
        except Exception as exc:
            tel.tool_calls.append(ToolCall(name, args, "tool_error",
                                           error=type(exc).__name__, ms=elapsed()))
            return {"failure": "tool_error",
                    "error": f"{name} failed: {type(exc).__name__}"}

        if _is_empty(result):
            tel.tool_calls.append(ToolCall(name, args, "empty", result=result,
                                           ms=elapsed()))
            return {"failure": "empty", "result": result,
                    "error": f"{name} returned nothing useful"}

        tel.tool_calls.append(ToolCall(name, args, "ok", result=result, ms=elapsed()))
        return {"result": result}

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
            return ("[Internal note, never mention this: no tools ran. Answer "
                    "normally from your own knowledge. Say you are unsure only if "
                    "you genuinely are.]")
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
