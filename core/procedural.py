"""Procedural memory: remember which tool sequence worked for a question shape.

Agent Workflow Memory (AWM) in the literature induces workflows from action
trajectories, embeds a description, and retrieves by similarity at inference
time, reporting 30-50% step reductions. That is built for agents running
thousands of trajectories across many distinct task types.

At this project's scale the useful version is much smaller: which set of
tools, in which order, resolved a question with this shape, so the router can
offer the WHOLE set together next time instead of the model discovering the
second tool is needed only after already answering with the first. This is
also a straight latency win, the same category as the multi-part heuristic in
the harness: fewer wasted decision turns.

Keyed on words significant to the question, not full text, so "convert 5kg to
lbs and tell me the date" and "convert 10 miles to km and what time is it"
hit the same procedure despite different numbers.
"""
import re
import time
from collections import Counter

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "is", "are", "was", "for", "of", "and", "in", "on",
         "to", "what", "how", "tell", "me", "also", "then", "please"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures(
  id INTEGER PRIMARY KEY, shape TEXT, tools TEXT,
  uses INTEGER DEFAULT 1, last_used REAL, created REAL,
  UNIQUE(shape));
"""

MAX_PROCEDURES = 500          # small table, prune the coldest when it grows
MIN_TOOLS = 2                 # a single-tool answer is not a workflow


def _shape(question: str) -> str:
    """A normalised key: significant words, sorted, numbers stripped.

    Sorted so word order does not matter ("convert X and check the date" and
    "check the date and convert X" hit the same key), numbers stripped so
    the quantity does not fragment the key.
    """
    # Strip leading digits so "5kg" and "10kg" both normalise to "kg" - a
    # token like "5kg" is alphanumeric, not a pure digit, so isdigit() alone
    # missed it and the quantity was leaking into the key.
    words = set()
    for raw in _WORD.findall(question.lower()):
        w = raw.lstrip("0123456789")
        if w and w not in _STOP and len(w) > 2:
            words.add(w)
    return " ".join(sorted(words))


class Procedural:
    def __init__(self, conn_factory):
        self._conn = conn_factory

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def record_success(self, question: str, tools_used: list[str],
                       threshold: float = 0.4) -> None:
        distinct = list(dict.fromkeys(tools_used))  # preserve order, dedupe
        if len(distinct) < MIN_TOOLS:
            return
        shape = _shape(question)
        if not shape:
            return
        qwords = set(shape.split())
        now = time.time()
        with self._conn() as conn:
            # Fuzzy match against existing rows before inserting, mirroring
            # recall()'s own matching. Writing on exact-shape only while
            # reading on fuzzy match meant two near-identical questions never
            # accumulated uses on the same row, so "uses >= 2" was never
            # reached and recall() always returned None even for a pattern
            # that had genuinely repeated.
            rows = conn.execute("SELECT id, shape, uses FROM procedures").fetchall()
            best_id, best_score = None, 0.0
            for row in rows:
                pwords = set(row["shape"].split())
                if not pwords:
                    continue
                score = len(qwords & pwords) / min(len(qwords), len(pwords))
                if score > best_score:
                    best_id, best_score = row["id"], score

            if best_id is not None and best_score >= threshold:
                conn.execute(
                    "UPDATE procedures SET tools=?, uses=uses+1, last_used=?"
                    " WHERE id=?", (",".join(distinct), now, best_id))
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO procedures"
                    "(shape,tools,last_used,created) VALUES(?,?,?,?)",
                    (shape, ",".join(distinct), now, now))
                n = conn.execute("SELECT COUNT(*) c FROM procedures").fetchone()["c"]
                if n > MAX_PROCEDURES:
                    conn.execute(
                        "DELETE FROM procedures WHERE id IN (SELECT id FROM "
                        "procedures ORDER BY last_used LIMIT ?)",
                        (n - MAX_PROCEDURES,))

    def recall(self, question: str, threshold: float = 0.4) -> list[str] | None:
        """The tool sequence that resolved a similarly shaped question before,
        or None. A procedure used only once is a coincidence, not a pattern,
        so it needs at least two successful uses before it is trusted."""
        qwords = set(_shape(question).split())
        if not qwords:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT shape, tools FROM procedures WHERE uses >= 2"
                " ORDER BY last_used DESC LIMIT 100").fetchall()
        best, best_score = None, 0.0
        for row in rows:
            pwords = set(row["shape"].split())
            if not pwords:
                continue
            score = len(qwords & pwords) / min(len(qwords), len(pwords))
            if score > best_score:
                best, best_score = row["tools"].split(","), score
        return best if best_score >= threshold else None
