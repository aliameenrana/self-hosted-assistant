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
    """Topics only, never prior assistant PROSE.

    Splicing raw history into the voice pass let it read last turn's rendered
    results as its own confident text and re-serve them for this turn. That is
    where the Pixar film appeared in a pizza search.

    [tool] lines are the one exception, carried through verbatim. They are
    structured facts the harness wrote, not the model's own narration, so the
    conflation risk above does not apply to them. Excluding them alongside
    prose meant a fact fetched in an earlier turn, like the current date,
    became invisible to every later turn in the same conversation: asked
    "what is the date" then "is June in the past", the second question had
    no access to the first answer at all and the model invented a year.
    """
    asks = [m["content"].strip().replace("\n", " ")[:90]
            for m in history if m.get("role") == "user"][-4:]
    tool_facts = [m["content"] for m in history
                 if m.get("role") == "assistant"
                 and m["content"].startswith("[tool]")][-6:]
    system = [m["content"] for m in history if m.get("role") == "system"]
    parts = []
    if asks:
        parts.append("Earlier in this conversation the user asked about: "
                     + "; ".join(asks))
    if tool_facts:
        parts.append("Facts already established earlier in this conversation, "
                     "still true now unless the user says otherwise:\n"
                     + "\n".join(tool_facts))
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
        self.episodic = None
        self.procedural = None


    async def _chat(self, client: httpx.AsyncClient, messages: list[dict],
                    tools: list[dict] | None = None, stream: bool = False,
                    max_tokens: int | None = None, force: bool = False,
                    slot_id: int | None = None) -> Any:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "stream": stream, "cache_prompt": True}
        if slot_id is not None:
            body["id_slot"] = slot_id
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
                         history: list[dict], tel: Telemetry,
                         slot_id: int | None = None, on_status=None,
                         session_id: str | None = None) -> list[dict]:
        """Pass 1. Characterless. Returns verified tool results only."""
        messages = [{"role": "system", "content": TOOL_PROMPT}, *history,
                    {"role": "user", "content": question}]
        # Offer only the tools relevant to this message. Router accuracy drops
        # once a small model sees more than about eight at once.
        offered = self.registry
        if self.router:
            offered, tel.tools_offered = self.router.select(question, self.registry)
        if self.procedural:
            recalled = self.procedural.recall(question)
            if recalled:
                extra = {n: self.registry[n] for n in recalled
                         if n in self.registry and n not in offered}
                if extra:
                    offered = {**offered, **extra}
                    tel.tools_offered = list(offered)
                    tel.trace.append(f"procedural: past pattern suggests "
                                     f"{', '.join(recalled)} together")
        schemas = [t.schema() for t in offered.values()]

        # A cheap, cheerless nudge if a similarly worded query already failed
        # on one of the tools being offered. No model call, no vector store,
        # just a lookup against a small table of past failures.
        if self.episodic:
            for name in offered:
                warning = self.episodic.warn(name, question)
                if warning:
                    messages.append({"role": "system", "content":
                                     f"Heads up before you decide: {warning}"})
                    tel.trace.append(f"episodic: {warning}")
                    break  # one nudge is enough, more is noise
        last_signature = None
        attempts: dict[str, int] = {}
        forced = False
        full_retry = False

        for turn in range(self.max_turns):
            tel.turns = turn + 1
            budget = max([t.budget for t in offered.values()] or
                          [self.decide_tokens])
            if tel.tool_calls:
                # A tool already succeeded. On a single sequential GPU an
                # extra confirmation round trip costs more than the tokens it
                # saves, so chaining is only offered when the FIRST call
                # itself signalled there might be more to do: it errored, or
                # more than one tool was relevant enough to be offered in the
                # first place. Otherwise treat one success as the answer and
                # move straight to the voice pass. Measured: this cut a
                # search+voice request from four LLM round trips to two.
                last_ok = tel.tool_calls[-1].outcome == "ok"
                # Widened after a real miss: "search for the score, then
                # multiply the winner's score by 1000" matched none of the
                # original phrasings, so the model never got a second turn
                # to call calculate and did the multiplication itself in the
                # voice pass instead, exactly the fabrication this harness
                # exists to prevent.
                #
                # First widening was too broad: "what is 4871 times 392" now
                # matched on "times" even though calculate already answered
                # it fully in one step, which kept the loop open for no
                # reason and made a purely single-step question intermittently
                # slower and less predictable. The arithmetic-operation words
                # are only real chaining signal when the tool that already
                # ran was NOT itself a compute tool: "search, then multiply"
                # means the multiply is still owed, "what is X times Y"
                # answered by calculate means it already happened.
                sequencing = bool(re.search(
                    r"\b(and (also |then )?(tell|check|find|get|what|when|"
                    r"how|convert|search))\b|\?.*\?"
                    r"|\b(then|after that|next|once you|and then)\b",
                    question, re.I))
                last_was_compute = tel.tool_calls[-1].name in COMPUTE_TOOLS
                arithmetic_owed = (not last_was_compute and bool(re.search(
                    r"\b(multiply|divide|add|subtract|percent(age)?|times|"
                    r"convert|calculate)\b", question, re.I)))
                multi_part = sequencing or arithmetic_owed
                if last_ok and not multi_part and len(tel.tool_calls) == 1:
                    tel.trace.append(f"turn {turn + 1}: one tool answered a "
                                     "single-part question, skipping ahead")
                    return messages
                budget = max(budget, self.continuation_tokens)
            data = await self._chat(client, messages, tools=schemas,
                                    max_tokens=budget, slot_id=slot_id)
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
                # The router is a lexical heuristic, not a guarantee (measured
                # 88% top-1, not 100%), and when it under-offers, the model
                # previously had no way to say so, it could only pick from a
                # wrong menu or silently answer without a tool. NEED_OTHER_TOOL
                # is that escape hatch: the model asks for the full registry
                # instead of guessing. Costs one extra round trip only on the
                # rare miss, not on every turn.
                if (self.router and not full_retry
                        and said.strip().upper().startswith("NEED_OTHER_TOOL")):
                    full_retry = True
                    all_schemas = [t.schema() for t in self.registry.values()]
                    tel.trace.append(
                        f"turn {turn + 1}: router's offer did not fit, "
                        "retrying with the full tool list")
                    data = await self._chat(client, messages, tools=all_schemas,
                                            max_tokens=budget, slot_id=slot_id)
                    msg = data["choices"][0]["message"]
                    calls = msg.get("tool_calls") or []
                    said = (msg.get("content") or "").strip()
                    if calls:
                        schemas = all_schemas
                        offered = self.registry
                # Omission is the dominant failure for models this size: it
                # answers in prose while holding the tool. Force the call once
                # rather than accept a refusal.
                if schemas and not forced and REFUSAL.search(said):
                    forced = True
                    tel.trace.append(
                        f"turn {turn + 1}: refused in prose, forcing the tool")
                    data = await self._chat(client, messages, tools=schemas,
                                            max_tokens=budget, force=True,
                                            slot_id=slot_id)
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
                if on_status:
                    await on_status(name)
                record = await self._run_tool(call, tel, session_id)
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

    async def _run_tool(self, call: dict, tel: Telemetry,
                        session_id: str | None = None) -> dict:
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

        # Prefer the longest string argument. A dict's first value is not
        # reliably the meaningful one - convert_units puts a number first,
        # so "5" was being recorded as the episodic query instead of the
        # actual units, making the warning useless.
        strings = [v for v in args.values() if isinstance(v, str)] if args else []
        query_arg = max(strings, key=len, default="")
        if not query_arg and args:
            query_arg = " ".join(str(v) for v in args.values())
        try:
            result = execute(self.registry, name, args)
        except ToolError as exc:
            tel.tool_calls.append(ToolCall(name, args, "bad_args", error=str(exc),
                                           ms=elapsed()))
            if self.episodic:
                self.episodic.record_failure(name, str(query_arg), "bad_args",
                                             session_id or "")
            return {"failure": "bad_args", "error": str(exc)}
        except Exception as exc:
            tel.tool_calls.append(ToolCall(name, args, "tool_error",
                                           error=type(exc).__name__, ms=elapsed()))
            return {"failure": "tool_error",
                    "error": f"{name} failed: {type(exc).__name__}"}

        if _is_empty(result):
            tel.tool_calls.append(ToolCall(name, args, "empty", result=result,
                                           ms=elapsed()))
            if self.episodic:
                self.episodic.record_failure(name, str(query_arg), "returned "
                                             "nothing", session_id or "")
            return {"failure": "empty", "result": result,
                    "error": f"{name} returned nothing useful"}

        if name in RELEVANCE_CHECKED:
            query_text = next(iter(args.values()), "") if args else ""
            score = _relevance(str(query_text), result)
            if score == 0:
                tel.tool_calls.append(ToolCall(name, args, "irrelevant",
                                               result=result, ms=elapsed()))
                if self.episodic:
                    self.episodic.record_failure(name, str(query_arg),
                                                 "returned off-topic results",
                                                 session_id or "")
                return {"failure": "irrelevant", "result": result,
                        "error": f"{name} returned results with no overlap "
                                 "with the query, likely off-topic"}

        tel.tool_calls.append(ToolCall(name, args, "ok", result=result, ms=elapsed()))
        return {"result": result}

    async def answer(self, question: str, persona: str,
                     history: list[dict] | None = None,
                     slot_id: int | None = None,
                     session_id: str | None = None) -> AsyncIterator[dict]:
        import asyncio
        status_queue: asyncio.Queue = asyncio.Queue()

        async def on_status(tool_name: str) -> None:
            await status_queue.put(tool_name)

        if persona not in PERSONAS:
            raise ValueError(f"unknown persona: {persona}")
        tel = Telemetry(model=self.model)
        started = time.monotonic()

        async with httpx.AsyncClient() as client:
            tool_task = asyncio.create_task(
                self._tool_pass(client, question, history or [], tel, slot_id,
                                on_status, session_id))
            while not tool_task.done():
                get_status = asyncio.ensure_future(status_queue.get())
                done, _ = await asyncio.wait(
                    {tool_task, get_status}, return_when=asyncio.FIRST_COMPLETED)
                if get_status in done:
                    yield {"type": "status", "tool": get_status.result()}
                else:
                    get_status.cancel()
            await tool_task

            if self.procedural:
                ok_tools = [c.name for c in tel.tool_calls if c.outcome == "ok"]
                if len(set(ok_tools)) >= 2:
                    self.procedural.record_success(question, ok_tools)

            tel.trace.append(f"voice pass: {len(tel.tool_calls)} verified "
                             f"result(s) handed to the writer")
            recap = _recap(history or [])
            facts = self._render_facts(tel, has_recap=bool(recap))
            messages = [{"role": "system", "content": voice_prompt(persona)}]
            if recap:
                messages.append({"role": "system", "content": recap})
            messages.append({"role": "user", "content": f"{question}\n\n{facts}"})

            body = {"model": self.model, "messages": messages, "stream": True,
                    "cache_prompt": True, "max_tokens": VOICE_MAX_TOKENS,
                    "stop": STOP}
            if slot_id is not None:
                body["id_slot"] = slot_id
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
    def _render_facts(tel: Telemetry, has_recap: bool = False) -> str:
        if not tel.tool_calls:
            # This note used to say flatly "no tools ran, answer from your own
            # knowledge" regardless of earlier turns. Found live: a follow-up
            # question needing the date fetched last turn got this note, and
            # the model appeared to read "no tools ran" as "you have no
            # information at all" and override the recap sitting right above
            # it, inverting a date comparison it got right in isolation. The
            # note is now honest about which case applies.
            if has_recap:
                return ("[Internal note, never mention this: no tool ran on "
                        "THIS message, but facts from earlier turns are given "
                        "above in the recap. Use those if they answer the "
                        "question. Only fall back to your own knowledge for "
                        "anything the recap does not cover.]")
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
                # The all-failed branch above had a strong instruction not to
                # compute a substitute answer from memory. A single failed
                # call mixed with successes had only "say so in one clause",
                # which is weaker, and the model used it as license to
                # silently compute the failed conversion itself: asked to
                # convert litres to gallons, the tool errored on the unit
                # name, and the voice pass answered "approximately 0.787
                # gallons", tied to no tool result at all. The
                # instruction is now identical in strength regardless of how
                # many other calls succeeded.
                lines.append(f"[{i}] {call.name}{args} FAILED: {call.error}. "
                             "Say so in one clause. Do NOT compute or guess "
                             "this value yourself from memory. Do NOT present "
                             "any number for this as if it came from the tool.")
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


# calculate and convert_units are the computational counterparts of
# search_web/read_url: when they fail, the honest reply states the failure
# and stops, it never substitutes a number. A model that ignores this is
# indistinguishable from one that ignores "I searched" for lookups, so it
# gets the same treatment: a deterministic check, not a hope that the prompt
# held. Found live: convert_units errored on "litres" vs "l", and the voice
# pass answered "approximately 0.787 gallons" anyway, a plausible-looking
# number computed from memory with no tool result behind it at all.
COMPUTE_TOOLS = {"calculate", "convert_units"}
_DIGITS = re.compile(r"\d")


# An explicit arithmetic operation named in the question, not just any
# digit. "what year did X happen" has digits and needs no tool; "multiply
# the score by 1000" names an operation the harness should have verified.
# Found live: "search for the score, then multiply the winner's score by
# 1000" never called calculate at all, the multi_part heuristic that is
# supposed to keep the loop open for a second tool call did not match this
# phrasing, and the voice pass did "29 x 1000 = 29,000" itself with no tool
# behind it, correct this time, but exactly the pattern that produced the
# wrong litres/gallons number earlier.
_ARITHMETIC_OP = re.compile(
    r"\b(multiply|divide|add|subtract|percent(age)?|times|convert)\b", re.I)


def check_computed_after_failure(text: str, tel: Telemetry,
                                 question: str = "") -> list[str]:
    ran = {c.name for c in tel.tool_calls if c.name in COMPUTE_TOOLS}
    failed = {c.name for c in tel.tool_calls
             if c.name in COMPUTE_TOOLS and c.outcome != "ok"}
    succeeded = {c.name for c in tel.tool_calls
                if c.name in COMPUTE_TOOLS and c.outcome == "ok"}

    # Case 1: a compute tool was tried and failed, nothing succeeded to back
    # a number, but the reply contains one anyway.
    if failed and not succeeded and _DIGITS.search(text):
        return [f"a number appears in the reply but {', '.join(sorted(failed))} "
                f"failed and returned none"]

    # Case 2: the question names an arithmetic operation, no compute tool
    # ran AT ALL, and the reply computed one anyway.
    if (not ran and question and _ARITHMETIC_OP.search(question)
            and _DIGITS.search(text)):
        return ["the question named an arithmetic operation but no "
                "calculate or convert_units call was made, the number in "
                "the reply was not verified by a tool"]
    return []


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
