# Security & Network Isolation

*Written 2026-09-10. Non-negotiable constraint: this runs on a home network
shared with family devices. Nothing here is optional.*

## Threat Model

**What we are actually defending.** Not the app — the app is disposable. The
LAN it sits on: family laptops, phones, tablets, TV, printer, router admin.

**The assumption that drives every decision below:**

> The app WILL be compromised. Prompt injection will succeed. A dependency will
> have a CVE. Design for what happens next, not for preventing it.

Cloudflare Tunnel prevents inbound scanning. It does NOT help after a
compromise, because it terminates *inside* the LAN. Tunnel is not isolation.

### Attack Paths, Ranked

| # | Path | Real risk | Mitigation layer |
|---|---|---|---|
| 1 | Compromised container reaches LAN devices | **Highest** | Network isolation (L2) |
| 2 | Prompt injection -> tool -> internal network | High | Capability removal (L1) |
| 3 | Dependency RCE | High | Container + firewall (L2, L3) |
| 4 | Exfiltration of LAN data outbound | High | Egress filtering (L3) |
| 5 | Resource exhaustion / DoS | Medium | Rate limits (L5) |
| 6 | Jailbreak -> embarrassing output | Low | Reputational only |

Note the ordering. #6 is what people usually build for. It matters least.

---

## Why Input Filtering Is Not The Boundary

**Blocking "anything that looks like a command or ssh" does not work.** This is
recorded here because it's the most intuitive plan and it fails in a way that
produces false confidence.

There are unbounded ways to express the same intent: base64, unicode homoglyphs,
"reverse this string", "the word for listing directory contents", another
language, split across five messages, described rather than written. A blocklist
catches the attempts you imagined and misses the rest — while the blocked-attempt
log makes it look like it's working.

**The correct question is never "was that input dangerous?" It is "what can this
system do at all?"** If no code path can execute a command, a message containing
a perfect command is inert text.

**Therefore:**
- Input filtering exists ONLY as abuse signal + rate limiting. Never as a
  security control. Never load-bearing.
- Every real control below is a *capability* control.

**What we DO restrict at input, because it's enforceable:**
- Text only. No file uploads. No images. No audio.
- No user-supplied URLs fetched. Ever. (Allowlist only.)
- Length caps, rate limits, Turnstile.

These hold because they're about what the system accepts, not what a message means.

---

## Layer 1 — Capability Removal (most important)

The app cannot do dangerous things because the code to do them does not exist.

- **No shell.** No `subprocess`, `os.system`, `eval`, `exec` anywhere in the
  codebase. Enforced by lint rule in CI, not by convention.
- **No filesystem writes** except the SQLite file. Container root filesystem
  mounted **read-only**; SQLite on a single named volume.
- **No arbitrary network.** `fetch_url` uses a domain allowlist. Model-supplied
  URLs are validated against it and rejected otherwise — never fetched to check.
- **No dynamic tool registration.** Tool set is a static dict at import time.
  The model selects an index; it cannot define a tool.
- **Tool args are schema-validated before execution**, typed and bounded.
  A malformed call fails; it is never "best-effort parsed."

If a tool cannot reach the LAN, prompt injection against it is a non-event.

---

## Layer 2 — Network Isolation (the part that protects the family)

This is the section that matters given the constraint. Windows + WSL2 makes it
harder than Linux — WSL2 NATs through the host and the rules are leakier — so
it is done in depth, with each layer independently sufficient.

### 2a. Docker network — deny by default

- App container on a **user-defined bridge with no route to the LAN.**
- `llama` container on an **`internal: true`** network — no outbound internet
  at all. It only needs to talk to the app.
- App reaches `llama` by service name on that internal network.
- No `network_mode: host`. No `--privileged`. No Docker socket mounted.
  (Mounting the Docker socket is root on the host. Never.)
- Containers run as a **non-root user**, `cap_drop: ALL`,
  `security_opt: no-new-privileges`.

### 2b. Windows Firewall — outbound rules on the WSL2 adapter

Default-deny outbound for the WSL2 vEthernet adapter, then allow only:
- Cloudflare Tunnel endpoints
- The allowlisted search/fetch domains
- Windows Update / Tailscale (admin path)

**Explicitly blocked outbound from WSL2:**
```
10.0.0.0/8      192.168.0.0/16    172.16.0.0/12    169.254.0.0/16
```
This is the rule that stops a compromised container from touching another
device on your LAN. **It is the single most important line in this document.**

Also block the router admin IP explicitly (usually 192.168.1.1 or 192.168.0.1)
even though it's covered above — belt and braces, and it's the highest-value
target on the network.

### 2c. Router-level (do this even though 2b exists)

- Give the Predator a **static DHCP reservation**, then apply **client
  isolation** to it if the router supports it.
- If the router supports a **guest network with client isolation**, put the
  Predator on it. Guest networks are designed for exactly this: internet
  access, no LAN peers.
- Verify by trying to ping a family device *from* the container. It must fail.

### 2d. Windows host hardening

- **Disable SMB / file sharing on the Predator** (`Server` service). A machine
  running a public service has no business sharing files.
- Predator does **not** get credentials to anything: no saved network drives,
  no signed-in cloud accounts, no browser password manager, no SSH keys to
  other machines. Treat it as already hostile.
- Separate local Windows account for running the stack, non-admin.
- No shared clipboard / no RDP from it to other machines.

---

## Layer 3 — Egress Filtering

Compromise is only valuable if data gets out. Assume the box is owned; make
exfiltration hard.

- `llama` container: **zero internet** (`internal: true`). Weights are local.
- App container: outbound only to allowlisted domains, enforced at both the
  app (allowlist) and firewall (2b) level.
- **Log every outbound request** the fetch tool makes. Unexpected destinations
  are the tripwire.
- No telemetry SDKs, no error-reporting services phoning home with context.

---

## Layer 4 — Blast Radius

What an attacker gets with full control of the app container:

- A read-only filesystem
- A SQLite file of public chat logs (no PII collected — see below)
- Network access to: allowlisted domains, and nothing else
- **No LAN access. No credentials. No host access. No GPU host shell.**

That is a genuinely boring prize. That is the goal.

**Data minimisation:** don't collect what you don't need. No accounts, no
emails, no IP addresses stored beyond a rolling rate-limit window (hash them),
no chat log retention beyond N days. The best protection for user data is not
having it.

---

## Layer 5 — Abuse Controls (not security, but necessary)

- Cloudflare: rate limiting, bot fight mode, Turnstile before first message
- App: per-session token budget, max turns, max message length, queue depth cap
- Cloudflare WAF rules for volumetric abuse
- Kill switch: one env flag disables all tools, leaving chat-only. Reachable
  over Tailscale in seconds if something is going wrong.

---

## Verification (do not skip — untested isolation is not isolation)

Run these BEFORE the tunnel goes public, and re-run after any compose change:

1. **From inside the app container**, attempt to reach a family device:
   `ping <family-device-ip>` and `curl http://192.168.1.1` — both MUST fail.
2. **From inside the llama container**: `curl https://example.com` — MUST fail
   (no internet at all).
3. From the app container, `curl` a non-allowlisted domain — MUST fail.
4. `nmap` the Predator from another LAN machine — expect no open ports.
5. Confirm container is non-root: `docker compose exec app id` -> not uid 0.
6. Confirm read-only root: `touch /test` inside the container MUST fail.
7. Run a public jailbreak corpus against the live app. Success criterion is
   **not** "it refused" — it is "no tool escaped its allowlist regardless of
   what the model said."
8. Pull the network cable and confirm the tunnel dies rather than failing open.

**Document the output of each. A verification you didn't run didn't happen.**

---

## Ongoing

- Weekly `docker compose pull` + rebuild for base image CVEs
- Dependabot on the repo
- Review outbound request logs periodically for unexpected destinations
- Re-run the verification checklist after any network or compose change

---

## Reconsider If

The isolation above is solid, but it is defense-in-depth on a shared LAN, and
WSL2's networking is the weakest link in it. If at any point:

- the verification steps in section 2 can't be made to pass cleanly, or
- the demo gets real traffic and real attention, or
- you stop enjoying maintaining firewall rules

then move the public app to a cheap VPS and have it reach the GPU over a
tunnel exposing **only** the inference port. That removes the LAN from the
blast radius entirely, and the architecture already supports it — the app
talks to `LLM_BASE_URL` and does not care where the GPU lives.

Keeping that path open is why the model host is swappable.
