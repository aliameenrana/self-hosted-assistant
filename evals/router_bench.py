"""Router accuracy on queries written to avoid the trigger vocabulary.

Three tiers: plain phrasing, paraphrase that shares no words with the
triggers, and adversarial cases where the obvious keyword points at the wrong
tool. A router that only passes tier one is memorising, not generalising.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tool_router import ToolRouter
from core.tools import WEB_TOOLS
from core.tools.memory_tools import build


class FakeMem:
    def search(self, *a, **k): return []
    def remember(self, *a, **k): return 1


REGISTRY = {**WEB_TOOLS, **build(FakeMem(), "s", "o")}

PLAIN = [
    ("what is 4871 times 392", "calculate"),
    ("how many pounds is 80 kg", "convert_units"),
    ("what day is it", "get_datetime"),
    ("search for the latest llama.cpp release", "search_web"),
    ("read https://en.wikipedia.org/wiki/Mutex", "read_url"),
    ("build me a landing page for a bakery", "create_webpage"),
    ("extract the name and email from this", "extract_structured"),
    ("compare these two versions", "diff_text"),
    ("what is in the ggml-org/llama.cpp readme", "read_repo"),
    ("what did I say earlier about rust", "search_memory"),
    ("remember that I prefer rust", "remember_fact"),
]

PARAPHRASE = [
    ("whats 17 percent of 4200", "calculate"),
    ("4871 * 392", "calculate"),
    ("add up 45, 89 and 203 for me", "calculate"),
    ("two thirds of 900", "calculate"),
    ("split 847 between 3 people", "calculate"),
    ("is 80 kilos more or less than 180 pounds", "convert_units"),
    ("how cold is 40 fahrenheit really", "convert_units"),
    ("a 2 terabyte drive holds how many gigabytes", "convert_units"),
    ("whats that in metric", "convert_units"),
    ("whats the date", "get_datetime"),
    ("is it the weekend yet", "get_datetime"),
    ("how long until christmas", "get_datetime"),
    ("who won the election", "search_web"),
    ("whats new with llama.cpp lately", "search_web"),
    ("any news on the release", "search_web"),
    ("https://en.wikipedia.org/wiki/Mutex summarise this", "read_url"),
    ("have a look at this link for me", "read_url"),
    ("knock me up a quick page for a bakery", "create_webpage"),
    ("i want a mockup of a dashboard", "create_webpage"),
    ("can you show me that as a web thing", "create_webpage"),
    ("put together something i can click around", "create_webpage"),
    ("get the name, email and phone from this text", "extract_structured"),
    ("turn this cv into json", "extract_structured"),
    ("list out the line items from this invoice", "extract_structured"),
    ("whats different between these two versions", "diff_text"),
    ("did anything change from the first draft", "diff_text"),
    ("show me what moved between these", "diff_text"),
    ("whats in the ggml-org/llama.cpp readme", "read_repo"),
    ("go look at the source on github for torvalds/linux", "read_repo"),
    ("what was that thing i mentioned ages ago", "search_memory"),
    ("i forget, what did we decide on", "search_memory"),
    ("remind me what i told you about my setup", "search_memory"),
    ("keep in mind i work in python", "remember_fact"),
    ("note that my deadline is friday", "remember_fact"),
    ("for future reference im on windows", "remember_fact"),
]

# The obvious keyword points somewhere wrong, or two tools compete.
ADVERSARIAL = [
    ("how do i calculate a median by hand", None),          # explain, no tool
    ("what is a web page made of", None),                   # not create_webpage
    ("explain what github is", None),                       # not read_repo
    ("why do people remember things badly", None),          # not remember_fact
    ("what does convert mean in typescript", None),         # not convert_units
    ("write python that compares two files", None),         # code, not diff_text
    ("whats 2+2", "calculate"),
    ("convert this cv to json", "extract_structured"),      # convert, but extract
    ("what time does the github api rate limit reset", "get_datetime"),
]


def evaluate(router, cases, label):
    top1 = hits = 0
    scored = 0
    for query, want in cases:
        ranked = router.score(query)
        offered, _ = router.select(query, REGISTRY)
        if want is None:
            # Success is not forcing a wrong specialist tool into the offer.
            specialists = set(offered) - {"search_web", "get_datetime"}
            ok = not specialists
            hits += ok
            top1 += ok
            scored += 1
            if not ok:
                print(f"  [{label}] noise {query:<44} pulled {sorted(specialists)}")
            continue
        scored += 1
        if ranked and ranked[0][0] == want:
            top1 += 1
        if want in offered:
            hits += 1
        else:
            print(f"  [{label}] MISS  {query:<44} want={want}")
    return top1 / scored, hits / scored


def run(alpha=None, top_k=3):
    kwargs = {"top_k": top_k}
    if alpha is not None:
        kwargs["alpha"] = alpha
    router = ToolRouter(REGISTRY, **kwargs)
    results = {}
    for label, cases in (("plain", PLAIN), ("para", PARAPHRASE), ("adv", ADVERSARIAL)):
        results[label] = evaluate(router, cases, label)
    every = PLAIN + PARAPHRASE + ADVERSARIAL
    sizes = [len(router.select(q, REGISTRY)[0]) for q, _ in every]
    results["mean_offered"] = sum(sizes) / len(sizes)
    results["n"] = len(every)
    return results


if __name__ == "__main__":
    r = run()
    print()
    for label in ("plain", "para", "adv"):
        t1, hit = r[label]
        print(f"  {label:<6} top1={t1:.0%}  recall={hit:.0%}")
    total = sum(r[l][1] * n for l, n in
                (("plain", len(PLAIN)), ("para", len(PARAPHRASE)),
                 ("adv", len(ADVERSARIAL)))) / r["n"]
    print(f"\n  overall recall {total:.0%} over {r['n']} cases, "
          f"mean {r['mean_offered']:.1f} of {len(REGISTRY)} tools offered")
