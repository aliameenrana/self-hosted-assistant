"""Model-driven compaction and fact extraction.

Both run on a plain prompt with no persona. Compaction output feeds back into
context, so a persona voice here would contaminate every later turn.
"""
import json
import re

import httpx

from .memory import SUMMARY_MAX_TOKENS

SUMMARY_PROMPT = """/no_think Summarise this conversation extract for your own \
later reference. Keep concrete details: names, numbers, decisions, preferences, \
unresolved questions. Drop pleasantries. Write plain prose, no bullet points, \
under 120 words. If an earlier summary is given, merge it with the new turns \
into one summary rather than appending."""

ENTITY_PROMPT = """/no_think List the specific named things in this conversation \
that would be wrong to forget: proper nouns, product and tool names, version \
numbers, file paths, quantities, dates, decisions made.

Summaries lose these first, so they are stored separately and verbatim.

Reply with a JSON array of short strings, each a single specific item. No \
sentences, no explanation, no other text."""

FACT_PROMPT = """/no_think Read the conversation and list durable facts about the \
user that would still matter in a different conversation next week.

Include: their name, job, stack, tools, ongoing projects, stated preferences.
Exclude: anything about the current question, one-off requests, anything you \
inferred rather than were told.

Base this ONLY on what the user said. Never turn your own replies into facts \
about them.

Reply with a JSON array of short strings. Empty array if nothing qualifies. \
No other text."""


async def summarise(client: httpx.AsyncClient, base_url: str, model: str,
                    turns: list[dict], previous: str = "") -> str:
    body = "\n".join(f"{t['role']}: {t['content'][:600]}" for t in turns)
    user = f"Earlier summary:\n{previous}\n\nNew turns:\n{body}" if previous else body
    resp = await client.post(
        f"{base_url}/v1/chat/completions",
        json={"model": model, "max_tokens": SUMMARY_MAX_TOKENS, "cache_prompt": True,
              "messages": [{"role": "system", "content": SUMMARY_PROMPT},
                           {"role": "user", "content": user}]},
        timeout=120.0)
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()


async def extract_entities(client: httpx.AsyncClient, base_url: str, model: str,
                           turns: list[dict]) -> list[str]:
    body = "\n".join(f"{t['role']}: {t['content'][:600]}" for t in turns)
    resp = await client.post(
        f"{base_url}/v1/chat/completions",
        json={"model": model, "max_tokens": 250, "cache_prompt": True,
              "messages": [{"role": "system", "content": ENTITY_PROMPT},
                           {"role": "user", "content": body}]},
        timeout=120.0)
    resp.raise_for_status()
    return _parse_list((resp.json()["choices"][0]["message"]["content"] or "").strip())


async def extract_facts(client: httpx.AsyncClient, base_url: str, model: str,
                        turns: list[dict]) -> list[str]:
    body = "\n".join(f"{t['role']}: {t['content'][:600]}" for t in turns)
    resp = await client.post(
        f"{base_url}/v1/chat/completions",
        json={"model": model, "max_tokens": 300, "cache_prompt": True,
              "messages": [{"role": "system", "content": FACT_PROMPT},
                           {"role": "user", "content": body}]},
        timeout=120.0)
    resp.raise_for_status()
    text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    return _parse_list(text)


def _parse_list(text: str) -> list[str]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [str(i).strip() for i in items
            if isinstance(i, (str, int, float)) and str(i).strip()][:10]
