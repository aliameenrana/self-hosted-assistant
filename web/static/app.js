const log = document.getElementById("log");
const form = document.getElementById("f");
const input = document.getElementById("q");
const send = document.getElementById("send");
const picker = document.getElementById("picker");
const who = document.getElementById("who");

let session = null;
let chosen = null;
let locked = false;

const personas = await (await fetch("/api/personas")).json();
for (const p of [...personas, { key: null, name: "surprise me", tagline: "" }]) {
  const b = document.createElement("button");
  b.textContent = p.name.toLowerCase();
  b.title = p.tagline;
  b.onclick = () => {
    if (locked) return;
    chosen = p.key;
    [...picker.children].forEach(c => c.setAttribute("aria-pressed", c === b));
  };
  picker.append(b);
}

function add(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  log.append(d);
  scroll();
  return d;
}

const scroll = () => window.scrollTo(0, document.body.scrollHeight);

function telemetry(node, t) {
  const d = document.createElement("details");
  const tools = t.tools.length
    ? t.tools.map(x => `${x.name} ${x.outcome}${x.retries ? ` (${x.retries} retries)` : ""} ${x.ms}ms`).join("\n")
    : "none. answered from memory.";
  const flags = t.flags.length
    ? `\n\nFLAGGED: ${t.flags.join(", ")}\nit claimed a tool it never called. telemetry says otherwise.`
    : "";
  d.innerHTML = `<summary>what happened</summary><div class="tel${t.flags.length ? " flag" : ""}"></div>`;
  d.querySelector("div").textContent =
    `model    ${t.model}\nfirst token  ${t.ttft_ms}ms\ntotal    ${t.total_ms}ms\nturns    ${t.turns}\ntools\n  ${tools.replace(/\n/g, "\n  ")}${flags}`;
  node.append(d);
}

form.onsubmit = async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;
  input.value = "";
  send.disabled = true;
  add("user", text);
  const node = add("bot", "");

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ session, persona: chosen, message: text }),
  });

  if (!res.ok) {
    node.textContent = res.status === 503 ? "queue is full. give it a minute." : "that failed.";
    send.disabled = false;
    return;
  }

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for (const part of parts) {
      if (!part.startsWith("data: ")) continue;
      const ev = JSON.parse(part.slice(6));
      if (ev.type === "queued") node.textContent = `${ev.position} ahead of you. they are almost certainly asking something worse.`;
      else if (ev.type === "start") {
        session = ev.session;
        locked = true;
        picker.style.display = "none";
        who.textContent = ev.persona;
        node.textContent = "";
      }
      else if (ev.type === "token") { node.textContent += ev.text; scroll(); }
      else if (ev.type === "error") node.textContent = ev.message;
      else if (ev.type === "done") telemetry(node, ev.telemetry);
    }
  }
  send.disabled = false;
  input.focus();
};
