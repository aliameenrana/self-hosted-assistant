import asyncio, sys, time
sys.path.insert(0, "/Users/apple/ali/work/self-hosted")
from core.harness import Harness, check_citations
from core.tools import WEB_TOOLS
from core.personas import voice_prompt

CASES = [
    "search for the best pizza in lahore and tell me the top result",
    "search for zzqqx nonexistent gibberish term",
    "what is 4871 times 392, and what would 10 percent more be",
]

async def run(think):
    h = Harness("http://127.0.0.1:8090", "qwen3-8b", WEB_TOOLS)
    import core.harness as H
    orig = H.voice_prompt
    H.voice_prompt = lambda p: orig(p, think=think)
    results = []
    for q in CASES:
        t0 = time.monotonic()
        text, tel = "", None
        async for ev in h.answer(q, "default"):
            if ev["type"] == "token": text += ev["text"]
            else: tel = ev["telemetry"]
        results.append((tel.total_ms, len(text.split()), text[:90]))
    H.voice_prompt = orig
    return results

async def main():
    for label, think in [("no-think", False), ("think", True)]:
        rows = await run(think)
        ms = [r[0] for r in rows]
        print(f"\n{label}: mean {sum(ms)/len(ms):.0f}ms")
        for total, words, snip in rows:
            print(f"  {total:>6}ms {words:>3}w  {snip}")

asyncio.run(main())
