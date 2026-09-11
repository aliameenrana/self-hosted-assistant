# Scalability & Distribution

*Written 2026-09-11.*

## Scope: One Laptop

**The Predator alone is the product.** Every number, flag, and design decision
below assumes a single machine: GTX 1060 6GB, Windows, WSL2.

The 3060 desktop is an **optional future worker, never a dependency.** The
worker-pool abstraction exists so adding it is a config line — but if it never
happens, nothing breaks and no design changes. Do not build anything that
requires two machines.

## The Honest Ceiling

**One GPU means one stream of compute.** If a response takes ~8s and ten people
arrive at once, someone waits ~80s. No amount of async, worker processes, or
load balancing changes that — they all contend for the same silicon.

So this document is not "how to scale to 1000 users." It is four things, in
descending order of value:

1. **Serve more per GPU** (batching) — the real multiplier
2. **Don't call the model** (caching, tiering) — the cheapest win
3. **Add GPUs** (distribution) — real scaling, but OPTIONAL and future
4. **Degrade gracefully** (queue, load shed) — what happens past the ceiling

Anything else is complexity without evidence.

---

## 1. Serve More Per GPU — Continuous Batching

llama-server merges decode steps for different requests into the same forward
pass. Measured ~3.8x throughput over sequential decode on a single T4.

**This is the single highest-leverage change in the document, and it's config.**

### The trap

`--parallel` alone does nothing good. Raising slot count without also raising
prefill throughput just spreads the same tok/s across more queues: latency up,
aggregate throughput flat. **These three flags are one system:**

```
--parallel N        # concurrent slots
--batch-size        # prefill tokens per pass
--ubatch-size       # micro-batch
--cache-reuse       # collapse repeated prefill on the stable system prompt
```

### Sizing on the 1060 (6GB) — the target

KV cache is per-slot, so slots cost VRAM. On a 6GB card already running a
hybrid-offloaded 8B model, VRAM is the binding constraint — not CPU.

**Start at `--parallel 2` and measure.** Raise only while aggregate tok/s
improves. Expect a low ceiling (2-4) — KV cache per slot is the binding
constraint on 6GB, and the model is already partially offloaded to system RAM.

**Do not guess these numbers. Benchmark them** (see Measurement below).

---

## 2. Don't Call The Model

Cheapest request is the one that never reaches the GPU.

### 2a. Prompt cache (already in architecture.md)
`cache_prompt` + `--cache-reuse` + slot save/restore. The system prompt and tool
definitions are identical across every request — never reprocess them.

**Pin sessions to slots** via `id_slot`. A returning message in the same
conversation hits a warm KV cache instead of re-prefilling history. This is
worth more than any other single optimization for multi-turn chat.

### 2b. Exact-match cache
Hash of (persona + normalized message) -> stored response, for cold-open
questions with no history. "what is this", "who made you", "what can you do".
On a public demo these are a large fraction of first messages.

SQLite table, TTL, in-voice per persona (cache key includes persona).

### 2c. Semantic cache (only if 2b proves insufficient)
Embedding similarity over recent Q/A. Adds an embedding model and a failure
mode (wrong-but-similar answers). **Not in v1.** Requires evidence.

### 2d. Tier-1 routing (deferred)
A 1-3B model handling greetings, refusals, persona small-talk, and routing —
never tool calls, never real answers. On a single laptop this would share the
same GPU, so the win is smaller than it looks (less VRAM for the main model's
KV cache). Probably not worth it on one machine.

**Gate:** only build this if measurement shows >30% of traffic is trivially
routable. Instrument first (log an intent classification per message), decide
later.

---

## 3. Add GPUs — Distribution

This is where real scaling lives, and the architecture already supports it
because the app only knows `LLM_BASE_URL`.

### Worker pool

Generalize the single URL into a pool:

```
workers:
  - url: http://llama-predator:8080   slots: 2   weight: 1
  - url: http://desktop-3060:8080     slots: 6   weight: 3
```

- App holds one **global FIFO queue**.
- Dispatcher assigns to the worker with free slot capacity, weighted by speed.
- **Session affinity:** a conversation sticks to the worker holding its warm KV
  cache. Only move it if that worker is down. Affinity beats load balance here
  — a cache miss costs more than a slightly uneven queue.
- Health check per worker; drop from pool on failure, re-add on recovery.
- Worker set is config. Adding the 3060 is a config line, not a code change.

### Why one queue, not per-worker queues

Per-worker queues produce the supermarket problem — one line stalls behind a
long request while another is idle. Single queue, workers pull when free.

### Realistic capacity

**Single laptop (the actual target):**

| Setup | Concurrent (comfortable) | Notes |
|---|---|---|
| 1060, no batching | 1 | baseline |
| 1060, `--parallel 2` | 2-3 | measure before believing |
| + cache hits (~30%) | 3-5 | effective, not raw |

**Ten simultaneous strangers WILL queue on one laptop.** Some will wait 30s+.
This is the real ceiling and the design accepts it: queue with live position,
in-character trash-talk, load shed past a depth cap, streaming from first token.

The failure mode to avoid is not slowness — it is a silent spinner. Slow and
talkative beats fast-looking and stuck.

*Optional, if the 3060 is ever added as worker #2: ~6-10 concurrent. Treat as
upside, never as the plan.*

---

## 4. Degrade Gracefully

The ceiling will be hit. Design for it.

- **Queue with live position** over SSE. Already designed — the trash-talk
  turns wait time into content, which is a genuine advantage here.
- **Depth cap.** Past N waiting, shed load with an in-character message rather
  than a 503 or an infinite spinner.
- **Per-session token budget** so one user can't monopolize a slot.
- **Timeout + requeue** if a worker stalls.
- **Streaming from first token** — perceived latency is what people feel.
  TTFT matters more than total time.
- **Under heavy load, drop to a smaller model** rather than a longer queue.
  Slightly worse answers beat 90-second waits. Announce it in voice:
  *"You've got the fast one today. There's a crowd."*

---

## What We Are NOT Building

- **Kubernetes / autoscaling** — nothing to autoscale. GPUs are physical.
- **Redis** — the queue is in-process. One app instance. SQLite for state.
- **Multiple app replicas** — the app is not the bottleneck; the GPU is.
  Replicas would contend for the same workers and complicate affinity.
- **A message broker** — a Python `asyncio.Queue` is sufficient and auditable.
- **Speculative decoding** — real technique, real gains, but needs a draft
  model and VRAM we don't have on a 6GB card. Revisit on the 3060.

Every one of these becomes correct at some scale. None is correct at ten users.

---

## Measurement (do this before tuning anything)

Tuning without numbers is guessing. Establish baselines first.

1. **Single-request baseline:** TTFT, tok/s, total latency. Both GPUs.
2. **Batching sweep:** `--parallel` 1,2,3,4 x `--batch-size` variations.
   Record aggregate tok/s AND p95 latency. Pick the knee, not the peak.
3. **Load test:** 1, 5, 10, 20 concurrent simulated users. Record p50/p95/p99
   and queue depth. This produces the real capacity number.
4. **Cache effectiveness:** hit rate for prompt cache and exact-match cache
   against replayed real traffic.
5. **Re-run after every change.** A regression you didn't measure is a
   regression you shipped.

**These numbers are a portfolio artifact.** "I measured 3.8x from batching and
cut TTFT 60% with prefix caching" is worth more than any framework name.

---

## Build Order (fits into architecture.md)

Do these in order; each is independently verifiable.

1. Batching flags + measurement sweep — **highest value, lowest effort**
2. Slot pinning for session affinity (warm KV on follow-ups)
3. Queue + position + load shed
4. Exact-match cache
5. Worker pool abstraction (single worker in the pool at first)
7. Instrument intent classification; decide on tier-1 routing from data

*(Optional, later: add the 3060 as worker #2. Not part of the plan.)*
