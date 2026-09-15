import asyncio
import json
import logging
import os
import random
import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import httpx

from core.compact import extract_entities, extract_facts, summarise
from core.documents import DocumentError, extract
from core.harness import (Harness, check_citations, check_computed_after_failure,
                          detect_fabrication)
from core.episodic import Episodic
from core.procedural import Procedural
from core.ratelimit import RateLimiter
from core.memory import Memory
from core.personas import PERSONAS
from core.tools import WEB_TOOLS
from core.tool_router import ToolRouter
from core.tools.memory_tools import build as build_memory_tools
from core.tools.image_tools import build as build_image_tools
from core.tools.csv_tools import build as build_csv_tools
from web import auth

DB = Path(os.getenv("DB_PATH", "data/app.db"))
QUEUE_CAP = int(os.getenv("QUEUE_DEPTH_CAP", "12"))
SLOTS = int(os.getenv("PARALLEL_SLOTS", "2"))
ARTIFACT_TOOLS = {"create_webpage", "edit_webpage", "crop_image", "resize_image",
                  "convert_image_format", "set_image_transparency"}


def _artifact_of(call) -> dict | None:
    """filter_csv only produces a file when return_file was asked for, and
    nests it under "file" since the tool's main return value is the match
    preview, not a file - a different shape than create_webpage/the image
    tools, which are nothing BUT a file. Flattened here so the frontend
    only ever has one artifact shape to render."""
    if call.outcome != "ok":
        return None
    if call.name in ARTIFACT_TOOLS:
        return call.result
    if call.name == "filter_csv" and isinstance(call.result, dict):
        return call.result.get("file")
    return None

log = logging.getLogger("app")


def slot_for(session: str) -> int:
    """Pin a session to one llama-server slot for the request's lifetime.

    Two pass-1 round trips happen per message. Without pinning they can land
    on different slots and each pays full prompt re-evaluation instead of
    hitting the KV cache from the previous call.
    """
    return int(hashlib.sha256(session.encode()).hexdigest(), 16) % SLOTS

harness = Harness(
    os.getenv("LLM_BASE_URL", "http://localhost:8090"),
    os.getenv("LLM_MODEL", "qwen3-8b"),
    WEB_TOOLS,
    max_turns=int(os.getenv("MAX_TURNS", "6")),
)

_slots = asyncio.Semaphore(SLOTS)
_waiting = 0
memory = Memory(lambda: db())
episodic = Episodic(lambda: db())
procedural = Procedural(lambda: db())
ratelimit = RateLimiter()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          id TEXT PRIMARY KEY, persona TEXT, created REAL,
          owner TEXT DEFAULT 'anon', title TEXT);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT,
          ttft_ms INT, total_ms INT, created REAL);
        CREATE TABLE IF NOT EXISTS tool_calls(
          id INTEGER PRIMARY KEY, session TEXT, name TEXT, outcome TEXT,
          retries INT, ms INT, args TEXT, created REAL);
        """)
        # Sessions predate owner/title. CREATE TABLE IF NOT EXISTS skips an
        # existing table, so add the columns explicitly.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "owner" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT DEFAULT 'anon'")
        if "title" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        tc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_calls)")}
        if "args" not in tc_cols:
            conn.execute("ALTER TABLE tool_calls ADD COLUMN args TEXT")
    memory.init()
    episodic.init()
    procedural.init()
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(auth.router)


@app.middleware("http")
async def ensure_uid(request: Request, call_next):
    # Assign before the handler, not after. Setting it only on the response
    # meant the first request created a session owned by "anon" and the
    # second arrived with a real uid, locking the user out of their own thread.
    issued = None
    if not request.cookies.get("uid"):
        issued = "a:" + uuid.uuid4().hex
        request.scope["uid"] = issued
    response = await call_next(request)
    migrate = getattr(request.state, "migrate", None)
    if migrate:
        old, new = migrate
        if old and old != "anon" and old != new:
            with db() as conn:
                conn.execute("UPDATE sessions SET owner=? WHERE owner=?", (new, old))
                conn.execute("UPDATE facts SET owner=? WHERE owner=?", (new, old))
    if issued:
        response.set_cookie("uid", issued, max_age=31_536_000,
                            httponly=True, samesite="lax")
    return response


class Ask(BaseModel):
    session: str | None = None
    persona: str | None = None
    message: str = Field(max_length=4000)
    attachment: str | None = None


# Extracted text lives in memory only. Uploaded bytes are never written to
# disk, so a malicious file leaves nothing behind after parsing.
_attachments: dict[str, object] = {}

# Images and CSVs are held separately from text attachments and are NOT
# popped on first use: a document is read once and its text folded into
# context, but both of these are things tools act on repeatedly across a
# conversation (crop an image then resize the result, filter a CSV then
# query it another way), so they have to survive more than one message.
_images: dict[str, bytes] = {}
_csvs: dict[str, bytes] = {}
IMAGE_KINDS = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CSV_BYTES = 8 * 1024 * 1024


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), request: Request = None):
    allowed, retry_after = ratelimit.check(owner_of(request))
    if not allowed:
        raise HTTPException(429, f"slow down, try again in {retry_after}s",
                            headers={"Retry-After": str(retry_after)})
    data = await file.read()
    content_type = (file.content_type or "").split(";")[0]
    filename = file.filename or "file"
    if content_type in IMAGE_KINDS:
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"image too large, limit "
                                f"{MAX_IMAGE_BYTES // 1024 // 1024}MB")
        key = uuid.uuid4().hex
        _images[key] = data
        if len(_images) > 200:
            for stale in list(_images)[:50]:
                _images.pop(stale, None)
        return {"id": key, "name": filename,
                "kind": IMAGE_KINDS[content_type], "is_image": True,
                "chars": len(data), "pages": 0, "truncated": False}
    # Extension checked first, same as core.documents.kind_for: a CSV saved
    # from Excel commonly arrives as application/vnd.ms-excel or even
    # text/plain depending on OS and browser, so content-type alone misses it.
    is_csv = (filename.lower().endswith(".csv")
             or content_type in ("text/csv", "application/csv"))
    if is_csv:
        if len(data) > MAX_CSV_BYTES:
            raise HTTPException(400, f"file too large, limit "
                                f"{MAX_CSV_BYTES // 1024 // 1024}MB")
        key = uuid.uuid4().hex
        _csvs[key] = data
        if len(_csvs) > 200:
            for stale in list(_csvs)[:50]:
                _csvs.pop(stale, None)
        return {"id": key, "name": filename, "kind": "csv", "is_csv": True,
                "chars": len(data), "pages": 0, "truncated": False}
    try:
        doc = extract(data, filename, file.content_type or "")
    except DocumentError as exc:
        raise HTTPException(400, str(exc))
    key = uuid.uuid4().hex
    _attachments[key] = doc
    if len(_attachments) > 200:
        for stale in list(_attachments)[:50]:
            _attachments.pop(stale, None)
    return {"id": key, "name": doc.name, "kind": doc.kind,
            "pages": doc.pages, "chars": len(doc.text),
            "truncated": doc.truncated}


def owner_of(request: Request) -> str:
    return request.cookies.get("uid") or request.scope.get("uid") or "anon"


@app.get("/api/session/{session}")
def session_detail(session: str, request: Request):
    # Ownership check. Session ids are 128 bit and unguessable, but an id that
    # leaks through a shared link or a log must not hand over the transcript.
    with db() as conn:
        row = conn.execute("SELECT persona FROM sessions WHERE id=? AND owner=?",
                           (session, owner_of(request))).fetchone()
        turns = conn.execute(
            "SELECT role, content FROM turns WHERE session=? ORDER BY id",
            (session,)).fetchall()
    if not row:
        raise HTTPException(404, "no such session")
    return {"persona": row["persona"],
            "messages": [dict(t) for t in turns]}


@app.get("/api/history")
def history(request: Request):
    with db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.persona, s.title, s.created,"
            " (SELECT COUNT(*) FROM turns t WHERE t.session=s.id) n"
            " FROM sessions s WHERE s.owner=? ORDER BY s.created DESC LIMIT 40",
            (owner_of(request),)).fetchall()
    return [dict(r) for r in rows if r["n"]]


@app.post("/api/forget")
def forget(request: Request):
    memory.forget(owner_of(request))
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    owner = owner_of(request)
    with db() as conn:
        facts = conn.execute("SELECT fact FROM facts WHERE owner=?",
                             (owner,)).fetchall()
    return {"owner": owner, "signed_in": owner.startswith("g:"),
            "email": request.cookies.get("email", ""),
            "google_available": auth.enabled(),
            "facts": [f["fact"] for f in facts]}


@app.get("/api/personas")
def personas():
    return [{"key": p.key, "name": p.name, "tagline": p.tagline}
            for p in PERSONAS.values()]


@app.get("/api/stats")
def stats():
    with db() as conn:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) c FROM tool_calls GROUP BY outcome").fetchall()
    total = sum(r["c"] for r in rows) or 1
    bad = sum(r["c"] for r in rows if r["outcome"] != "ok")
    return {"tool_calls": sum(r["c"] for r in rows),
            "failure_rate": round(100 * bad / total, 1),
            "queue_waiting": _waiting}


@app.post("/api/chat")
async def chat(ask: Ask, request: Request):
    global _waiting
    owner = owner_of(request)
    # Per-owner, not global. The queue cap and slot semaphore stop the whole
    # server from being overwhelmed, but did nothing to stop one visitor from
    # occupying both GPU slots back to back with rapid requests.
    allowed, retry_after = ratelimit.check(owner)
    if not allowed:
        raise HTTPException(429, f"slow down, try again in {retry_after}s",
                            headers={"Retry-After": str(retry_after)})
    if _waiting >= QUEUE_CAP:
        raise HTTPException(503, "queue full, try in a minute")

    session = ask.session or uuid.uuid4().hex
    switched_from = None

    with db() as conn:
        row = conn.execute("SELECT persona, owner FROM sessions WHERE id=?",
                           (session,)).fetchone()
        if row and row["owner"] != owner:
            raise HTTPException(403, "not your conversation")
        if row:
            persona = row["persona"]
            # Switching mid-conversation keeps the context. The new persona
            # inherits the thread and knows who it took over from.
            if ask.persona and ask.persona in PERSONAS and ask.persona != persona:
                switched_from, persona = persona, ask.persona
                conn.execute("UPDATE sessions SET persona=? WHERE id=?",
                             (persona, session))
        else:
            persona = ask.persona if ask.persona in PERSONAS else random.choice(
                list(PERSONAS))
            conn.execute(
                "INSERT INTO sessions(id,persona,created,owner,title)"
                " VALUES(?,?,?,?,?)",
                (session, persona, time.time(), owner, ask.message[:60]))

    ctx = memory.load(session, owner)
    history = ctx.as_messages()
    doc = _attachments.pop(ask.attachment, None) if ask.attachment else None
    if doc:
        history = history + [{"role": "system", "content": doc.as_context()}]
    # Images and CSVs are not popped here (see _images/_csvs comment above):
    # the tool calls, not this handler, are what consume them, and they may
    # be referenced again in a later message in the same conversation.
    force_tools: list[str] = []
    if ask.attachment and ask.attachment in _images:
        history = history + [{"role": "system", "content":
            f"The user has attached an image, id {ask.attachment!r}. Use "
            "crop_image, resize_image, convert_image_format, or "
            "set_image_transparency with this id if they ask you to edit "
            "it, now or in a later message in this conversation."}]
        force_tools += ["crop_image", "resize_image",
                        "convert_image_format", "set_image_transparency"]
    if ask.attachment and ask.attachment in _csvs:
        history = history + [{"role": "system", "content":
            f"The user has attached a CSV, id {ask.attachment!r}. Call "
            "summarize_csv with this id FIRST to see the real column "
            "names before filtering or aggregating, now or in a later "
            "message in this conversation."}]
        force_tools += ["summarize_csv", "filter_csv", "query_csv"]
    question = ask.message
    if switched_from:
        history.append({"role": "system", "content":
            f"You just took over this conversation from {PERSONAS[switched_from].name}. "
            f"You may acknowledge that once, briefly, in your own voice."})

    async def events():
        global _waiting
        _waiting += 1
        position = _waiting
        try:
            if position > SLOTS:
                yield _sse({"type": "queued", "position": position - SLOTS})
            async with _slots:
                yield _sse({"type": "start", "session": session,
                            "persona": persona, "switched_from": switched_from})
                # Memory tools are bound to this owner and session, so the
                # model cannot reach another user's history.
                harness.registry = {
                    **WEB_TOOLS,
                    **build_memory_tools(memory, session, owner),
                    **build_image_tools(_images.get),
                    **build_csv_tools(_csvs.get)}
                harness.router = ToolRouter(harness.registry)
                harness.episodic = episodic
                harness.procedural = procedural
                text = ""
                async for ev in harness.answer(question, persona,
                                               history=history,
                                               slot_id=slot_for(session),
                                               session_id=session,
                                               force_tools=force_tools):
                    if ev["type"] == "token":
                        text += ev["text"]
                        yield _sse(ev)
                    elif ev["type"] == "status":
                        yield _sse(ev)
                    else:
                        tel = ev["telemetry"]
                        flags = (detect_fabrication(text, tel) +
                                check_citations(text, tel) +
                                check_computed_after_failure(text, tel, question))
                        stored = (f"[attached {doc.name}] {ask.message}"
                                  if doc else ask.message)
                        memory.add_turn(session, "user", stored)
                        for c in tel.tool_calls:
                            q = json.dumps(c.args, default=str)[:120]
                            # Carry the actual result, not just ok/fail. This
                            # line is what _recap surfaces to later turns, so
                            # without the value a fact like today's date was
                            # recorded as "ok" with nothing usable in it, and
                            # a follow-up question had nothing to reason from.
                            out = (json.dumps(c.result, default=str)[:200]
                                  if c.outcome == "ok"
                                  else f"{c.outcome}: {str(c.error)[:80]}")
                            memory.add_turn(session, "assistant",
                                            f"[tool] {c.name}{q} -> {out}")
                        memory.add_turn(session, "assistant", text)
                        _persist(session, ask.message, text, tel)
                        yield _sse({"type": "done", "telemetry": {
                            "model": tel.model, "ttft_ms": tel.ttft_ms,
                            "total_ms": tel.total_ms, "turns": tel.turns,
                            "flags": flags,
                            "offered": tel.tools_offered,
                            "trace": tel.trace,
                            "context": {"recent": len(ctx.recent),
                                        "entities": len(ctx.entities),
                                        "facts": len(ctx.long),
                                        "summary": bool(ctx.short)},
                            "artifacts": [a for c in tel.tool_calls
                                          if (a := _artifact_of(c))],
                            "tools": [{"name": c.name, "outcome": c.outcome,
                                       "retries": c.retries, "ms": c.ms}
                                      for c in tel.tool_calls]}})
                        asyncio.create_task(_maintain(session, owner))
        except Exception:
            log.exception("chat stream failed (session=%s)", session)
            yield _sse({"type": "error", "message": "something broke on my end"})
        finally:
            _waiting -= 1

    return StreamingResponse(events(), media_type="text/event-stream")


async def _maintain(session: str, owner: str) -> None:
    """Compaction and fact extraction, after the reply has been sent."""
    try:
        pending = memory.pending(session)
        async with httpx.AsyncClient() as client:
            base, model = harness.base_url, harness.model
            if pending:
                ctx = memory.load(session, owner)
                summary = await summarise(client, base, model, pending, ctx.short)
                # User turns only. Extracting from assistant prose pulled last
                # turn's search results into a block labelled "keep these
                # exact", which re-served them on every later turn.
                user_only = [t for t in pending if t["role"] == "user"]
                entities = (await extract_entities(client, base, model, user_only)
                            if user_only else [])
                if summary:
                    memory.apply_compaction(session, [t["id"] for t in pending],
                                            summary, entities)
            with db() as conn:
                n = conn.execute("SELECT COUNT(*) c FROM turns WHERE session=?",
                                 (session,)).fetchone()["c"]
            if n and n % 6 == 0:
                recent = memory.load(session, owner).recent
                user_only = [t for t in recent if t["role"] == "user"]
                if user_only:
                    facts = await extract_facts(client, base, model, user_only)
                    memory.remember(owner, facts, session)
    except Exception:
        pass  # maintenance is best-effort; never breaks a reply


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _persist(session: str, question: str, answer: str, tel) -> None:
    now = time.time()
    with db() as conn:
        conn.execute("INSERT INTO messages(session,role,content,created) VALUES(?,?,?,?)",
                     (session, "user", question, now))
        conn.execute(
            "INSERT INTO messages(session,role,content,ttft_ms,total_ms,created)"
            " VALUES(?,?,?,?,?,?)",
            (session, "assistant", answer, tel.ttft_ms, tel.total_ms, now))
        conn.executemany(
            "INSERT INTO tool_calls(session,name,outcome,retries,ms,args,created)"
            " VALUES(?,?,?,?,?,?,?)",
            [(session, c.name, c.outcome, c.retries, c.ms,
              json.dumps(c.args, default=str)[:500], now) for c in tel.tool_calls])


ARTIFACTS = Path("data/artifacts")
ARTIFACT_MEDIA_TYPES = {"html": "text/html", "png": "image/png",
                        "jpg": "image/jpeg", "webp": "image/webp",
                        "csv": "text/csv"}


@app.get("/artifacts/{name}")
def artifact(name: str):
    m = re.fullmatch(r"([a-z0-9-]+)\.(html|png|jpg|webp|csv)", name)
    if not m:
        raise HTTPException(404, "no")
    ext = m.group(2)
    path = ARTIFACTS / name
    if not path.is_file():
        raise HTTPException(404, "no such page")
    if ext == "html":
        # Model-authored HTML. Sandboxed at write time and served with a
        # restrictive CSP, no same-origin access to the app.
        return FileResponse(path, media_type="text/html", headers={
            "Content-Security-Policy":
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "font-src data:; sandbox allow-popups",
            "X-Content-Type-Options": "nosniff"})
    # Images have no script context to sandbox, but keep them from being
    # framed or sniffed as something else regardless.
    return FileResponse(path, media_type=ARTIFACT_MEDIA_TYPES[ext], headers={
        "Content-Security-Policy": "default-src 'none'",
        "X-Content-Type-Options": "nosniff"})


static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static), name="static")


@app.get("/")
def index():
    return FileResponse(static / "index.html")
