"use strict";
// Browser side: one WebSocket (api/ws) brings decoded messages and receiver
// status, and carries settings changes back. Relative URLs, so the page
// also works behind a reverse proxy under a sub-path.

const $ = (id) => document.getElementById(id);
const MAX_SHOWN = 500;
const FUNC = "ABCD";
// ISO 646-SE: Swedish pagers send ÄÖÅäöå in place of [\]{|}
const SWEDISH = { "[": "Ä", "\\": "Ö", "]": "Å", "{": "ä", "|": "ö", "}": "å" };

let ws = null;
let config = {};
let messages = [];               // oldest first
let open = new Set();            // ids of messages showing details
let reconnectDelay = 1000;
let reconnectTimer = null;

// ---- connection --------------------------------------------------------------

function connect() {
  setConn("Connecting…");
  const url = new URL("api/ws", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(url);
  ws.onopen = () => { reconnectDelay = 1000; setConn("Connected", "ok"); };
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = () => scheduleReconnect();
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  setConn("Disconnected – retrying", "bad");
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 15000);
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  else toast("Not connected to the decoder");
}

function setConn(text, cls) {
  const el = $("conn");
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

function onMessage(m) {
  switch (m.type) {
    case "hello":
      messages = m.messages;
      setConfig(m.config);
      showStatus(m.status);
      renderList();
      break;
    case "message":
      messages.push(m.message);
      if (messages.length > MAX_SHOWN) messages.splice(0, messages.length - MAX_SHOWN);
      addToList(m.message);
      break;
    case "status": showStatus(m); break;
    case "config": setConfig(m.config); renderList(); break;
    case "error": toast(m.message); break;
  }
}

// ---- status ------------------------------------------------------------------

const STATES = { starting: "STARTING", listening: "LISTENING", receiving: "RECEIVING", error: "NO RECEIVER" };

function showStatus(s) {
  const st = $("state");
  st.textContent = STATES[s.state] || s.state || "—";
  st.className = "state" + (s.state === "receiving" ? " rx" : "");
  $("device").textContent = s.device ? `${s.device} · ${(s.sample_rate / 1000).toFixed(0)} kS/s` : "";
  $("freq").textContent = s.freq ? `${mhz(s.freq)} MHz` : " ";
  const hasLevel = typeof s.level_db === "number";
  $("level").textContent = hasLevel ? `Level ${s.level_db.toFixed(0)} dB` : " ";
  $("offset").textContent = typeof s.offset_hz === "number"
    ? `Offset ${s.offset_hz >= 0 ? "+" : ""}${(s.offset_hz / 1000).toFixed(1)} kHz` : "";
  // -90 dB (no signal) .. -10 dB (very strong)
  $("meterbar").style.width = hasLevel ? `${Math.max(0, Math.min(100, (s.level_db + 90) * 1.25))}%` : "0";
  $("stats").textContent = s.batches !== undefined
    ? `${s.messages} messages · ${s.batches} batches · ${s.corrected_bits} bits corrected · ${s.bad_codewords} bad codewords (since tuning)`
    : "";
  const err = $("error");
  err.hidden = !s.error;
  err.textContent = s.error || "";
}

function mhz(hz) { return (hz / 1e6).toFixed(4); }

// ---- settings ----------------------------------------------------------------

function setConfig(c) {
  config = c;
  const input = $("freqinput");
  if (document.activeElement !== input) input.value = mhz(c.freq);
  $("charset").value = c.charset;
}

$("freqform").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = parseFloat($("freqinput").value.replace(",", "."));
  if (!(v > 0)) { toast("Enter the frequency in MHz, e.g. 169.8000"); return; }
  send({ type: "config", changes: { freq: Math.round(v * 1e6) } });
  $("freqinput").blur();
});

$("charset").addEventListener("change", (e) => send({ type: "config", changes: { charset: e.target.value } }));

// ---- messages ----------------------------------------------------------------

function localize(text) {
  return config.charset === "sv" ? text.replace(/[[\\\]{|}]/g, (c) => SWEDISH[c]) : text;
}

function shownText(m) {
  return m.type === "alpha" ? localize(m.text) : m.text;
}

function pad(n) { return String(n).padStart(2, "0"); }

function when(t) {
  const d = new Date(t * 1000);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  if (d.toDateString() === new Date().toDateString()) return time;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`;
}

function matches(m, q) {
  if (!q) return true;
  return String(m.ric).includes(q) || shownText(m).toLowerCase().includes(q);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderMessage(m) {
  const box = el("div", "msg");
  box.dataset.id = m.id;
  const meta = el("div", "meta");
  meta.append(el("span", "", when(m.time)), el("span", "ric", `${m.ric}`),
              el("span", "", `${FUNC[m.func]} · ${m.type}`));
  if (m.bad) meta.append(el("span", "warn", `${m.bad} codeword${m.bad > 1 ? "s" : ""} lost`));
  else if (m.corrected) meta.append(el("span", "", `${m.corrected} bit${m.corrected > 1 ? "s" : ""} corrected`));
  box.append(meta);
  box.append(m.type === "tone" ? el("div", "text tone", "Tone only (no message)")
                               : el("div", "text " + m.type, shownText(m)));
  if (open.has(m.id)) {
    const d = el("div", "details");
    const line = (label, value) => { const r = el("div"); r.append(el("b", "", label + ": "), value); d.append(r); };
    line("Frequency", `${mhz(m.freq)} MHz`);
    line("Function", `${m.func} (${FUNC[m.func]})`);
    if (m.type !== "tone") {
      line("As text", localize(m.alpha) || "—");
      line("As numeric", m.numeric || "—");
    }
    line("Received", new Date(m.time * 1000).toLocaleString());
    box.append(d);
  }
  box.addEventListener("click", () => {
    if (getSelection().toString()) return;     // selecting text, not toggling
    open.has(m.id) ? open.delete(m.id) : open.add(m.id);
    box.replaceWith(renderMessage(m));
  });
  return box;
}

function query() { return $("search").value.trim().toLowerCase(); }

function renderList() {
  const q = query();
  const list = $("list");
  const shown = messages.filter((m) => matches(m, q)).reverse();
  list.replaceChildren(...shown.map(renderMessage));
  updateCount(shown.length);
}

function addToList(m) {
  const list = $("list");
  if (matches(m, query())) {
    const box = renderMessage(m);
    box.classList.add("new");
    list.prepend(box);
    while (list.children.length > MAX_SHOWN) list.lastChild.remove();
  }
  updateCount(list.children.length);
}

function updateCount(n) {
  $("count").textContent = messages.length ? (n === messages.length ? `(${n})` : `(${n} of ${messages.length})`) : "";
  $("empty").hidden = n > 0;
  $("empty").textContent = messages.length ? "No messages match the filter." : "No messages yet.";
}

$("search").addEventListener("input", renderList);

// Refresh "today" times after midnight etc.
setInterval(renderList, 10 * 60 * 1000);

let toastTimer = null;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

connect();
