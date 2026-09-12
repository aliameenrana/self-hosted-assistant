import json, sys, time, urllib.request

URL = "http://127.0.0.1:8090/v1/chat/completions"

def run(prompt, think=False, max_tokens=200):
    sys_msg = "You are helpful." if think else "/no_think You are helpful."
    body = json.dumps({"model": "qwen3-8b", "stream": True, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": sys_msg},
                     {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(URL, body, {"content-type": "application/json"})
    start = time.monotonic(); ttft = None; n = 0; text = ""
    with urllib.request.urlopen(req) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: ") or line.endswith("[DONE]"):
                continue
            delta = json.loads(line[6:])["choices"][0].get("delta", {}).get("content")
            if delta:
                if ttft is None:
                    ttft = time.monotonic() - start
                n += 1; text += delta
    total = time.monotonic() - start
    return ttft or total, total, n, text

print(f"{'prompt':<34} {'ttft':>7} {'total':>7} {'tok':>5} {'tok/s':>7}")
for p in ["What is a mutex? Two sentences.",
          "Name three sorting algorithms.",
          "Why is CAP theorem misunderstood?"]:
    ttft, total, n, text = run(p)
    rate = n / total if total else 0
    print(f"{p[:33]:<34} {ttft*1000:>6.0f}ms {total:>6.2f}s {n:>5} {rate:>6.1f}")
