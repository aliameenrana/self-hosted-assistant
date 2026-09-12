"""Scripted tool-correctness eval. Build Order step 1, finally built.

Every case here corresponds to a real failure seen in use. Run before any
change to the harness, prompts, router or tools.

    .venv/bin/python evals/tool_eval.py
"""
import json
import sys
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8000"

# (message, must_call, must_not_say)
SINGLE = [
    ("what is 4871 times 392", "calculate", ["1905", "1906"]),
    ("find the best pizza places in Johar Town Lahore", "search_web",
     ["cannot retrieve", "real-time data", "unable to"]),
    ("open aliameen.com and tell me what you see", "read_url",
     ["cannot open", "unable to open", "can't open"]),
    ("what is today's date", "get_datetime", ["don't have access", "real-time"]),
    ("how many pounds is 80 kg", "convert_units", []),
    ("what is the capital of France", None, []),
]

# Multi-turn. The conflation case that started this.
SEQUENCE = [
    ("can you search the term 'cars'", "search_web", []),
    ("can you search 'pizza'", "search_web", ["cars", "pixar", "film"]),
]


def ask(op, message, session):
    body = json.dumps({"session": session, "message": message}).encode()
    req = urllib.request.Request(f"{BASE}/api/chat", body,
                                 {"content-type": "application/json"})
    text, tools, tel = "", [], {}
    with op.open(req) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "start":
                session = ev["session"]
            elif ev["type"] == "token":
                text += ev["text"]
            elif ev["type"] == "done":
                tel = ev["telemetry"]
                tools = [t["name"] for t in tel["tools"] if t["outcome"] == "ok"]
    return text.strip(), tools, tel, session


def check(text, tools, want_call, banned):
    problems = []
    if want_call and want_call not in tools:
        problems.append(f"did not call {want_call} (called {tools or 'nothing'})")
    lowered = text.lower()
    for phrase in banned:
        if phrase in lowered:
            problems.append(f"said {phrase!r}")
    return problems


def main():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    failures = 0

    print("single turn")
    for message, want, banned in SINGLE:
        text, tools, tel, _ = ask(op, message, None)
        problems = check(text, tools, want, banned)
        if tel.get("flags"):
            problems.append(f"fabrication flags {tel['flags']}")
        failures += bool(problems)
        mark = "FAIL" if problems else "ok  "
        print(f"  {mark} {message[:46]:<48} {tools or []}")
        for p in problems:
            print(f"       {p}")

    print("\nmulti turn, conflation")
    session = None
    for message, want, banned in SEQUENCE:
        text, tools, tel, session = ask(op, message, session)
        problems = check(text, tools, want, banned)
        failures += bool(problems)
        mark = "FAIL" if problems else "ok  "
        print(f"  {mark} {message[:46]:<48} {tools or []}")
        for p in problems:
            print(f"       {p}")
        if problems:
            print(f"       reply: {text[:150]}")

    total = len(SINGLE) + len(SEQUENCE)
    print(f"\n{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
