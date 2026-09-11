"""The fabrication test. Everything else is plumbing."""
import asyncio
import sys

from core.harness import Harness
from core.tools import WEB_TOOLS


async def run(mode: str) -> tuple[str, object]:
    h = Harness("http://127.0.0.1:8099", "fake", WEB_TOOLS)
    text, tel = "", None
    async for ev in h.answer("what is 17 times 23", "onyx"):
        if ev["type"] == "token":
            text += ev["text"]
        else:
            tel = ev["telemetry"]
    return text, tel


if __name__ == "__main__":
    mode = sys.argv[1]
    text, tel = asyncio.run(run(mode))
    print(f"MODE={mode}")
    print(f"reply: {text.strip()}")
    for c in tel.tool_calls:
        print(f"  tool={c.name} outcome={c.outcome} result={c.result}")
    print(f"  turns={tel.turns} ttft={tel.ttft_ms}ms")

    names = {c.name for c in tel.tool_calls}
    if "web_search" in names:
        print("RESULT: FAIL, a search actually ran")
    elif mode == "liar":
        claimed = "search" in text.lower()
        print(f"model attempted fabrication: {claimed}")
        print("RESULT: no web_search tool was executed. "
              "Telemetry is ground truth and shows only:", sorted(names))

    from core.harness import detect_fabrication
    flags = detect_fabrication(text, tel)
    print("fabrication flags:", flags or "none")
