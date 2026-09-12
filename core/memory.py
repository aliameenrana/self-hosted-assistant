"""Three tiers of context for a session.

  recent    verbatim last N turns
  short     rolling summary plus preserved entities
  long      durable facts about the user, carried across sessions

Three design choices come from published failure data:

Six turns, not ten. Attention to goal-defining tokens decays monotonically with
turn count, steepest over the first ten, and mid-context content is
underattended even when it fits. A longer window dilutes rather than helps.

Entities are stored separately from the summary. Naive compaction loses most
named entities because each cycle summarises the previous summary, so detail
decays every round.

Raw turns are never deleted. Compacted turns leave the live window but stay
searchable, so anything the summary dropped is still recoverable.
"""
import json
import sqlite3
import time
from dataclasses import dataclass

RECENT_TURNS = 6          # exchanges, so 12 messages
RECENT_MESSAGES = RECENT_TURNS * 2
COMPACT_AT_TOKENS = 2800
SUMMARY_MAX_TOKENS = 220


def estimate_tokens(text: str) -> int:
    """Rough enough for budgeting. Avoids shipping a tokenizer."""
    return len(text) // 4 + 1


@dataclass
class Context:
    recent: list[dict]
    short: str
    long: list[str]
    entities: list[str] = None

    def __post_init__(self):
        if self.entities is None:
            self.entities = []

    def as_messages(self) -> list[dict]:
        blocks = []
        if self.long:
            blocks.append("What you know about this person:\n" +
                          "\n".join(f"- {f}" for f in self.long))
        if self.entities:
            blocks.append("Specifics mentioned earlier, keep these exact:\n" +
                          "\n".join(f"- {e}" for e in self.entities))
        if self.short:
            blocks.append(f"Earlier in this conversation:\n{self.short}")
        messages = []
        if blocks:
            messages.append({"role": "system", "content": "\n\n".join(blocks)})
        return messages + self.recent

    def tokens(self) -> int:
        return sum(estimate_tokens(m["content"]) for m in self.as_messages())


SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT,
  compacted INT DEFAULT 0, created REAL);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session, id);
CREATE TABLE IF NOT EXISTS summaries(
  session TEXT PRIMARY KEY, text TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY, session TEXT, value TEXT, created REAL,
  UNIQUE(session, value));
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
  content, session UNINDEXED, content=turns, content_rowid=id);
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
  INSERT INTO turns_fts(rowid, content, session)
  VALUES (new.id, new.content, new.session);
END;
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, owner TEXT, fact TEXT, source_session TEXT,
  created REAL, UNIQUE(owner, fact));
"""


class Memory:
    def __init__(self, conn_factory):
        self._conn = conn_factory

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def add_turn(self, session: str, role: str, content: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO turns(session,role,content,created) VALUES(?,?,?,?)",
                (session, role, content, time.time()))

    def load(self, session: str, owner: str = "anon") -> Context:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content FROM turns WHERE session=? AND compacted=0"
                " ORDER BY id DESC LIMIT ?", (session, RECENT_MESSAGES)).fetchall()
            summary = conn.execute(
                "SELECT text FROM summaries WHERE session=?", (session,)).fetchone()
            facts = conn.execute(
                "SELECT fact FROM facts WHERE owner=? ORDER BY id DESC LIMIT 12",
                (owner,)).fetchall()
            ents = conn.execute(
                "SELECT value FROM entities WHERE session=? ORDER BY id DESC LIMIT 15",
                (session,)).fetchall()
        recent = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        if recent and recent[0]["role"] == "assistant":
            recent = recent[1:]  # never start on a dangling assistant turn
        return Context(recent=recent,
                       short=summary["text"] if summary else "",
                       long=[f["fact"] for f in facts],
                       entities=[e["value"] for e in ents])

    def needs_compaction(self, session: str) -> bool:
        return self.load(session).tokens() > COMPACT_AT_TOKENS

    def pending(self, session: str) -> list[dict]:
        """Oldest half of the live window, the part that will be folded away."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, role, content FROM turns WHERE session=? AND compacted=0"
                " ORDER BY id", (session,)).fetchall()
        keep = RECENT_MESSAGES
        return [dict(r) for r in rows[:-keep]] if len(rows) > keep else []

    def apply_compaction(self, session: str, ids: list[int], summary: str,
                         entities: list[str] | None = None) -> None:
        with self._conn() as conn:
            conn.executemany("UPDATE turns SET compacted=1 WHERE id=?",
                             [(i,) for i in ids])
            for value in (entities or []):
                value = value.strip()
                if 2 < len(value) < 120:
                    try:
                        conn.execute("INSERT INTO entities(session,value,created)"
                                     " VALUES(?,?,?)", (session, value, time.time()))
                    except sqlite3.IntegrityError:
                        pass
            conn.execute(
                "INSERT INTO summaries(session,text,updated) VALUES(?,?,?)"
                " ON CONFLICT(session) DO UPDATE SET text=excluded.text,"
                " updated=excluded.updated", (session, summary, time.time()))

    def remember(self, owner: str, facts: list[str], session: str) -> int:
        added = 0
        with self._conn() as conn:
            for fact in facts:
                fact = fact.strip()
                if not (8 < len(fact) < 200):
                    continue
                try:
                    conn.execute(
                        "INSERT INTO facts(owner,fact,source_session,created)"
                        " VALUES(?,?,?,?)", (owner, fact, session, time.time()))
                    added += 1
                except sqlite3.IntegrityError:
                    pass
        return added

    def forget(self, owner: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM facts WHERE owner=?", (owner,))

    def search(self, session: str, query: str, limit: int = 5) -> list[dict]:
        """Full-text recall over everything ever said, compacted or not."""
        terms = " OR ".join(w for w in query.split() if len(w) > 2)
        if not terms:
            return []
        with self._conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT t.role, t.content FROM turns_fts f JOIN turns t"
                    " ON t.id = f.rowid WHERE turns_fts MATCH ? AND t.session = ?"
                    " ORDER BY rank LIMIT ?", (terms, session, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"role": r["role"], "content": r["content"][:400]} for r in rows]
