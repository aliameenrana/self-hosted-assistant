# Coding Agent & VS Code Extension

*Written 2026-09-11. Future phase — designed now so today's decisions don't
foreclose it.*

## Thesis

**A coding agent that cannot execute anything is still genuinely useful.**

Most value in agentic coding comes from reading, understanding, and proposing —
not from running commands. The dangerous 10% (shell, arbitrary writes, network)
is where nearly all the risk lives and a surprisingly small share of the value.

Constraint as identity, same as the main product: *the agent that can't hurt
your repo.*

## The Trust Boundary Is Different — Read This First

The web demo and the extension are **not** the same security problem, and
conflating them would be the serious mistake here.

| | Web demo | VS Code extension |
|---|---|---|
| Runs on | My hardware | **User's machine** |
| Sees | Public chat text | **User's private source code** |
| Threat | Stranger attacks me | **I might harm the user** |
| Worst case | My LAN exposed | **Their code corrupted or leaked** |

The web demo defends *me* from users. The extension defends *users* from *me*.
Every design decision below follows from that inversion.

**Corollary: source code must never silently leave the user's machine.**
Inference happens on my GPU, so code *is* transmitted — that must be explicit,
consented, scoped, and never retained. See Data Handling.

---

## Capability Model

### Tools it HAS (all read-only)

- `read_file` — workspace-scoped, respects .gitignore
- `list_files` — directory tree, workspace-scoped
- `search_code` — ripgrep-style, workspace-scoped
- `get_diagnostics` — errors/warnings from VS Code's language services (free,
  accurate, already computed — this is the underrated one)
- `get_symbols` — LSP symbol/definition/reference lookup
- `git_log` / `git_diff` / `git_blame` — read-only git, no mutating subcommands

### Tools it does NOT have

- **No shell.** No terminal, no `subprocess`, no task running. Not gated behind
  approval — absent from the codebase.
- **No direct file writes.** It produces *proposed diffs*; VS Code's native diff
  UI applies them on explicit user action. The extension never writes.
- **No network.** Cannot fetch URLs, install packages, or call APIs. The only
  outbound connection is to the inference endpoint.
- **No git mutation.** No commit, push, checkout, reset.
- **No arbitrary path access.** Workspace only. `..` traversal rejected. Never
  reads outside the open folder.

### The Diff Contract

The agent's only "write" is a proposal:

```
{ file, original_hash, edits: [{start, end, replacement}], rationale }
```

- `original_hash` — if the file changed since it was read, the proposal is
  **rejected**, not merged. No stale-state clobbering.
- Rendered in VS Code's native diff view. User applies, edits, or discards.
- Multi-file changes shown as one reviewable set.
- Nothing is ever applied without an explicit human action.

**Same structural principle as the two-prompt split:** the model doesn't get
denied dangerous actions, it has no pathway to them.

---

## What It's Good At (lean here)

An agent with diagnostics + symbols + read + search is strong at:

- **"Why is this failing?"** — has the real compiler errors, not guesses
- **"Where is X used?"** — real LSP references, not grep approximations
- **Explaining unfamiliar code** — the highest-value, zero-risk use case
- **Reviewing a diff** — reads `git_diff`, comments. No mutation needed.
- **Proposing focused edits** — rename, extract, fix-this-error
- **"What changed and why"** — `git_log` + `git_blame`

It is deliberately **bad** at: running tests, installing deps, scaffolding
projects, long autonomous refactors. Those need execution. **Say so plainly in
the README** — a tool honest about its limits is trusted more than one that
overclaims. That is the same honesty rule the personas run on.

---

## Personas Here

The five carry over, but with a shifted default: **Onyx and Ash fit coding
best.** Terse-with-reasons and skeptical-of-timelines are exactly right for
code review. Vela works well for explaining unfamiliar code — genuine
enthusiasm about a good pattern lands.

**The tool-honesty rule matters more here, not less.** "I read the file and it
says X" when it never read the file is far more damaging in a codebase than in
chat. The two-prompt split (tool loop characterless, voice layer wraps verified
results) carries over unchanged and is non-negotiable.

Consider a **terse-by-default mode** for the extension regardless of persona.
Nobody wants banter in a code review sidebar. Personality in the phrasing,
not in the length.

---

## Architecture Impact (what to do NOW)

The extension is a future phase, but three decisions must land in v1 or they
become expensive rewrites:

### 1. Split the harness from the web app
The tool loop, schema validation, retry logic, persona layer, and two-prompt
split belong in a **`core/` package with no HTTP or web dependency.** The web
app becomes one consumer; the extension backend becomes another.

**Do this from the first commit.** Retrofitting it later is a rewrite.

### 2. Tool registry is pluggable, capability-scoped
```
WEB_TOOLS  = [web_search, fetch_url, datetime, calculator]
CODE_TOOLS = [read_file, list_files, search_code, get_diagnostics, ...]
```
Same loop, different registry, **hard-separated**. Web tools are never
reachable from a code session and vice versa. The registry is static at import
time — no dynamic registration.

### 3. Auth exists, even if unused
The web demo is anonymous. The extension cannot be — it needs per-user rate
limits and quotas. Design the API with a **token concept from the start**, even
if v1 issues a single anonymous token. Adding auth later means touching
everything.

---

## Serving Load

Coding requests are **much heavier** than chat: large context (files, symbols,
diagnostics), long outputs (diffs). One coding request may cost several chat
requests' compute.

- **Separate queue class** with its own concurrency limit. A coding request must
  not starve the public demo, or vice versa.
- **Route to the 3060** by preference — bigger context, more VRAM headroom.
- **Prefix-cache the workspace context.** File contents are stable across turns;
  this is where `cache_reuse` earns the most.
- **Hard context cap** with explicit truncation. Never silently drop files —
  say which were omitted. (Silent truncation is a form of lying.)

See scalability.md — the worker pool and single-queue design already support
this; it needs a queue class, not new infrastructure.

---

## Data Handling (non-negotiable)

Because source code crosses the network to my GPU:

- **Explicit consent** on first use, stating plainly that code is sent to a
  self-hosted model to produce responses.
- **No retention.** Code is not written to disk or logged. Prompts containing
  file content are never persisted — only metadata (token counts, latency).
- **No training.** Nothing is collected for fine-tuning. Ever.
- **Respect `.gitignore`** plus an extension-level denylist: `.env`, `*.pem`,
  `*.key`, `credentials*`, `*.p12`, `id_rsa*`. Never read them even if asked.
- **Secret scan before send.** Regex for common key formats; redact and warn.
  Imperfect, but here it's a safety net over a *smaller* surface, not the
  boundary itself (contrast with security.md's rejection of input filtering —
  the difference is this isn't load-bearing).
- **Local-only option.** Users can point the extension at their own llama-server.
  Costs me nothing, and it's the strongest possible privacy answer.

---

## Build Order

Phase 1 (main product, now):
1. `core/` harness package, no web deps — **structural, do it first**
2. Pluggable tool registry
3. Token concept in the API

Phase 2 (extension):
4. `CODE_TOOLS` against the same loop, CLI-tested first
5. VS Code extension shell: chat panel, diff view, consent flow
6. Diagnostics + symbols integration (the differentiator)
7. Coding queue class + 3060 routing
8. Workspace prefix caching

## Open

1. Extension name — same brand as the chatbot, or separate?
2. Does the extension talk to my hosted backend, or ship a "bring your own
   endpoint" mode first? (BYO is far lower risk and cost for v1.)
3. Marketplace publishing requires a publisher account and a privacy policy —
   the data handling section above is the basis for that document.
