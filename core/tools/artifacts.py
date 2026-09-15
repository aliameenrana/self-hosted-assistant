"""Rendered HTML pages the assistant can produce, link to, and edit.

Kept deliberately narrow. The model supplies a title and a complete HTML
document; the tool stores it and returns a URL. No templating, no
server-side execution of anything the model wrote.

Pages get a random id at creation, not a content hash: a hash meant a page
could only ever be a brand new, unrelated file, so "now make the header
blue" after building a page had no way to target what was already built,
only create a second disconnected one. The id is stable for the page's
whole life; edit_page overwrites the same file rather than making a new
one, which is what makes iterating on a page actually work.
"""
import re
import secrets
import time
from pathlib import Path

from .errors import ToolError

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


def _finalise(title: str, html: str) -> tuple[str, int]:
    if not html.strip():
        raise ToolError("html was empty")
    if len(html.encode()) > MAX_BYTES:
        raise ToolError(f"page too large, limit {MAX_BYTES} bytes")
    cleaned, stripped = sanitise(html)
    if "<html" not in cleaned.lower():
        cleaned = (f"<!doctype html><html><head><meta charset=utf-8>"
                   f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                   f"<title>{title[:120]}</title></head><body>{cleaned}</body></html>")
    return cleaned, stripped


def create_page(title: str, html: str) -> dict:
    cleaned, stripped = _finalise(title, html)
    DIR.mkdir(parents=True, exist_ok=True)
    page_id = secrets.token_hex(8)
    name = f"{page_id}.html"
    (DIR / name).write_text(cleaned, encoding="utf-8")

    out = {"page_id": page_id, "title": title, "url": f"/artifacts/{name}",
           "bytes": len(cleaned.encode()), "created": time.time()}
    if stripped:
        out["note"] = f"{stripped} script or handler blocks were removed"
    return out


def edit_page(page_id: str, title: str, html: str) -> dict:
    """Overwrite an existing page in place. page_id must be exactly what
    create_page (or a prior edit_page) returned - not guessed, not derived
    from a URL, so this can never be used to write to an id that was never
    actually allocated by this tool."""
    if not re.fullmatch(r"[0-9a-f]{16}", page_id):
        raise ToolError("not a page id this tool ever issued")
    path = DIR / f"{page_id}.html"
    if not path.is_file():
        raise ToolError(f"no page with id {page_id!r}. It may have expired "
                         "or never existed - create a new one instead.")
    cleaned, stripped = _finalise(title, html)
    path.write_text(cleaned, encoding="utf-8")

    out = {"page_id": page_id, "title": title, "url": f"/artifacts/{page_id}.html",
           "bytes": len(cleaned.encode()), "created": time.time(), "edited": True}
    if stripped:
        out["note"] = f"{stripped} script or handler blocks were removed"
    return out
