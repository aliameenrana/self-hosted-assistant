"""Pick the few tools worth showing the model for a given message.

Router accuracy drops once a small model sees more than roughly eight tools,
and we now have eleven. Retrieval keeps the offered set small.

Scoring is a hybrid of BM25 and TF-IDF, plus regex patterns for shapes that
neither can see.

Embeddings were measured against this and lost, on our own tool set:

    lexical      top1 88%   recall@k 100%   0.03ms/query
    MiniLM-L6    top1 83%   recall@k  88%   5ms/query, 162s load, ~11GB cache

That inverts the published result (BM25 14 percent Top-1, BM25 plus TF-IDF 21,
embeddings 38) because those benchmarks ran 270 to 2,792 tools, where lexical
scoring drowns in near-duplicate names. At eleven tools with hand-written
triggers there is nothing to disambiguate, and the same source puts the
crossover at roughly 2,000 tools.

Revisit embeddings if the catalogue passes a few hundred tools. Until then
they cost a gigabyte of dependencies to be worse.

Alpha is 0.2 on BM25 and 0.8 on TF-IDF, which is where that study found the
optimum across the full range.

Patterns exist because lexical scoring cannot tell that "4871 * 392" is
arithmetic. The symbols are stripped and bare digits match nothing, so every
score comes out zero and the calculator is never offered.
"""
import math
import re
from collections import Counter

# Always offered. Cheap, frequently needed, and the model reaches for them
# often enough that omitting one is worse than the context they cost.
ALWAYS = {"search_web", "get_datetime"}

TOP_K = 3

# Written in user language, not tool language. This is the mitigation for the
# vocabulary mismatch that pure similarity gets wrong.
TRIGGERS = {
    "calculate": "maths arithmetic multiply divide add subtract sum total "
                 "percent how much times product average work out",
    "search_web": "search look up google find current news latest recent "
                  "today happening who is what is release",
    "read_url": "open this link read this page fetch url website article",
    "get_datetime": "date time today now day week month year clock when",
    "create_webpage": "build make create design website page site landing "
                      "dashboard chart html mockup demo game visual show me "
                      "web thing knock up put together whip up something i "
                      "can look at render display preview interactive",
    "extract_structured": "extract pull parse fields json structured cv "
                          "resume invoice table records data out of get the "
                          "name email phone address from this list them",
    "diff_text": "diff compare changed difference between versions before "
                 "after revision edit changes draft drafts first second "
                 "anything change what moved side by side old new",
    "convert_units": "convert units kg pounds miles km celsius fahrenheit "
                     "litres gallons bytes gigabytes how many in",
    "read_repo": "github repository owner slash name clone readme "
                 "browse the repo look at the source on github",
    "search_memory": "earlier before you said i said remember recall "
                     "previously we discussed forgot conversation "
                     "what did i say did i mention last time we talked "
                     "remind me what i told you ages ago back then forget "
                     "forgot decide decided that thing we agreed",
    "remember_fact": "remember note keep in mind store save for later "
                     "my name is i prefer i work with",
}

# A question ABOUT a thing is not a request to DO the thing. "explain what
# github is" scored read_repo because it shares vocabulary with the trigger,
# which is the adversarial tier the bench measures.
EXPLAIN = re.compile(
    r"^\s*(what|why|how)\s+(is|are|was|were|do|does|did|come|come s)\b"
    r"|^\s*(explain|describe|define|tell me about|what'?s the difference)\b"
    r"|\bwhat does \w+ mean\b|\bhow do(es)? \w+ work\b", re.I)

# Beats EXPLAIN: real requests that happen to be phrased as questions.
# Recall about this conversation is the big one, "what did I say earlier"
# parses as a question but is a genuine lookup.
DO_ANYWAY = re.compile(
    r"\d\s*[-+*/^%]\s*\d|\bhttps?://|\d+\s*(percent|%)\b"
    r"|\bwhat (day|date|time|year) is\b|\bwhat'?s the (date|time)\b"
    r"|\bhow (many|much) .*\b(in|to)\s+\w+$"
    r"|\b(i|we|you) (said|told|mentioned|asked|decided|agreed)\b"
    r"|\bdid i (say|mention|tell)\b|\bearlier\b|\blast time\b"
    r"|\bwe (discussed|talked about)\b", re.I)

_WORD = re.compile(r"[a-z0-9]+")

# Lexical scoring cannot see that "4871 * 392" is arithmetic: the symbols are
# stripped and bare digits match nothing. These fire on shape, not vocabulary.
PATTERNS = [
    # Arithmetic in words, not symbols: "two thirds of 900", "split 847
    # between 3", "add up 45, 89 and 203".
    (re.compile(r"\d\s*[-+*/^%]\s*\d|\d+\s*(times|plus|minus|divided)"
                r"|\b(half|third|thirds|quarter|quarters|double|triple)\b.*\d"
                r"|\b(split|divide|share|add up|sum|total|average)\b.*\d"
                r"|\d.*\b(between|among)\b.*\d"), "calculate"),
    # Elliptical unit reference: "whats that in metric", "in celsius".
    (re.compile(r"\bin (metric|imperial|celsius|fahrenheit|kilos|pounds|"
                r"km|miles|inches|feet|litres|liters|gallons)\b"), "convert_units"),
    (re.compile(r"https?://"), "read_url"),
    (re.compile(r"\b[\w.-]+/[\w.-]+\b(?!\s*(=|\d))"), "read_repo"),
    (re.compile(r"\d+\s*(kg|lb|lbs|km|mi|miles|cm|ft|in|c|f|gb|mb|kb|ml|l|oz)\b"),
     "convert_units"),
]
_STOP = {"the", "a", "an", "is", "it", "to", "of", "and", "for", "in", "on",
         "do", "i", "you", "me", "my", "can", "what", "how", "this", "that"}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower())
            if w not in _STOP and len(w) > 1]


# Measured on evals/router_bench.py: the alpha sweep is nearly flat and BM25
# alone edges ahead once triggers are written in user language. Kept at a
# hybrid because the published comparison favours it at larger tool counts and
# the cost is nil.
ALPHA = 0.5       # weight on BM25, the rest on TF-IDF
BM25_K1 = 1.5
BM25_B = 0.75


class ToolRouter:
    def __init__(self, registry: dict, top_k: int = TOP_K, alpha: float = ALPHA):
        self.top_k = top_k
        self.alpha = alpha
        self._docs: dict[str, Counter] = {}
        self._idf: dict[str, float] = {}
        self._len: dict[str, int] = {}
        self._avg_len = 1.0
        self.reindex(registry)

    def reindex(self, registry: dict) -> None:
        self._docs = {}
        for name, tool in registry.items():
            text = f"{name} {name.replace('_', ' ')} {tool.description} " \
                   f"{TRIGGERS.get(name, '')}"
            self._docs[name] = Counter(_tokens(text))
        self._len = {n: sum(c.values()) or 1 for n, c in self._docs.items()}
        self._avg_len = (sum(self._len.values()) / len(self._len)) if self._len else 1.0
        n = len(self._docs) or 1
        seen: Counter = Counter()
        for counts in self._docs.values():
            seen.update(counts.keys())
        self._idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5))
                     for w, c in seen.items()}

    def _bm25(self, name: str, query: Counter) -> float:
        counts, length = self._docs[name], self._len[name]
        total = 0.0
        for word, qf in query.items():
            tf = counts.get(word, 0)
            if not tf:
                continue
            norm = tf * (BM25_K1 + 1) / (
                tf + BM25_K1 * (1 - BM25_B + BM25_B * length / self._avg_len))
            total += self._idf.get(word, 0.0) * norm * qf
        return total

    def _tfidf(self, name: str, query: Counter) -> float:
        counts, length = self._docs[name], self._len[name]
        return sum((counts[w] / length) * self._idf.get(w, 0.0) * qf
                   for w, qf in query.items() if w in counts)

    def score(self, message: str) -> list[tuple[str, float]]:
        query = Counter(_tokens(message))
        if not query:
            return []
        bm = {n: self._bm25(n, query) for n in self._docs}
        tf = {n: self._tfidf(n, query) for n in self._docs}
        bm_max = max(bm.values()) or 1.0
        tf_max = max(tf.values()) or 1.0
        ranked = [(n, self.alpha * (bm[n] / bm_max)
                   + (1 - self.alpha) * (tf[n] / tf_max)) for n in self._docs]
        return sorted(ranked, key=lambda x: -x[1])

    def select(self, message: str, registry: dict) -> tuple[dict, list[str]]:
        """Return the tools to offer, plus the ranking for telemetry."""
        ranked = self.score(message)
        lowered = message.lower()
        explaining = bool(EXPLAIN.search(message)) and not DO_ANYWAY.search(message)

        hits = [(n, sc) for n, sc in ranked if sc > 0]
        if hits and not explaining:
            # Only offer runners up that are close to the leader. A weak second
            # place is noise, and noise is what the model wrongly reaches for.
            best = hits[0][1]
            chosen = {n for n, sc in hits[:self.top_k] if sc >= best * 0.35}
        elif explaining:
            # Scores are normalised to the leader, so a dominance test always
            # passes and tells us nothing. A question about a thing needs no
            # specialist tool: search alone can answer it.
            chosen = set()
        else:
            chosen = set()

        chosen |= {name for pattern, name in PATTERNS
                   if name in registry and pattern.search(lowered)}
        chosen |= ALWAYS & set(registry)
        offered = {n: t for n, t in registry.items() if n in chosen}
        order = [n for n, _ in ranked if n in chosen]
        return offered, order
