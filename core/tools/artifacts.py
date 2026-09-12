"""Rendered HTML pages the assistant can produce and link to.

Kept deliberately narrow. The model supplies a title and a complete HTML
document; the tool stores it and returns a URL. No templating, no partial
updates, no server-side execution of anything the model wrote.
"""
import hashlib
import re
import time
from pathlib import Path

DIR = Path("data/artifacts")
MAX_BYTES = 400_000

# Scripts run in the viewer's browser, not on the server, and the page is
# served from a sandboxed iframe. These still go, because a stored page is
# reachable by anyone with the link.
_STRIP = re.compile(
    r"<\s*(script|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>"
    r"|<\s*(script|iframe|object|embed)\b[^>]*/?>"
    r"|\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)"
    r"|javascript:",
    re.I | re.S)


def sanitise(html: str) -> tuple[str, int]:
    cleaned, n = _STRIP.subn("", html)
    return cleaned, n


def create_page(title: str, html: str) -> dict:
    if not html.strip():
        raise ValueError("html was empty")
    if len(html.encode()) > MAX_BYTES:
        raise ValueError(f"page too large, limit {MAX_BYTES} bytes")

    cleaned, stripped = sanitise(html)
    if "<html" not in cleaned.lower():
        cleaned = (f"<!doctype html><html><head><meta charset=utf-8>"
                   f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                   f"<title>{title[:120]}</title></head><body>{cleaned}</body></html>")

    DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "page"
    digest = hashlib.sha256(cleaned.encode()).hexdigest()[:8]
    name = f"{slug}-{digest}.html"
    (DIR / name).write_text(cleaned, encoding="utf-8")

    out = {"title": title, "url": f"/artifacts/{name}",
           "bytes": len(cleaned.encode()), "created": time.time()}
    if stripped:
        out["note"] = f"{stripped} script or handler blocks were removed"
    return out
