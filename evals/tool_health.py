"""P7: smoke test each tool with a known-good input, assert a known-good
shape. Would have caught the DDG Instant Answer bug on day one. Run weekly
or in CI, not per-request.

    .venv/bin/python evals/tool_health.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tools import WEB_TOOLS, execute, ToolError

CHECKS = [
    ("get_datetime", {}, lambda r: "iso" in r),
    ("calculate", {"expression": "2+2"}, lambda r: r["result"] == 4),
    ("search_web", {"query": "capital of france"},
     lambda r: len(r["results"]) > 0 and "paris" in str(r).lower()),
    ("read_url", {"url": "https://en.wikipedia.org/wiki/Mutual_exclusion"},
     lambda r: len(r["content"]) > 500),
    ("convert_units", {"value": 1, "from_unit": "km", "to_unit": "m"},
     lambda r: r["result"] == 1000),
    ("diff_text", {"before": "a", "after": "b"}, lambda r: "similarity" in r),
    ("extract_structured", {"text": "x", "fields": "a"}, lambda r: "fields" in r),
    ("read_repo", {"repo": "ggml-org/llama.cpp", "path": "README.md"},
     lambda r: len(r["content"]) > 100),
]

if __name__ == "__main__":
    fail = 0
    for name, args, check in CHECKS:
        try:
            r = execute(WEB_TOOLS, name, args)
            ok = check(r)
        except (ToolError, Exception) as e:
            ok, r = False, str(e)
        fail += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<20} {str(r)[:70] if not ok else ''}")
    print(f"\n{len(CHECKS) - fail}/{len(CHECKS)} healthy")
    sys.exit(1 if fail else 0)
