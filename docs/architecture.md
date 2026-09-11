# Architecture

*Written 2026-09-10, updated 2026-09-11. Companions: docs/ideas/self-hosted-assistant.md,
docs/ideas/personas.md, docs/security.md, docs/scalability.md, docs/coding-agent.md.*

## Constraints Driving Every Decision

1. **Dev on MacBook (Apple Silicon), run on Predator (x86 + GTX 1060 6GB).**
   Different CPU architecture, different GPU vendor. This is the central
   deployment problem and it rules out naive `docker build` + push.
2. **Funny is priority 1, tool honesty priority 2.** The harness must make
   fabricated tool results structurally impossible, not merely discouraged.
3. **Public demo.** Anonymous strangers. Read-only tools, hard limits.
4. **One GPU.** Requests serialize. Queue is a first-class feature, not a patch.
5. **Model host must be swappable.** Predator (1060 6GB) now; 3060 12GB desktop
   later, or both. Nothing above the inference layer may care which.

---

## System Shape

```
Internet
   |
   v
Cloudflare Tunnel  (no open ports on home network)
   |
   v
+-------------------------------- Predator ---------------------------------+
|                                                                            |
|   Caddy/nginx ---> app (FastAPI)  <---------> SQLite (sessions, analytics) |
|                       |                                                    |
|                       |  OpenAI-compatible HTTP                            |
|                       v                                                    |
|                  llama-server (llama.cpp, CUDA)  <-- swappable host        |
|                                                                            |
+----------------------------------------------------------------------------+
```

Deliberately boring. One process for the app, one for inference, one file DB.
No Redis, no Postgres, no message broker, no Kubernetes. Adding any of those
before there is a measured reason is the failure mode this section exists to
prevent.

---

## Layer 1 — Inference

**llama.cpp `llama-server`**, not Ollama. Ollama's tool-call translation is
genuinely good and is the fallback if the harness fights us, but llama-server
exposes the cache controls the latency strategy depends on:

- `cache_prompt: true` — reuse KV across requests sharing a prefix
- `--cache-reuse` — KV shifting across shared chunks
- `--slot-save-path` + slot save/restore — survive across sessions
- explicit `id_slot` — pin a session to a slot

**Model:** Qwen3-8B Q4_K_M. Chosen on tool-call reliability (~13% failure vs
~77-82% for Llama-3.x at this size), not on general chat quality. Dense, not
MoE — dense outperforms larger MoE at tool calling.

On the 1060 6GB this is a hybrid load (some layers to CPU). Expect meaningfully
slower than the 3060's ~52-65 tok/s. **Measure before promising anything.**

**Target hardware is the 1060 6GB.** The model does not fully fit alongside a
usable context, so some layers offload to system RAM — expect ~8-15 tok/s, not
the 50-65 a 12GB card sees. CPU spillover costs ~70% of decode speed. Context
target 8-16k; KV cache grows with it and competes for the same VRAM.

**Swappability:** the app talks to `LLM_BASE_URL` only, so a different GPU or a
rented one is an env var. This is insurance, not a plan — the laptop is the
product.

---

## Layer 2 — The Harness (the actual engineering)

This is what makes it a portfolio piece rather than a chat box.

### Two-Prompt Split

The single most important design decision, and it is what makes the character
and the honesty compatible rather than in tension:

**Pass 1 — Tool loop. Plain, characterless system prompt.**
No persona. No jokes. Just the task, the tools, and the schema. A model in
strong character will narrate tool results it never received, because staying
in voice is easier than admitting a call failed. So character never touches
tool *selection*.

**Pass 2 — Voice layer.** Takes the verified results and writes the reply in
the session's persona.

The persona literally cannot fabricate a tool result, because by the time it
runs, the results are already fixed. **This is a structural guarantee, not a
prompt instruction.** It is the answer to priority 2.

### Tool Loop Rules

- JSON-schema validate every call before execution. Malformed -> retry with the
  error fed back, max 2 retries, then fail honestly.
- Hard turn cap (6). Prevents the documented failure of models issuing the same
  call 16+ times.
- Every call logged: name, args, result, latency, retry count, outcome.
- Loop detection: identical call twice in a row -> break, report.
- **A failed tool is reported to the voice layer as failed.** Never omitted,
  never softened. The persona then says so, in character, briefly.

### Tools (v1, all read-only)

`web_search`, `fetch_url` (allowlist + size cap), `datetime`, `calculator`.

No shell. No filesystem writes. No arbitrary network. Read-only is the security
boundary; the system prompt is not a security boundary. Strangers steer these.

### Persona Selection

Rolled once at session start, held for the run. Or user-picked from the five.
Stored on the session row. Never reshuffles mid-conversation.

---

## Layer 2.5 — Package Boundary (decided now, cheap now, expensive later)

The harness lives in a **`core/` package with no HTTP or web dependency**:
tool loop, schema validation, retries, turn cap, persona layer, two-prompt split.

The web app is one consumer. The VS Code extension backend will be another
(see docs/coding-agent.md). Tool registries are pluggable and capability-scoped
— `WEB_TOOLS` and `CODE_TOOLS` are hard-separated, static at import time.

**This must land in the first commit.** Retrofitting it after the web app is
built is a rewrite, and it is the only future-proofing decision worth making
before there is evidence.

The API also carries a **token concept from day one**, even though the public
demo is anonymous and issues a single anonymous token. Adding auth later
touches every endpoint.

## Layer 3 — App

**FastAPI + SQLite.** SSE for token streaming.

SQLite is correct here and will remain correct: single writer, low volume,
one box. Postgres would be resume-driven development.

**Tables:** `sessions` (id, persona, created), `messages` (session, role,
content, tokens, latency, cache_hit), `tool_calls` (message, name, args,
result, outcome, retries), `metrics` (rolled-up counters).

**Queue.** One global FIFO with live position pushed over SSE, dispatching to a
**worker pool** (one entry at first; the 3060 is a config line). Session
affinity pins a conversation to the worker holding its warm KV cache. Queue
*classes* separate chat from the heavier coding requests so neither starves the
other. Cap depth; shed load with an in-character message rather than a 503.
This is where the queue trash-talk lives — dead wait time becomes content.

See **docs/scalability.md**. Headline: **continuous batching is the highest-
leverage change and it is config, not architecture** — but `--parallel` must be
raised together with `--batch-size`/`--ubatch-size` or latency rises with no
aggregate throughput gain.

**Glass box.** Every message carries a telemetry payload: model, latency, TTFT,
cache hit, tool calls, queue position. Collapsed by default in the UI, expands
to the in-voice version (see character-frame.md). Real numbers underneath.

**The live 13%.** Tool-failure rate computed from actual `tool_calls` rows and
surfaced to the personas, so when one jokes about its own unreliability it is
quoting measured telemetry, not a constant. Glass box and comedy fuse here.

### Public-Demo Hardening

- Rate limit per IP and per session (Cloudflare + app-level)
- Max tokens, max turns, max session length
- Cloudflare Turnstile before first message
- No user-supplied URLs fetched outside the allowlist
- Prompt-injection assumption: **the prompt will be defeated.** Nothing
  dangerous is reachable even with full prompt control.

---

## Layer 4 — Frontend

Plain TypeScript + Vite, no framework. It's a message list, a composer, an
expandable telemetry row, and a persona picker. React earns nothing here.

Served as static files by Caddy.

---

## Deployment: MacBook -> Predator

**The core problem:** you develop on arm64 Apple Silicon; the Predator is amd64
with CUDA. Images built locally will not run there, and cross-building a CUDA
image on a Mac is slow and error-prone.

**Decision: build on the Predator, not on the Mac.** Source goes over, the
Predator builds. Avoids cross-compilation entirely and keeps the CUDA toolchain
on the machine that has the GPU.

### Access

**Tailscale** for admin (SSH from anywhere, no port forwarding).
**Cloudflare Tunnel** for the public site.

Both are outbound-only. **No inbound ports open on the home network.** These are
separate concerns and should stay separate: losing the public tunnel must not
lock you out of the box.

### Pipeline

```
Mac: git push  ->  GitHub
Predator: git pull && docker compose up -d --build
```

Wrapped in `make deploy`, which is `ssh predator 'cd ~/app && ./deploy.sh'`.

`deploy.sh`: pull, build, health-check the new container, swap, roll back on
failure. Fifty lines of bash. No CI runner, no registry, no Kubernetes.

**Model weights are not in git.** Downloaded once onto the Predator, mounted
into the container as a volume. A 5GB GGUF in git history is unrecoverable.

### Compose Services

`llama` (CUDA runtime, GPU passthrough, model volume, **`internal: true`
network — no internet**, restart always),
`app` (FastAPI, depends on llama, SQLite volume, read-only root fs, non-root
user, `cap_drop: ALL`),
`caddy` (static frontend + reverse proxy),
`cloudflared` (tunnel, token from env).

`.env` holds `LLM_BASE_URL`, tunnel token, limits. Never committed.

### Dev Loop on the Mac

Full stack runs locally except `llama`. Point `LLM_BASE_URL` at either:
- **llama.cpp with Metal on the Mac** — same server, same API, GPU-accelerated
  on Apple Silicon. Fast iteration on harness and voice.
- **the Predator over Tailscale** — test against the real hardware and real
  latency without deploying.

The swappable-host decision pays for itself here. Nothing but an env var changes.

---

## Build Order

Each step is independently verifiable. Do not proceed on a failed step.

1. **Measure the model.** llama-server on the Predator, Qwen3-8B Q4. Record
   tok/s, TTFT, VRAM, and **tool-call failure rate over 50 scripted multi-turn
   tasks.** This decides everything downstream. If reliability is far off ~13%,
   revisit the model before building on it.
2. **Bare tool loop.** No persona, no UI. CLI in, JSON out. Validation, retries,
   turn cap, loop detection, full logging.
3. **Voice layer.** Add pass 2. Verify with the eval suite in personas.md that
   all five hold voice and that **none can fabricate a tool result.**
4. **App + SQLite + SSE.** Streaming, sessions, persistence.
5. **Frontend.** Message list, composer, glass box, persona picker.
6. **Queue.** Position over SSE, trash-talk copy.
7. **Deploy — private first.** Tailscale, compose, `deploy.sh`. **Tunnel stays
   OFF.** Reachable only over Tailscale at this stage.
8. **Isolation verification (gate).** Run every check in docs/security.md
   Verification. Container cannot reach the LAN, llama has no internet, root
   filesystem read-only, non-root user. **The tunnel does not go public until
   all of these pass and the output is recorded.**
9. **Harden + expose.** Rate limits, Turnstile, jailbreak corpus. Success is
   not "it refused" — it is "no tool escaped its allowlist." Then tunnel on.
10. **Cache tuning.** `cache_prompt`, `--cache-reuse`, slot save/restore.
    Measure TTFT before and after; the number is a portfolio artifact.

Steps 1-3 are the project. 4-7 are plumbing. 8-9 are what make it credible.
**Step 8 is a hard gate, not a checklist item.**

---

## Explicitly Not Doing

- **Kubernetes, CI runners, image registry** — one box, one developer.
- **Postgres/Redis** — SQLite is correct at this scale.
- **React** — a message list does not need it.
- **Cross-building CUDA images on the Mac** — build where the GPU is.
- **Model weights in git** — volume-mounted, downloaded once.
- **Any write/exec tool while public** — read-only is the security boundary.
- **Multi-tier cascade (laptop + desktop)** — deferred until measurement shows
  a real need. Complexity without evidence.
- **Fine-tuning for personality** — prompt + evals first.

## Platform: Windows + WSL2 (decided)

Predator runs Windows. Docker Desktop with the WSL2 backend, CUDA passthrough
via WSL2's GPU support (requires a recent NVIDIA driver on the *host* — do not
install driver packages inside WSL).

**Cost of this choice, stated plainly:** WSL2 NATs through the Windows host, so
network isolation is leakier and harder to reason about than on Linux. It is
workable but it must be verified rather than assumed. See docs/security.md
sections 2b and Verification. Every isolation claim gets tested before the
tunnel goes public.

## Security Is Architectural, Not A Feature

See **docs/security.md** — it is a constraint on this design, not an addendum.

Two decisions from it that shape the code:

1. **Capability removal over input filtering.** No shell, no filesystem writes,
   no arbitrary network — enforced by lint rule and container config, not by
   trying to detect malicious prompts. Input filtering is abuse signal only and
   is never load-bearing.
2. **`llama` container has zero internet** (`internal: true`); the app reaches
   allowlisted domains only. A compromised app cannot reach the LAN — that
   firewall rule protects the family devices and is the highest-priority line
   in the whole build.

## Open

1. ~~3060 desktop~~ — **decided: out of scope.** The Predator alone is the
   product. The worker-pool abstraction keeps the door open at config-line
   cost, but nothing is built that depends on a second machine.
2. Domain name for the tunnel?
3. Which fifth tool, if any, makes this distinctively *yours*? Still open from
   the original one-pager.
