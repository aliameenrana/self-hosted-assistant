# Self-Hosted LLM Assistant — Concept One-Pager

*Refined 2026-09-10 via idea-refine. Status: awaiting confirmation before spec.*

## Problem Statement

**How might we** ship a self-hosted, personality-driven AI assistant on consumer hardware
that reliably uses tools, survives public internet exposure, and demonstrates real
engineering skill — where the constraint is the story, not the excuse?

## Correcting the Original Premise

| Original assumption | Reality |
|---|---|
| "Run Kimi, top few layers only" | Transformer layers are not independently useful. Truncation yields noise, not a smaller model. |
| "MoE means only active params in RAM" | MoE needs ALL params resident for expert routing. K2's 32B "active" = compute/token, not memory. ~250GB+ even quantized. |
| "8GB RAM is the constraint" | You have an RTX 3060 12GB. That is the real target machine. The 8GB laptop is tier 1, not the whole system. |

The legitimate technique resembling the original idea is `--n-cpu-moe`: attention on GPU,
expert FFN weights in system RAM. Real, useful — but still needs the full model available.

## Recommended Direction: Two-Tier Cascade + Glass Box

**Tier 1 — Predator laptop (GTX 1060 6GB, 8GB RAM budget).** Always-on front door.
A 4B dense model handles greetings, intent routing, refusals, and cache hits. Also
terminates the Cloudflare Tunnel. Cheap requests never wake the big model.

**Tier 2 — Desktop (RTX 3060 12GB).** The brain. Qwen3-14B Q4_K_M (~9GB VRAM,
~22 tok/s @ 16k ctx, ~678 t/s prompt processing) or Qwen3-8B for longer context and
higher throughput. Handles anything requiring tool use or real reasoning.

**The Glass Box.** The web UI exposes the harness: which tier served the request, cache
hit or miss, tokens/sec, VRAM, tool calls with arguments and results, retry attempts.
Visitors watch it think. For a portfolio piece the engineering IS the product — a
chat box alone shows nothing a hosted API couldn't.

**Personality** is a first-class artifact: a versioned character spec (voice, refusal
style, opinions, humor) evaluated by regression tests, not a paragraph of system prompt.

## Key Assumptions to Validate

- [ ] **Tool reliability clears the bar.** Qwen3-8B ~13% tool-call failure; Llama-3.2-3B ~81%.
      Test: 50 scripted multi-turn tool tasks against Qwen3-8B and 14B, measure failure
      rate and classify modes (loop / hallucinated success / malformed args). Do this FIRST —
      it decides model and harness.
- [ ] **Cascade actually saves work.** Test: log 200 real messages, measure what fraction
      tier 1 could serve alone. If under ~30%, collapse to one tier and drop the complexity.
- [ ] **KV cache reuse delivers the latency win.** Test: benchmark TTFT with and without
      `cache_prompt` + `--cache-reuse` + slot save/restore, with the full system prompt and
      tool definitions loaded. Expect a large, permanent cut.
- [ ] **Two machines stay reachable.** Test: leave the tunnel up 72h, measure uptime,
      reconnects, and behavior when the desktop sleeps.
- [ ] **Public exposure survives contact.** Test: run a public jailbreak corpus against it
      and confirm no tool escapes its allowlist. Assume the prompt WILL be defeated.

## MVP Scope

**In:**
- llama.cpp (`llama-server`) on the 3060, Qwen3-8B or 14B Q4_K_M, OpenAI-compatible API
- Own thin agent harness: tool loop, schema validation, retry-on-malformed-call, hard turn cap
- 4-6 read-only tools: web search, time/date, calculator, weather, one distinctive tool
- Cloudflare Tunnel + Access, rate limiting, per-session quotas, request queue with position display
- KV cache reuse configured and measured
- Glass-box UI showing tiering, cache, timings, tool calls
- Structured analytics: latency percentiles, cache hit rate, tool success rate, queue depth
- Versioned personality spec + a small eval suite that catches drift

**Out (v1):** the tier-1 laptop cascade (add once tier 2 is proven), RAG over personal
files, voice, image input, multi-user accounts, fine-tuning.

## Not Doing (and Why)

- **Kimi K2 / any 100B+ model** — physically impossible on this hardware. Not a tuning problem.
- **Layer truncation** — produces noise. The premise doesn't hold.
- **Ollama as the serving layer** — llama.cpp directly gives the cache flags and slot control
  that the whole latency strategy depends on. (Ollama's tool-call translation is genuinely
  good; revisit if the harness fights us.)
- **Any write/exec tool while public** — anonymous strangers steering tool calls on home
  hardware. Read-only allowlist is the security boundary; the system prompt is not.
- **Concurrency beyond a queue** — one GPU serializes. Honest queue feedback beats a
  demo that looks hung.
- **Presaved canned answers as the primary caching strategy** — KV cache reuse is the
  larger and more general win. Semantic cache for repeat questions layers on top later.
- **Fine-tuning for personality** — prompt + eval suite first. Fine-tune only if evals prove
  prompting insufficient.

## Open Questions

1. What is the distinctive tool that makes this *yours* rather than a generic chatbot?
   This is the single biggest differentiator for a portfolio piece and it's still blank.
2. Is the desktop always-on, or does the laptop need to serve alone at times?
   (Determines whether the cascade is an optimization or a hard requirement.)
3. What analytics matter to you — engagement metrics, or systems metrics?
4. Docker Compose or bare metal? Affects reproducibility for anyone reading the repo.
5. Does the public demo need a "safe mode" with tools disabled entirely as a fallback?

## Reference Evidence

- Tool-call failure rates: Qwen3-8B ~13%, Qwen3-4B ~15.7%, Llama-3.1-8B ~76.6%, Llama-3.2-3B ~81.6%
- Dense models outperform larger MoE at tool calling; identical weights behaved differently across runtimes
- RTX 3060 12GB: Qwen3-14B Q4 ~9GB / 22 t/s @16k; Qwen3-8B ~5.5GB / 52-65 t/s
- GTX 1060 6GB: 35B-A3B MoE at ~17 t/s via MoE offload (3 t/s unoptimized)
- CPU spillover costs ~70% decode speed even at 4 layers offloaded
