"""Collect persona output for review. Writes a markdown sheet to grade by hand."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.harness import Harness, detect_fabrication
from core.tools import WEB_TOOLS
from evals.cases import all_cases


async def main():
    h = Harness(os.getenv("LLM_BASE_URL", "http://localhost:8080"),
                os.getenv("LLM_MODEL", "qwen3-8b"), WEB_TOOLS)
    out = ["# Eval sheet\n", "Grade each by hand. Mark PASS or FAIL.\n"]
    fabrications = 0

    for persona, case in all_cases():
        text = ""
        tel = None
        async for ev in h.answer(case["prompt"], persona):
            if ev["type"] == "token":
                text += ev["text"]
            else:
                tel = ev["telemetry"]
        flags = detect_fabrication(text, tel)
        if flags:
            fabrications += 1

        out += [
            f"\n## {persona} / {case['id']}\n",
            f"**prompt:** {case['prompt']}\n",
            f"**reply:**\n\n> {text.strip() or '(empty)'}\n",
            f"must: {', '.join(case['must'])}\n",
            f"must not: {', '.join(case['must_not'])}\n",
            f"em dash present: {'YES, FAIL' if chr(8212) in text else 'no'}\n",
            f"fabrication flags: {flags or 'none'}\n",
            "verdict: \n",
        ]

    path = "evals/sheet.md"
    with open(path, "w") as fh:
        fh.write("\n".join(out))
    print(f"wrote {path}")
    print(f"fabrication flags raised: {fabrications}")
    if fabrications:
        print("FAIL: any fabrication flag is a hard failure")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
