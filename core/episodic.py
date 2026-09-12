"""Episodic memory: specific past failures, not durable facts.

The research pattern (embed every episode, retrieve by vector similarity from
a store) is built for agents with thousands of runs. Measured scale here: 151
total tool calls, 8 failures, ever. A vector store for eight rows is the same
mistake as embedding-based tool retrieval at 11 tools: real overhead for
noise. Lexical overlap against a small table does the same job at this scale
and costs nothing.

Only failures are stored. A success teaches nothing an eval suite doesn't
already cover; a failure is the one thing worth warning the next attempt
about, which is also why 25-40% of the measured benefit in the literature
comes specifically from failure retrieval, not success replay.
"""
import re
import time

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "is", "are", "was", "for", "of", "and", "in", "on",
         "to", "search", "find", "what", "how"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY, tool TEXT, query TEXT, failure TEXT,
  session TEXT, created REAL);
CREATE INDEX IF NOT EXISTS episodes_tool ON episodes(tool);
"""

# A query that has failed this many times is retired from warnings, not
# because it stops mattering but because repeating the same warning forever
# is noise. Recency matters more than count past this.
MAX_TRACKED_PER_TOOL = 200


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower())
            if w not in _STOP and len(w) > 2}


class Episodic:
    def __init__(self, conn_factory):
        self._conn = conn_factory

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def record_failure(self, tool: str, query: str, failure: str,
                       session: str) -> None:
        if not query:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO episodes(tool,query,failure,session,created)"
                " VALUES(?,?,?,?,?)", (tool, query[:200], failure, session,
                                       time.time()))
            n = conn.execute("SELECT COUNT(*) c FROM episodes WHERE tool=?",
                             (tool,)).fetchone()["c"]
            if n > MAX_TRACKED_PER_TOOL:
                conn.execute(
                    "DELETE FROM episodes WHERE id IN (SELECT id FROM "
                    "episodes WHERE tool=? ORDER BY created LIMIT ?)",
                    (tool, n - MAX_TRACKED_PER_TOOL))

    def warn(self, tool: str, query: str, threshold: float = 0.5) -> str | None:
        """A one-line warning if a similar query failed before on this tool,
        or None. Word overlap, no embeddings, no model call."""
        qwords = _words(query)
        if not qwords:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT query, failure FROM episodes WHERE tool=?"
                " ORDER BY created DESC LIMIT 50", (tool,)).fetchall()
        for row in rows:
            pwords = _words(row["query"])
            if not pwords:
                continue
            # Overlap against the SMALLER set, not the union. A short past
            # failure ("wibblewombs") fully contained in a longer new query
            # ("convert 10 wibblewombs to lbs") is a strong signal even though
            # the union-based Jaccard ratio would be small and miss it.
            overlap = len(qwords & pwords) / min(len(qwords), len(pwords))
            if overlap >= threshold:
                return (f'A similar {tool} call ("{row["query"]}") '
                        f'{row["failure"]} before. Consider different words.')
        return None
