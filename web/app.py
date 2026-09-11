import asyncio
import json
import os
import random
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.harness import Harness, detect_fabrication
from core.personas import PERSONAS
from core.tools import WEB_TOOLS

DB = Path(os.getenv("DB_PATH", "data/app.db"))
QUEUE_CAP = int(os.getenv("QUEUE_DEPTH_CAP", "12"))
SLOTS = int(os.getenv("PARALLEL_SLOTS", "2"))

harness = Harness(
    os.getenv("LLM_BASE_URL", "http://localhost:8080"),
    os.getenv("LLM_MODEL", "qwen3-8b"),
    WEB_TOOLS,
    max_turns=int(os.getenv("MAX_TURNS", "6")),
)

_slots = asyncio.Semaphore(SLOTS)
_waiting = 0


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
          id TEXT PRIMARY KEY, persona TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT,
          ttft_ms INT, total_ms INT, created REAL);
        CREATE TABLE IF NOT EXISTS tool_calls(
          id INTEGER PRIMARY KEY, session TEXT, name TEXT, outcome TEXT,
          retries INT, ms INT, created REAL);
        """)
    yield


app = FastAPI(lifespan=lifespan)


class Ask(BaseModel):
    session: str | None = None
    persona: str | None = None
    message: str = Field(max_length=4000)


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
async def chat(ask: Ask):
    global _waiting
    if _waiting >= QUEUE_CAP:
        raise HTTPException(503, "queue full, try in a minute")

    session = ask.session or uuid.uuid4().hex
    persona = ask.persona
    with db() as conn:
        row = conn.execute("SELECT persona FROM sessions WHERE id=?",
                           (session,)).fetchone()
        if row:
            persona = row["persona"]
        else:
            persona = persona if persona in PERSONAS else random.choice(list(PERSONAS))
            conn.execute("INSERT INTO sessions VALUES(?,?,?)",
                         (session, persona, time.time()))

    async def events():
        global _waiting
        _waiting += 1
        position = _waiting
        try:
            if position > SLOTS:
                yield _sse({"type": "queued", "position": position - SLOTS})
            async with _slots:
                yield _sse({"type": "start", "session": session, "persona": persona})
                text = ""
                async for ev in harness.answer(ask.message, persona):
                    if ev["type"] == "token":
                        text += ev["text"]
                        yield _sse(ev)
                    else:
                        tel = ev["telemetry"]
                        flags = detect_fabrication(text, tel)
                        _persist(session, ask.message, text, tel)
                        yield _sse({"type": "done", "telemetry": {
                            "model": tel.model, "ttft_ms": tel.ttft_ms,
                            "total_ms": tel.total_ms, "turns": tel.turns,
                            "flags": flags,
                            "tools": [{"name": c.name, "outcome": c.outcome,
                                       "retries": c.retries, "ms": c.ms}
                                      for c in tel.tool_calls]}})
        except Exception:
            yield _sse({"type": "error", "message": "something broke on my end"})
        finally:
            _waiting -= 1

    return StreamingResponse(events(), media_type="text/event-stream")


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
            "INSERT INTO tool_calls(session,name,outcome,retries,ms,created)"
            " VALUES(?,?,?,?,?,?)",
            [(session, c.name, c.outcome, c.retries, c.ms, now) for c in tel.tool_calls])


static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static), name="static")


@app.get("/")
def index():
    return FileResponse(static / "index.html")
