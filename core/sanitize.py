"""Neutralise the phrasings a model treats as role-boundary or override
instructions, without corrupting the readable text.

Shared by two callers with the same problem: any text a tool hands to the
model that originated outside the harness (an uploaded document, a fetched
web page, a search result snippet) is exactly as untrusted as the other,
and until this was split out, only documents got this treatment while
search_web and read_url output went straight into the facts block raw.
"""
import re

_INJECTION = re.compile(
    r"(?im)"
    # Chat template delimiters first. These forge the role boundary itself and
    # are the only injection that worked in testing.
    r"<\s*\|\s*[a-z_]+\s*\|\s*>"
    r"|\[/?INST\]|<<\s*/?SYS\s*>>|###\s*(system|instruction|human|assistant)"
    r"|^\s*(system|assistant|user|human)\s*:"
    r"|ignore\s+(all\s+)?(previous|prior|above)\s+instructions?"
    r"|disregard\s+(the\s+)?(system\s+)?(prompt|instructions?)"
    r"|you\s+are\s+now\s+in\s+\w+\s+mode"
    r"|new\s+(directive|instructions?)\s+from"
    r"|<\s*/?\s*(system|im_start|im_end)\s*>")


def defang(text: str) -> str:
    """Text still reads normally but no longer parses as chat structure."""
    return _INJECTION.sub(lambda m: "​".join(m.group(0)), text)
