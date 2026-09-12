import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .personas import (PERSONAS, STOP, TOOL_PROMPT, VOICE_MAX_TOKENS,
                       voice_prompt)
from .tools import ToolError
from .tools.registry import RELEVANCE_CHECKED, Tool, execute


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    outcome: str
    result: Any = None
    error: str | None = None
    retries: int = 0
    ms: int = 0


def _recap(history: list[dict]) -> str:
    """Topics only, never prior assistant prose.

    Splicing raw history into the voice pass let it read last turn's rendered
    results as its own confident text and re-serve them for this turn. That is
    where the Pixar film appeared in a pizza search.
    """
    asks = [m["content"].strip().replace("\n", " ")[:90]
            for m in history if m.get("role") == "user"][-4:]
    system = [m["content"] for m in history if m.get("role") == "system"]
    parts = []
    if asks:
        parts.append("Earlier in this conversation the user asked about: "
                     + "; ".join(asks))
    parts.extend(system)
    return "\n\n".join(parts)


def _summarise_result(name: str, result: Any) -> str:
    """One line for the trace, not the raw payload. Keeps the transcript
    readable and keeps token cost down when it round-trips through context."""
    if not isinstance(result, dict):
        return str(result)[:120]
    if "results" in result:
        n = len(result["results"])
        first = result["results"][0]["title"] if result["results"] else ""
        return f"{n} result(s), top: {first[:80]}" if n else "no results"
    if "content" in result:
        return f"{result.get('title') or 'page'}, {len(result['content'])} chars"
    if "result" in result:
        return str(result["result"])
    return json.dumps(result, default=str)[:120]


def strip_em_dashes(text: str) -> str:
    """Prompting for this failed repeatedly, so it is enforced after the fact.

    Spaced em dash becomes a comma, unspaced becomes a comma plus space.
    En dash between digits is a range and is left alone.
    """
    text = re.sub(r"\s*\u2014\s*", ", ", text)
    text = re.sub(r"(?<![0-9])\s*\u2013\s*(?![0-9])", ", ", text)
    return re.sub(r",\s*,", ",", text)


REPAIR = {
    "malformed": "Your arguments were not valid JSON. Call it again with "
                 "correctly formed JSON arguments.",
    "truncated": "Your arguments were cut off before they finished. Call it "
                 "again with shorter content.",
    "unknown_tool": "That tool does not exist. Pick one from the list, or "
                    "answer without a tool.",
    "bad_args": "The arguments were rejected. Read the error, fix them, and "
                "call it once more.",
    "tool_error": "The tool itself failed. Do not repeat that exact call. Try "
                  "a different tool, or stop and report the failure plainly.",
    "empty": "That returned nothing. Retry ONCE with simpler or more common "
             "words: drop the location, the preposition, or any rare term. "
             "Do not answer from memory as though you had looked it up.",
}


_STOP = {"the","a","an","is","are","was","for","of","and","in","on","to",
        "search","find","term","what","how","do","you","can"}


def _relevance(query: str, result: Any) -> float:
    """Zero model calls. Word overlap between the query and the result text.

    A result can be well formed and still not answer the question, which is
    the case a status code cannot catch. "search zzqqx nonexistent" that comes
    back with an unrelated game is syntactically fine and semantically noise.
    """
    q = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOP
        and len(w) > 2}
    if not q:
        return 1.0
    text = json.dumps(result, default=str).lower()
    hit = sum(1 for w in q if w in text)
    return hit / len(q)


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
    tools_offered: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    @property
    def fabrication_possible(self) -> bool:
        """Always False by construction. The voice pass only ever sees results
        produced by execute(); it has no tool access of its own."""
        return False


class Harness:
    def __init__(self, base_url: str, model: str, registry: dict[str, Tool],
                 max_turns: int = 6, max_retries: int = 2,
                 decide_tokens: int = 64, continuation_tokens: int = 400):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.registry = registry
        self.max_turns = max_turns
        self.max_retries = max_retries
        self.decide_tokens = decide_tokens
        self.continuation_tokens = continuation_tokens
        self.router = None


    async def _chat(self, client: httpx.AsyncClient, messages: list[dict],
                    tools: list[dict] | None = None, stream: bool = False,
                    max_tokens: int | None = None, force: bool = False) -> Any:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "stream": stream, "cache_prompt": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "required" if force else "auto"
        resp = await client.post(f"{self.base_url}/v1/chat/completions", json=body,
                                 timeout=180.0)
        resp.raise_for_status()
        return resp.json()

    async def _tool_pass(self, client: httpx.AsyncClient, question: str,
                         history: list[dict], tel: Telemetry) -> list[dict]:
        """Pass 1. Characterless. Returns verified tool results only."""
        messages = [{"role": "system", "content": TOOL_PROMPT}, *history,
                    {"role": "user", "content": question}]
        # Offer only the tools relevant to this message. Router accuracy drops
        # once a small model sees more than about eight at once.
        offered = self.registry
        if self.router:
            offered, tel.tools_offered = self.router.select(question, self.registry)
        schemas = [t.schema() for t in offered.values()]
        last_signature = None
        attempts: dict[str, int] = {}
        forced = False

        for turn in range(self.max_turns):
            tel.turns = turn + 1
            budget = max([t.budget for t in offered.values()] or
                          [self.decide_tokens])
            if tel.tool_calls:
                # A tool already ran this turn. The model may be reasoning
                # toward a second call, or about to answer directly - either
                # way it needs more than "emit one JSON call" room.
                budget = max(budget, self.continuation_tokens)
            data = await self._chat(client, messages, tools=schemas,
                                    max_tokens=budget)
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            said = (msg.get("content") or "").strip()
            truncated = (data["choices"][0].get("finish_reason") == "length"
                        and not calls)
            if truncated:
                tel.trace.append(f"turn {turn + 1}: hit the {budget} token cap")
                # It committed to answering without a tool but ran out of room
                # mid-answer. Pass 1's prose is discarded either way, so retry
                # with one token asking only for the decision, not the prose.
                data = await self._chat(
                    client, messages + [{"role": "assistant", "content": ""},
                    {"role": "user", "content":
                     "Reply with just a tool call if one is needed, or a "
                     "single word 'none' if not. No other text."}],
                    tools=schemas, max_tokens=32)
                msg = data["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                said = ""
            if not calls:
                # Omission is the dominant failure for models this size: it
                # answers in prose while holding the tool. Force the call once
                # rather than accept a refusal.
                if schemas and not forced and REFUSAL.search(said):
                    forced = True
                    tel.trace.append(
                        f"turn {turn + 1}: refused in prose, forcing the tool")
                    data = await self._chat(client, messages, tools=schemas,
                                            max_tokens=budget, force=True)
                    msg = data["choices"][0]["message"]
                    calls = msg.get("tool_calls") or []
                    said = (msg.get("content") or "").strip()
                if not calls:
                    tel.trace.append(
                        f"turn {turn + 1}: chose no tool"
                        + (f" — {said[:200]}" if said else ""))
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

                try:
                    shown = json.dumps(json.loads(fn.get("arguments") or "{}"))[:160]
                except json.JSONDecodeError:
                    shown = (fn.get("arguments") or "")[:160]
                tel.trace.append(f"turn {turn + 1}: call {name}({shown})")
                record = await self._run_tool(call, tel)
                payload = dict(record)
                failure = payload.pop("failure", None)
                if failure:
                    tel.trace.append(
                        f"  -> {failure}: {str(payload.get('error'))[:140]}")
                    payload["next_step"] = REPAIR[failure]
                    attempts[name] = attempts.get(name, 0) + 1
                    if attempts[name] > self.max_retries:
                        payload["next_step"] = (
                            f"{name} has failed {attempts[name]} times. Stop calling "
                            "it. Answer without it and say plainly that it failed.")
                else:
                    tel.trace.append(f"  -> ok: {_summarise_result(name, payload.get('result'))}")
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
            raw = fn.get("arguments") or ""
            # Unterminated string means the budget cut it off, not that the
            # model emitted bad JSON. Different cause, different repair.
            cut = raw.count('"') % 2 == 1 or not raw.rstrip().endswith("}")
            kind = "truncated" if cut else "malformed"
            tel.tool_calls.append(ToolCall(name, {}, kind, error=str(exc)))
            return {"failure": kind, "error": str(exc)}

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

        if name in RELEVANCE_CHECKED:
            query_text = next(iter(args.values()), "") if args else ""
            score = _relevance(str(query_text), result)
            if score == 0:
                tel.tool_calls.append(ToolCall(name, args, "irrelevant",
                                               result=result, ms=elapsed()))
                return {"failure": "irrelevant", "result": result,
                        "error": f"{name} returned results with no overlap "
                                 "with the query, likely off-topic"}

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

            tel.trace.append(f"voice pass: {len(tel.tool_calls)} verified "
                             f"result(s) handed to the writer")
            facts = self._render_facts(tel)
            messages = [{"role": "system", "content": voice_prompt(persona)}]
            recap = _recap(history or [])
            if recap:
                messages.append({"role": "system", "content": recap})
            messages.append({"role": "user", "content": f"{question}\n\n{facts}"})

            body = {"model": self.model, "messages": messages, "stream": True,
                    "cache_prompt": True, "max_tokens": VOICE_MAX_TOKENS,
                    "stop": STOP}
            first = True
            pending = ""
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
                    pending += delta
                    # Hold a trailing dash until the next chunk shows whether a
                    # space follows, otherwise spacing is applied wrongly.
                    if pending[-1] in "\u2014\u2013":
                        continue
                    yield {"type": "token", "text": strip_em_dashes(pending)}
                    pending = ""

        if pending:
            yield {"type": "token", "text": strip_em_dashes(pending)}
        tel.total_ms = int((time.monotonic() - started) * 1000)
        yield {"type": "done", "telemetry": tel}

    @staticmethod
    def _render_facts(tel: Telemetry) -> str:
        if not tel.tool_calls:
            return ("[Internal note, never mention this: no tools ran. Answer "
                    "normally from your own knowledge. Say you are unsure only if "
                    "you genuinely are.]")
        if all(c.outcome != "ok" for c in tel.tool_calls):
            names = ", ".join(sorted({c.name for c in tel.tool_calls}))
            return ("[Every lookup failed. Say in one clause that it came back "
                    "with nothing, then stop. Do NOT speculate about why. Do "
                    "NOT describe any results. Do NOT answer from memory as if "
                    f"you had looked it up. Failed: {names}]")
        lines = ["[Results from THIS message only. Earlier turns are gone. "
                 "Use only what is listed here and never mention a result "
                 "that is not below.]"]
        for i, call in enumerate(tel.tool_calls, 1):
            args = json.dumps(call.args, default=str)[:200]
            if call.outcome == "ok":
                lines.append(f"[{i}] {call.name}{args} returned: "
                             f"{json.dumps(call.result, default=str)[:2000]}")
            else:
                lines.append(f"[{i}] {call.name}{args} FAILED: {call.error}. "
                             "Say so in one clause, do not explain why.")
        return "\n".join(lines)


FABRICATION_MARKERS = ("i searched", "i ran a web search", "i looked it up",
                       "i fetched", "according to my search", "i found sources")

# Must match registry names. These were web_search and fetch_url, which stopped
# existing at the verb_noun rename, so this check silently passed on everything
# and would have flagged every real search as a fabrication.
LOOKUP_TOOLS = {"search_web", "read_url", "read_repo"}

# Omission is the dominant small-model tool failure: the model answers in prose
# instead of calling a tool it holds. Detected in code, not hoped away.
REFUSAL = re.compile(
    r"\b(cannot|can't|can not|unable to|don't have access|do not have access|"
    r"no access to)\b[^.]{0,60}\b(retrieve|access|browse|open|search|"
    r"real[- ]time|current|internet|web|live)\b", re.I)


_CITE = re.compile(r"\[(\d+)\]")


def check_citations(text: str, tel: Telemetry) -> list[str]:
    """A citation number the facts block never issued is conflation made
    visible: the model is pointing at a result that is not this turn's."""
    valid = set(range(1, len(tel.tool_calls) + 1))
    bad = {int(n) for n in _CITE.findall(text)} - valid
    return [f"cited [{n}], no such result this turn" for n in sorted(bad)]


def detect_fabrication(text: str, tel: Telemetry) -> list[str]:
    """Claims of tool use that telemetry contradicts.

    The voice pass cannot inject a fake result, but it can still assert one in
    prose. Telemetry is ground truth, so the assertion is checkable.
    """
    ran = {c.name for c in tel.tool_calls if c.outcome == "ok"}
    lowered = text.lower()
    flags = []
    if any(m in lowered for m in FABRICATION_MARKERS) and not (
            LOOKUP_TOOLS & ran):
        flags.append("claimed_search_without_call")
    return flags
