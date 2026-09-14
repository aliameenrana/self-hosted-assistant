"""Direct 4B vs 8B comparison on tool-call correctness and raw throughput.

Runs the harness directly against each llama-server instance, bypassing the
web app so results aren't affected by queueing or rate limits. Same tool
registry, same router, same prompts, only the model differs.
"""
import asyncio
import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/evals/", 1)[0])

from core.harness import Harness, check_citations, detect_fabrication
from core.tool_router import ToolRouter
from core.tools import WEB_TOOLS

MODELS = {
    "8B": ("http://127.0.0.1:8090", "qwen3-8b"),
    "4B": ("http://127.0.0.1:8091", "qwen3-4b"),
}

CASES = [
    ("what is 4871 times 392", "calculate", None),
    ("find the best pizza places in Johar Town Lahore", "search_web", None),
    ("open aliameen.com and tell me what you see", "read_url", None),
    ("what is today's date", "get_datetime", None),
    ("how many pounds is 80 kg", "convert_units", None),
    ("what is the capital of France", None, None),
    ("search for the exact final score of the most recent super bowl, "
     "then multiply the winning team score by 1000",
     "search_web", "calculate"),
    ("convert 3 litres to gallons", None, "must_not_fabricate"),
]


async def run_case(harness, message, want_first, want_second):
    text, tel = "", None
    async for ev in harness.answer(message, "default"):
        if ev["type"] == "token":
            text += ev["text"]
        elif ev["type"] == "done":
            tel = ev["telemetry"]
    used = [c.name for c in tel.tool_calls if c.outcome == "ok"]
    flags = detect_fabrication(text, tel) + check_citations(text, tel)
    problems = []
    if want_first and want_first not in used:
        problems.append(f"missing {want_first}")
    if want_second == "must_not_fabricate":
        if any(ch.isdigit() for ch in text) and not used:
            problems.append("fabricated a number with no tool")
    elif want_second and want_second not in used:
        problems.append(f"missing {want_second}")
    if flags:
        problems.append(f"flags: {flags}")
    return problems, tel


async def bench_raw(base_url: str, model: str) -> float:
    """Raw decode tok/s, no harness, no tools."""
    t0 = time.time()
    body = json.dumps({"model": model, "messages": [
        {"role": "user", "content": "count from 1 to 80"}],
        "max_tokens": 100, "stream": False}).encode()
    req = urllib.request.Request(f"{base_url}/v1/chat/completions", body,
                                 {"content-type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    dt = time.time() - t0
    tok = r["usage"]["completion_tokens"]
    return tok / dt


async def main():
    results = {}
    for label, (base_url, model) in MODELS.items():
        print(f"\n=== {label} ({model}) ===")
        harness = Harness(base_url, model, WEB_TOOLS)
        harness.router = ToolRouter(WEB_TOOLS)
        passed = 0
        total_ttft = 0
        for message, w1, w2 in CASES:
            t0 = time.time()
            problems, tel = await run_case(harness, message, w1, w2)
            ttft = tel.ttft_ms if tel else 0
            total_ttft += ttft
            ok = not problems
            passed += ok
            mark = "ok  " if ok else "FAIL"
            print(f"  {mark} {message[:55]:<57} ttft={ttft}ms")
            for p in problems:
                print(f"        {p}")
        rate = await bench_raw(base_url, model)
        mean_ttft = total_ttft / len(CASES)
        print(f"  --- {passed}/{len(CASES)} correct, "
              f"mean ttft {mean_ttft:.0f}ms, raw decode {rate:.1f} tok/s")
        results[label] = {"passed": passed, "total": len(CASES),
                          "mean_ttft": mean_ttft, "tok_s": rate}

    print("\n=== summary ===")
    for label, r in results.items():
        print(f"  {label}: {r['passed']}/{r['total']} correct, "
              f"{r['mean_ttft']:.0f}ms mean ttft, {r['tok_s']:.1f} tok/s raw")


if __name__ == "__main__":
    asyncio.run(main())
