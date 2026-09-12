"""Router accuracy on queries written to avoid the trigger vocabulary.

The first test set shared wording with the triggers, which made it easy. These
are paraphrases a real user might type instead.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tool_router import ToolRouter
from core.tools import WEB_TOOLS
from core.tools.memory_tools import build


class FakeMem:
    def search(self, *a, **k): return []
    def remember(self, *a, **k): return 1


REGISTRY = {**WEB_TOOLS, **build(FakeMem(), "s", "o")}

CASES = [
    ("whats 17 percent of 4200", "calculate"),
    ("4871 * 392", "calculate"),
    ("add up 45, 89 and 203 for me", "calculate"),
    ("is 80 kilos more or less than 180 pounds", "convert_units"),
    ("how cold is 40 fahrenheit really", "convert_units"),
    ("a 2 terabyte drive holds how many gigabytes", "convert_units"),
    ("whats the date", "get_datetime"),
    ("is it the weekend yet", "get_datetime"),
    ("who won the election", "search_web"),
    ("whats new with llama.cpp lately", "search_web"),
    ("https://en.wikipedia.org/wiki/Mutex summarise this", "read_url"),
    ("knock me up a quick page for a bakery", "create_webpage"),
    ("i want a mockup of a dashboard", "create_webpage"),
    ("can you show me that as a web thing", "create_webpage"),
    ("get the name, email and phone from this text", "extract_structured"),
    ("turn this cv into json", "extract_structured"),
    ("whats different between these two versions", "diff_text"),
    ("did anything change from the first draft", "diff_text"),
    ("whats in the ggml-org/llama.cpp readme", "read_repo"),
    ("go look at the source on github for torvalds/linux", "read_repo"),
    ("what was that thing i mentioned ages ago", "search_memory"),
    ("i forget, what did we decide on", "search_memory"),
    ("keep in mind i work in python", "remember_fact"),
    ("note that my deadline is friday", "remember_fact"),
]


def run(top_k: int = 3, alpha: float = 0.2) -> tuple[float, float, float]:
    router = ToolRouter(REGISTRY, top_k=top_k, alpha=alpha)
    top1 = topk = 0
    sizes = []
    for query, want in CASES:
        ranked = router.score(query)
        offered, _ = router.select(query, REGISTRY)
        if ranked and ranked[0][0] == want:
            top1 += 1
        if want in offered:
            topk += 1
        else:
            print(f"  MISS  {query:<46} want={want}")
        sizes.append(len(offered))
    n = len(CASES)
    return top1 / n, topk / n, sum(sizes) / n


if __name__ == "__main__":
    print("alpha sweep (recall@k):")
    for a in (0.0, 0.2, 0.5, 0.8, 1.0):
        t1, tk, sz = run(alpha=a)
        label = {0.0: "pure TF-IDF", 1.0: "pure BM25"}.get(a, f"hybrid {a}")
        print(f"  {label:<14} top1={t1:.0%}  recall@k={tk:.0%}  mean offered={sz:.1f}")
