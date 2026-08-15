"use strict";

/**
 * Dashboard front-end.
 *
 * Performance notes:
 *  - Boards are iframed directly on their own loopback port, so TensorBoard's
 *    data requests never traverse the manager process.
 *  - Panes are mounted on first view and least-recently-used panes are unmounted
 *    past `server.keep_warm`. A hidden iframe still holds a full TensorBoard
 *    front-end (tens of MB of JS heap plus its own polling), so unmounting is the
 *    only way to actually give the memory back.
 *  - Status arrives over a single SSE stream; nothing here polls.
 */

const els = {
  boards: document.getElementById("boards"),
  tabbar: document.getElementById("tabbar"),
  panes: document.getElementById("panes"),
  placeholder: document.getElementById("placeholder"),
  filter: document.getElementById("filter"),
  totals: document.getElementById("totals"),
  configPath: document.getElementById("config-path"),
  reloadConfig: document.getElementById("reload-config"),
  logs: document.getElementById("logs"),
  logsTitle: document.getElementById("logs-title"),
  logsBody: document.getElementById("logs-body"),
  logsClose: document.getElementById("logs-close"),
};

const ACTIVE_KEY = "tbmgr.active";
/** name -> HTMLIFrameElement */
const frames = new Map();
/** mount order, least recent first */
const lru = [];

let state = { boards: [], server: { keep_warm: 2 } };
let active = localStorage.getItem(ACTIVE_KEY) || null;
let statusPane = null;

/* --------------------------------------------------------------- formatting */

const fmtBytes = (n) => {
  if (n === null || n === undefined) return "-";
  const units = ["B", "K", "M", "G", "T"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)}${units[unit]}`;
};

const fmtAge = (since) => {
  if (!since) return "-";
  const secs = Math.max(0, Date.now() / 1000 - since);
  if (secs < 90) return `${Math.round(secs)}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
};

const byName = (name) => state.boards.find((b) => b.name === name) || null;

/* -------------------------------------------------------------------- api */

async function post(path) {
  const response = await fetch(path, { method: "POST" });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || response.statusText);
  }
  return response.json();
}

const act = (name, action) => post(`/api/boards/${encodeURIComponent(name)}/${action}`);

/* ------------------------------------------------------------------ panes */

function mount(name) {
  let frame = frames.get(name);
  if (!frame) {
    const board = byName(name);
    if (!board) return null;
    frame = document.createElement("iframe");
    frame.className = "pane";
    frame.src = board.url;
    frame.title = `TensorBoard: ${name}`;
    frame.setAttribute("referrerpolicy", "no-referrer");
    els.panes.appendChild(frame);
    frames.set(name, frame);
  }
  touchLru(name);
  evict();
  return frame;
}

function touchLru(name) {
  const at = lru.indexOf(name);
  if (at >= 0) lru.splice(at, 1);
  lru.push(name);
}

function evict() {
  const keep = Math.max(1, state.server.keep_warm || 2);
  while (lru.length > keep) {
    const victim = lru.find((name) => name !== active);
    if (!victim) break;
    unmount(victim);
  }
}

/**
 * Remove a pane. `keepActive` is used when the board merely stopped being
 * runnable (restart, crash, idle stop): the selection must survive so the status
 * card explains what happened and the pane comes back on its own.
 */
function unmount(name, { keepActive = false } = {}) {
  const frame = frames.get(name);
  if (frame) {
    frame.src = "about:blank";
    frame.remove();
    frames.delete(name);
  }
  const at = lru.indexOf(name);
  if (at >= 0) lru.splice(at, 1);
  if (active === name && !keepActive) {
    active = lru.length ? lru[lru.length - 1] : null;
    if (active) localStorage.setItem(ACTIVE_KEY, active);
    else localStorage.removeItem(ACTIVE_KEY);
  }
  render();
}

function select(name) {
  active = name;
  localStorage.setItem(ACTIVE_KEY, name);
  const board = byName(name);
  if (!board) return render();
  // `demand` starts an on_demand board *and* keeps its idle timeout; `start`
  // would pin it running forever, silently turning it into an always board.
  const action = board.autostart === "on_demand" || board.state === "running" ? "demand" : "start";
  act(name, action).catch(action === "demand" ? () => {} : reportError);
  render();
}

function detach(name) {
  const board = byName(name);
  if (board) window.open(board.url, `tb-${name}`, "noopener");
}

function reloadPane(name) {
  const frame = frames.get(name);
  const board = byName(name);
  if (frame && board) frame.src = board.url;
}

/* ----------------------------------------------------------------- render */

function render() {
  renderSidebar();
  renderTabs();
  renderPanes();
  renderTotals();
}

function renderTotals() {
  const running = state.boards.filter((b) => b.state === "running");
  const rss = running.reduce((sum, b) => sum + (b.rss_bytes || 0), 0);
  const cpu = running.reduce((sum, b) => sum + (b.cpu_percent || 0), 0);
  els.totals.textContent =
    `${running.length}/${state.boards.length} running - ${fmtBytes(rss)} rss - ` +
    `${cpu.toFixed(0)}% cpu - ${frames.size} pane(s) mounted`;
  els.configPath.textContent = state.server.config_path || "";
}

function renderSidebar() {
  const needle = els.filter.value.trim().toLowerCase();
  const frag = document.createDocumentFragment();

  state.boards.forEach((board, index) => {
    if (needle && !`${board.name} ${board.target}`.toLowerCase().includes(needle)) return;
    const row = document.createElement("div");
    row.className = `board state-${board.state}${board.name === active ? " active" : ""}`;
    row.dataset.name = board.name;

    const dot = document.createElement("span");
    dot.className = "dot";
    dot.title = board.state;

    const body = document.createElement("div");
    const name = document.createElement("div");
    name.className = "name";
    name.append(document.createTextNode(board.name));
    if (index < 9) {
      const hint = document.createElement("span");
      hint.className = "index";
      hint.textContent = index + 1;
      name.append(hint);
    }

    const meta = document.createElement("div");
    meta.className = "meta";
    const bits = [`:${board.port}`, board.autostart];
    if (board.state === "running") {
      bits.push(fmtAge(board.since), fmtBytes(board.rss_bytes));
      if (board.cpu_percent) bits.push(`${board.cpu_percent}%`);
    }
    meta.textContent = bits.join(" - ");

    const msg = document.createElement("div");
    msg.className = "msg";
    msg.textContent = board.state === "running" ? board.target : board.message || board.state;
    msg.title = `${board.target}\n${board.message || ""}`;

    const actions = document.createElement("div");
    actions.className = "actions";
    actions.append(
      button(board.state === "running" || board.state === "starting" ? "stop" : "start", (event) => {
        event.stopPropagation();
        act(board.name, board.state === "running" || board.state === "starting" ? "stop" : "start")
          .catch(reportError);
      }),
      button("restart", (event) => {
        event.stopPropagation();
        act(board.name, "restart").catch(reportError);
      }),
      button("logs", (event) => {
        event.stopPropagation();
        showLogs(board.name);
      }),
      button("open", (event) => {
        event.stopPropagation();
        detach(board.name);
      }),
    );

    body.append(name, meta, msg, actions);
    row.append(dot, body);
    row.addEventListener("click", () => select(board.name));
    frag.append(row);
  });

  els.boards.replaceChildren(frag);
}

function button(label, handler) {
  const el = document.createElement("button");
  el.textContent = label;
  el.addEventListener("click", handler);
  return el;
}

function renderTabs() {
  const frag = document.createDocumentFragment();
  for (const name of lru) {
    const tab = document.createElement("div");
    tab.className = `tab${name === active ? " active" : ""}`;
    tab.append(document.createTextNode(name));
    const close = document.createElement("span");
    close.className = "x";
    close.textContent = "x";
    close.title = "unmount this pane (frees its memory)";
    close.addEventListener("click", (event) => {
      event.stopPropagation();
      unmount(name);
    });
    tab.append(close);
    tab.addEventListener("click", () => select(name));
    frag.append(tab);
  }
  els.tabbar.replaceChildren(frag);
}

function renderPanes() {
  const board = active ? byName(active) : null;
  if (board && board.state === "running") mount(board.name);

  for (const [name, frame] of frames) frame.hidden = name !== active;

  if (board && board.state === "running") {
    hideStatusPane();
    els.placeholder.hidden = true;
    return;
  }
  els.placeholder.hidden = Boolean(board);
  if (board) showStatusPane(board);
  else hideStatusPane();
}

function showStatusPane(board) {
  if (!statusPane) {
    statusPane = document.createElement("div");
    statusPane.className = "pane card";
    els.panes.appendChild(statusPane);
  }
  statusPane.hidden = false;
  statusPane.replaceChildren();

  const title = document.createElement("h2");
  title.textContent = `${board.name} - ${board.state}`;
  const target = document.createElement("div");
  target.className = "muted";
  target.textContent = `${board.target} - port ${board.port} - autostart ${board.autostart}`;
  const message = document.createElement("p");
  message.textContent = board.message || "";

  const row = document.createElement("div");
  row.className = "row";
  row.append(
    button(board.state === "starting" ? "stop" : "start", () =>
      act(board.name, board.state === "starting" ? "stop" : "start").catch(reportError),
    ),
    button("restart", () => act(board.name, "restart").catch(reportError)),
    button("logs", () => showLogs(board.name)),
    button("open in new window", () => detach(board.name)),
  );

  statusPane.append(title, target, message, row);

  if (board.state !== "starting") {
    const pre = document.createElement("pre");
    pre.textContent = "loading log tail...";
    statusPane.append(pre);
    fetchLogs(board.name, 60)
      .then((text) => {
        pre.textContent = text || "(no log output yet)";
      })
      .catch((err) => {
        pre.textContent = String(err);
      });
  }
}

function hideStatusPane() {
  if (statusPane) statusPane.hidden = true;
}

function reportError(err) {
  const message = err && err.message ? err.message : String(err);
  els.totals.textContent = `error: ${message}`;
}

/* ------------------------------------------------------------------- logs */

async function fetchLogs(name, lines) {
  const response = await fetch(`/api/boards/${encodeURIComponent(name)}/logs?lines=${lines}`);
  if (!response.ok) throw new Error(`log fetch failed: ${response.status}`);
  return response.text();
}

async function showLogs(name) {
  els.logsTitle.textContent = `${name} - log tail`;
  els.logsBody.textContent = "loading...";
  els.logs.showModal();
  try {
    els.logsBody.textContent = (await fetchLogs(name, 500)) || "(empty)";
  } catch (err) {
    els.logsBody.textContent = String(err);
  }
  els.logsBody.scrollTop = els.logsBody.scrollHeight;
}

/* ------------------------------------------------------------------ events */

function connect() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
    state = JSON.parse(event.data);
    if (active && !byName(active)) {
      active = null;
      localStorage.removeItem(ACTIVE_KEY);
    }
    for (const name of [...frames.keys()]) {
      const board = byName(name);
      // Drop panes whose board went away or died: the iframe would show a
      // browser error page and keep its heap alive for nothing.
      if (!board) unmount(name);
      else if (board.state !== "running") unmount(name, { keepActive: true });
    }
    render();
  };
  source.onerror = () => {
    els.totals.textContent = "reconnecting to manager...";
  };
}

/** Tell the manager a human is watching, so on_demand boards stay up. */
function heartbeat() {
  const names = new Set(lru);
  if (active) names.add(active);
  for (const name of names) act(name, "demand").catch(() => {});
}

document.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLInputElement || event.metaKey || event.ctrlKey) {
    if (event.key === "Escape") event.target.blur();
    return;
  }
  if (event.key === "/") {
    event.preventDefault();
    els.filter.focus();
    return;
  }
  if (event.key >= "1" && event.key <= "9") {
    const board = state.boards[Number(event.key) - 1];
    if (board) select(board.name);
    return;
  }
  if (!active) return;
  if (event.key === "w") unmount(active);
  else if (event.key === "d") detach(active);
  else if (event.key === "r") reloadPane(active);
});

els.filter.addEventListener("input", renderSidebar);
els.logsClose.addEventListener("click", () => els.logs.close());
els.reloadConfig.addEventListener("click", () => {
  post("/api/reload")
    .then(() => {
      els.totals.textContent = "registry reloaded";
    })
    .catch(reportError);
});

fetch("/api/state")
  .then((response) => response.json())
  .then((data) => {
    state = data;
    if (active && !byName(active)) active = null;
    render();
  })
  .catch(reportError)
  .finally(connect);

setInterval(heartbeat, 60000);
setInterval(renderTotals, 5000);
