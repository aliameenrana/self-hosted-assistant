const $ = id => document.getElementById(id);
const log = $("log"), hello = $("hello"), scroll = $("scroll");
const input = $("q"), send = $("send"), who = $("who");

let session = null, persona = null, personas = [], me = null, busy = false;

const GREETINGS = {
  vex: ["you again", "Ask. I was not doing anything important."],
  wren: ["hello", "What are you actually trying to work out?"],
  onyx: ["yes", "Ask."],
  vela: ["oh good, someone", "Bring me something that is not boring."],
  ash: ["morning", "Let me guess. It worked on your machine."],
};

const at = () => scroll.scrollTo(0, scroll.scrollHeight);

async function boot() {
  [personas, me] = await Promise.all([
    fetch("/api/personas").then(r => r.json()),
    fetch("/api/me").then(r => r.json()),
  ]);
  buildPicker();
  renderAccount();
  await loadThreads();
  showWelcome();
}

function renderAccount() {
  $("acct").innerHTML = me.signed_in
    ? `${me.email || "signed in"}<br><form method="post" action="/auth/logout"><button>sign out</button></form>`
    : me.google_available
      ? `history is kept on this browser.<br><a href="/auth/google">sign in with Google</a> to keep it everywhere.`
      : `history is kept on this browser only.`;
}

async function loadThreads() {
  const rows = await fetch("/api/history").then(r => r.json());
  $("threads").innerHTML = "";
  for (const t of rows) {
    const d = document.createElement("div");
    d.className = "thread" + (t.id === session ? " on" : "");
    d.innerHTML = `${escape_(t.title || "untitled")}<small>${t.persona}</small>`;
    d.onclick = () => openThread(t.id);
    $("threads").append(d);
  }
}

const escape_ = s => s.replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));

function showWelcome() {
  log.innerHTML = "";
  hello.innerHTML = `<h2>five of them. one of you.</h2>
    <p>Pick who you want, or just start typing and get whoever is around.
    They will not flatter you and they will not pretend to have used a tool they did not.</p>
    <div class="cards"></div>`;
  const cards = hello.querySelector(".cards");
  for (const p of personas) {
    const b = document.createElement("button");
    b.className = "card";
    b.innerHTML = `<b>${p.name}</b><span>${p.tagline}</span>`;
    b.onclick = () => { persona = p.key; greet(p); };
    cards.append(b);
  }
}

function greet(p) {
  hello.innerHTML = "";
  log.innerHTML = "";
  who.innerHTML = `talking to <b>${p.name}</b>`;
  const [, line] = GREETINGS[p.key] || ["", "Go on then."];
  add("bot", line);
  input.focus();
}

function add(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  log.append(d);
  at();
  return d;
}

function note(text) {
  const d = document.createElement("div");
  d.className = "sysnote";
  d.textContent = text;
  log.append(d);
}

function buildPicker() {
  const list = $("picklist");
  list.innerHTML = "";
  for (const p of personas) {
    const b = document.createElement("button");
    b.className = "card";
    b.innerHTML = `<b>${p.name}</b><span>${p.tagline}</span>`;
    b.onclick = () => {
      $("pick").close();
      if (!session) { persona = p.key; greet(p); return; }
      persona = p.key;
      who.innerHTML = `talking to <b>${p.name}</b>`;
      note(`${p.name} took over.`);
      at();
    };
    list.append(b);
  }
}

async function openThread(id) {
  const d = await fetch(`/api/session/${id}`).then(r => r.json());
  session = id;
  persona = d.persona;
  hello.innerHTML = "";
  log.innerHTML = "";
  who.innerHTML = `talking to <b>${name_(d.persona)}</b>`;
  for (const m of d.messages) add(m.role === "user" ? "user" : "bot", m.content);
  await loadThreads();
  at();
}

const name_ = k => (personas.find(p => p.key === k) || {}).name || k;

function telemetry(node, t) {
  const d = document.createElement("details");
  const tools = t.tools.length
    ? t.tools.map(x => `  ${x.name} ${x.outcome}${x.retries ? ` retries:${x.retries}` : ""} ${x.ms}ms`).join("\n")
    : "  none. answered from memory.";
  const c = t.context || {};
  const ctx = `  ${Math.ceil((c.recent || 0) / 2)} recent exchanges, ${c.entities || 0} pinned details, ` +
    `${c.facts || 0} things known about you${c.summary ? ", plus a summary" : ""}`;
  const flags = t.flags.length
    ? `\n\nFLAGGED ${t.flags.join(", ")}\n  it claimed a tool it never called. telemetry disagrees.`
    : "";
  d.innerHTML = `<summary>what happened</summary><div class="tel${t.flags.length ? " flag" : ""}"></div>`;
  d.querySelector("div").textContent =
    `model  ${t.model}\nfirst token  ${t.ttft_ms}ms\ntotal  ${t.total_ms}ms\nloop turns  ${t.turns}\ncontext\n${ctx}\ntools\n${tools}${flags}`;
  node.append(d);
}

$("f").onsubmit = async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  busy = true;
  send.disabled = true;
  hello.innerHTML = "";
  add("user", text);
  const node = add("bot", "");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session, persona, message: text }),
    });
    if (!res.ok) {
      node.textContent = res.status === 503
        ? "queue is full. give it a minute." : "that failed on my end.";
      return;
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "", fresh = !session;
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
          node.textContent = `${ev.position} ahead of you. they are almost certainly asking something worse.`;
        else if (ev.type === "start") {
          session = ev.session;
          persona = ev.persona;
          who.innerHTML = `talking to <b>${name_(ev.persona)}</b>`;
          node.textContent = "";
        }
        else if (ev.type === "token") { node.textContent += ev.text; at(); }
        else if (ev.type === "error") node.textContent = ev.message;
        else if (ev.type === "done") { telemetry(node, ev.telemetry); if (fresh) loadThreads(); }
      }
    }
  } finally {
    busy = false;
    send.disabled = false;
    input.focus();
  }
};

$("switch").onclick = () => $("pick").showModal();
$("burger").onclick = () => $("side").classList.toggle("hidden");
$("newchat").onclick = () => { session = null; persona = null; who.textContent = ""; showWelcome(); loadThreads(); };
$("pick").onclick = e => { if (e.target.id === "pick") $("pick").close(); };

boot();
