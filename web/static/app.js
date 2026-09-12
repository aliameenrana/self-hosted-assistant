const $ = id => document.getElementById(id);
const log = $("log"), hello = $("hello"), scroll = $("scroll");
const input = $("q"), send = $("send"), who = $("who");

let session = null, me = null, busy = false, attachment = null;
let lastModel = null;

const SUGGESTIONS = [
  ["🧠", "explain something", "how does a bloom filter actually work"],
  ["🎨", "build a page", "make me a landing page for a coffee shop"],
  ["📄", "read a file", "attach a CV and ask what is weak"],
  ["🧮", "work a problem", "what is 17 percent of 4200"],
];

const at = () => scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" });
const esc = s => s.replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));

async function boot() {
  me = await fetch("/api/me").then(r => r.json());
  renderAccount();
  await loadThreads();
  showWelcome();
}

function renderAccount() {
  $("acct").innerHTML = me.signed_in
    ? `${esc(me.email || "signed in")}<br><form method="post" action="/auth/logout"><button>sign out</button></form>`
    : me.google_available
      ? `saved on this browser.<br><a href="/auth/google">sign in</a> to keep it everywhere.`
      : `saved on this browser only.`;
}

async function loadThreads() {
  const rows = await fetch("/api/history").then(r => r.json());
  $("threads").innerHTML = "";
  for (const t of rows) {
    const d = document.createElement("div");
    d.className = "thread" + (t.id === session ? " on" : "");
    d.textContent = t.title || "untitled";
    d.onclick = () => openThread(t.id);
    $("threads").append(d);
  }
}

function showWelcome() {
  log.innerHTML = "";
  who.textContent = "";
  hello.innerHTML = `<h2>ask me <em>anything</em></h2>
    <p>Runs on a gaming laptop in someone's house. It will not flatter you,
    and it will not claim to have used a tool it did not.</p><div class="cards"></div>`;
  const cards = hello.querySelector(".cards");
  for (const [icon, label, prompt] of SUGGESTIONS) {
    const b = document.createElement("button");
    b.className = "card";
    b.innerHTML = `<i>${icon}</i><b>${label}</b><span>${esc(prompt)}</span>`;
    b.onclick = () => { input.value = prompt; $("f").requestSubmit(); };
    cards.append(b);
  }
}

function turn(side, text) {
  const row = document.createElement("div");
  row.className = "turn " + side;
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = text;
  row.append(b);
  log.append(row);
  at();
  return { row, bubble: b };
}

function thinking(bubble) {
  bubble.innerHTML = `<span class="dots"><span></span><span></span><span></span></span>`;
}

function render(bubble, text) {
  const parts = text.split(/```(?:[\w-]*)\n?/);
  bubble.innerHTML = "";
  parts.forEach((part, i) => {
    if (i % 2) {
      const pre = document.createElement("pre");
      pre.textContent = part;
      bubble.append(pre);
    } else if (part) {
      bubble.append(document.createTextNode(part));
    }
  });
}

// The trace is a technical log. Rendered as short first-person lines so it
// reads as reasoning rather than a stack trace.
function readable(line) {
  let m;
  if ((m = line.match(/^turn (\d+): call (\w+)\((.*)\)$/))) {
    return `Turn ${m[1]}: I'll use ${m[2].replace(/_/g, " ")} with ${m[3] || "no arguments"}.`;
  }
  if ((m = line.match(/^  -> ok: (.*)$/))) {
    return `That worked. Result: ${m[1].slice(0, 140)}`;
  }
  if ((m = line.match(/^  -> (\w+): (.*)$/))) {
    return `That didn't work (${m[1]}): ${m[2].slice(0, 140)}`;
  }
  if ((m = line.match(/^turn (\d+): chose no tool(?: — (.*))?$/))) {
    return m[2] ? `Turn ${m[1]}: I don't need a tool. ${m[2].slice(0, 160)}`
                : `Turn ${m[1]}: No tool needed, answering directly.`;
  }
  if ((m = line.match(/^turn (\d+): hit the (\d+) token cap$/))) {
    return `Turn ${m[1]}: ran out of room (${m[2]} tokens) before finishing that thought.`;
  }
  if ((m = line.match(/^turn (\d+): refused in prose, forcing the tool$/))) {
    return `Turn ${m[1]}: I said I couldn't do that, which was wrong. Forcing myself to actually try.`;
  }
  if ((m = line.match(/^voice pass: (\d+) verified result\(s\) handed to the writer$/))) {
    return `Handing ${m[1]} verified result${m[1] === "1" ? "" : "s"} to the part of me that writes the reply.`;
  }
  return line;
}

const chip = (cls, text) => {
  const s = document.createElement("span");
  s.className = "chip " + cls;
  s.textContent = text;
  return s;
};

// Only what is notable. Repeating the model name and identical context counts
// on every turn buries the one line that actually differs.
function meta(row, t) {
  const bar = document.createElement("div");
  bar.className = "meta";

  for (const x of t.tools) {
    const label = x.outcome === "ok"
      ? x.name.replace(/_/g, " ")
      : `${x.name.replace(/_/g, " ")} ${x.outcome}`;
    bar.append(chip(x.outcome === "ok" ? "tool" : "flag", label));
  }
  for (const f of t.flags) bar.append(chip("flag", "claimed a tool it never used"));

  const secs = t.total_ms / 1000;
  if (secs > 12) bar.append(chip("slow", `${secs.toFixed(0)}s, it is thinking hard`));
  if (t.model !== lastModel) { bar.append(chip("", t.model)); lastModel = t.model; }

  const c = t.context || {};
  if (c.facts) bar.append(chip("mem", `${c.facts} things known about you`));

  const more = chip("more", "details");
  let open = null;
  more.onclick = () => {
    if (open) { open.remove(); open = null; return; }
    open = document.createElement("div");
    open.className = "detail";
    open.innerHTML = "";
    const stats = document.createElement("div");
    stats.textContent = [
      `model         ${t.model}`,
      `first token   ${t.ttft_ms}ms`,
      `total         ${t.total_ms}ms`,
      `tools offered ${(t.offered || []).join(", ") || "none"}`,
      `context       ${Math.ceil((c.recent || 0) / 2)} exchanges, ${c.entities || 0} pinned, ${c.facts || 0} facts${c.summary ? ", summarised" : ""}`,
    ].join("\n");
    open.append(stats);
    if ((t.trace || []).length) {
      const think = document.createElement("div");
      think.className = "think";
      think.innerHTML = "<b>thinking</b>";
      for (const line of t.trace) {
        const row = document.createElement("div");
        row.className = "think-line";
        row.textContent = readable(line);
        think.append(row);
      }
      open.append(think);
    }
    bar.after(open);
    at();
  };
  bar.append(more);
  row.append(bar);
  at();
}

function artifact(row, a) {
  const box = document.createElement("div");
  box.className = "artifact";
  box.innerHTML = `<div class="abar"><b></b><a target="_blank">open ↗</a></div>
    <iframe sandbox="allow-popups" loading="lazy"></iframe>`;
  box.querySelector("b").textContent = a.title;
  box.querySelector("a").href = a.url;
  box.querySelector("iframe").src = a.url;
  row.append(box);
  at();
}

async function openThread(id) {
  const d = await fetch(`/api/session/${id}`).then(r => r.json());
  session = id;
  hello.innerHTML = "";
  log.innerHTML = "";
  for (const m of d.messages) turn(m.role === "user" ? "you" : "them", m.content);
  await loadThreads();
}

$("file").onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  const box = $("attached");
  box.hidden = false;
  box.innerHTML = `reading <b>${esc(f.name)}</b>`;
  const body = new FormData();
  body.append("file", f);
  const res = await fetch("/api/upload", { method: "POST", body });
  const d = await res.json();
  if (!res.ok) {
    box.innerHTML = `<b>${esc(d.detail || "could not read that")}</b>`;
    attachment = null;
  } else {
    attachment = d.id;
    const size = d.pages ? `${d.pages} pages` : `${(d.chars / 1000).toFixed(1)}k chars`;
    box.innerHTML = `📄 <b>${esc(d.name)}</b> ${size}${d.truncated ? ", truncated" : ""}`;
    const x = document.createElement("button");
    x.textContent = "remove";
    x.onclick = clearAttachment;
    box.append(x);
    input.focus();
  }
  e.target.value = "";
};

function clearAttachment() {
  attachment = null;
  $("attached").hidden = true;
  $("attached").innerHTML = "";
  $("file").value = "";
}

$("f").onsubmit = async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  busy = true;
  send.disabled = true;
  hello.innerHTML = "";

  const label = attachment ? $("attached").querySelector("b").textContent : null;
  turn("you", label ? `📄 ${label}\n${text}` : text);
  clearAttachment();
  const { row, bubble } = turn("them", "");
  thinking(bubble);

  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ session, message: text, attachment }),
    });
    if (!res.ok) {
      bubble.textContent = res.status === 503
        ? "queue is full, give it a minute" : "that failed on my end";
      return;
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "", out = "", fresh = !session;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith("data: ")) continue;
        const ev = JSON.parse(part.slice(6));
        if (ev.type === "queued")
          bubble.textContent = `${ev.position} ahead of you, they are almost certainly asking something worse`;
        else if (ev.type === "start") { session = ev.session; bubble.textContent = ""; thinking(bubble); }
        else if (ev.type === "token") { out += ev.text; render(bubble, out); at(); }
        else if (ev.type === "error") bubble.textContent = ev.message;
        else if (ev.type === "done") {
          if (!out) bubble.textContent = "(nothing came back)";
          for (const a of ev.telemetry.artifacts || []) artifact(row, a);
          meta(row, ev.telemetry);
          if (fresh) loadThreads();
        }
      }
    }
  } finally {
    busy = false;
    send.disabled = false;
    input.focus();
  }
};

$("burger").onclick = () => $("side").classList.toggle("hidden");
$("newchat").onclick = () => { session = null; showWelcome(); loadThreads(); };

boot();
