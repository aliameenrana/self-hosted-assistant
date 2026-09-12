"""Memory exposed as tools rather than always-on context.

Retrieval on demand keeps the prompt small and makes recall visible in the
glass box: the model has to call for something, so a claim of remembering is
backed by a logged tool call.

Bound at request time to one owner and one session, so the model cannot reach
another user's history no matter what it emits.
"""
from typing import Any


def build(memory, session: str, owner: str) -> dict:
    from .registry import Tool, _obj

    def search_memory(query: str) -> dict[str, Any]:
        hits = memory.search(session, query, limit=5)
        if not hits:
            return {"query": query, "found": 0,
                    "note": "nothing in this conversation matches"}
        return {"query": query, "found": len(hits),
                "results": [{"role": h["role"], "text": h["content"]}
                            for h in hits]}

    def remember_fact(fact: str) -> dict[str, Any]:
        added = memory.remember(owner, [fact], session)
        return {"fact": fact,
                "stored": bool(added),
                "note": "already known" if not added else "stored"}

    return {t.name: t for t in [
        Tool("search_memory",
             "Search everything said earlier in this conversation, including "
             "parts that have scrolled out of view. Use when the user refers "
             "to something from before that you cannot see, or asks what they "
             "said earlier. Do not use for the last few messages, which you "
             "already have.",
             _obj({"query": {"type": "string",
                             "description": "Keywords to look for."}},
                  ["query"]), search_memory),
        Tool("remember_fact",
             "Store one durable fact about the user for future conversations. "
             "Use when they state something lasting: their name, their stack, "
             "a preference, a project they are working on. Do not use for "
             "passing details about the current question.",
             _obj({"fact": {"type": "string",
                            "description": "One short sentence about the user."}},
                  ["fact"]), remember_fact),
    ]}
