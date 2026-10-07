"use strict";

/**
 * TensorWatch dashboard.
 *
 * Performance contract:
 *  - Boards are iframed straight at their own loopback port, so TensorBoard's
 *    data traffic never passes through the manager process.
 *  - Panes mount on first view; least-recently-used panes are unmounted past
 *    `server.keep_warm`, because a hidden iframe keeps a whole TensorBoard
 *    front-end (tens of MB of heap plus its own polling) alive.
 *  - One SSE stream carries board state and the mlq queue; nothing polls.
 *  - Rows are created once and patched in place (no innerHTML churn), renders
 *    are coalesced into an animation frame, and everything stops while the
 *    window is hidden.
 */

const $ = (id) => document.getElementById(id);
const els = {
  boards: $("boards"),
  boardsCount: $("boards-count"),
  panes: $("panes"),
  placeholder: $("placeholder"),
  filter: $("filter"),
  totals: $("totals"),
  reloadConfig: $("reload-config"),
  queue: $("queue"),
  queueHead: $("queue-head"),
  queueMeta: $("queue-meta"),
  queueJobs: $("queue-jobs"),
  queueMore: $("queue-more"),
  queueError: $("queue-error"),
  logs: $("logs"),
  logsTitle: $("logs-title"),
  logsBody: $("logs-body"),
  logsClose: $("logs-close"),
  addBoard: $("add-board"),
  addDialog: $("add-board-dialog"),
  addClose: $("add-board-close"),
  addForm: $("add-board-form"),
  addPath: $("add-board-path"),
  addPaths: $("add-board-paths"),
  addName: $("add-board-name"),
  addAutostart: $("add-board-autostart"),
  addError: $("add-board-error"),
  suggestions: $("suggestions"),
  suggestionsMeta: $("suggestions-meta"),
  suggestionsList: $("suggestions-list"),
  boardsEmpty: $("boards-empty"),
  placeholderTitle: $("placeholder-title"),
  placeholderText: $("placeholder-text"),
  placeholderAdd: $("placeholder-add"),
  toasts: $("toasts"),
};

const ACTIVE_KEY = "tensorwatch.active";
const QUEUE_KEY = "tensorwatch.queue.collapsed";

let state = { boards: [], queue: null, server: { keep_warm: 2, queue_visible: 5 } };
let active = localStorage.getItem(ACTIVE_KEY) || null;
let queueExpanded = false;
let statusPane = null;
let statusKey = "";
let framePending = false;
let streamDown = false;
/** Static buttons get their icons on the first render, not at load. */
let chromeReady = false;
/** A board the user just added; opened as soon as the manager reports it. */
let pendingSelect = null;
let heartbeatTimer = null;
let heartbeatKey = "";

/** name -> HTMLIFrameElement */
const frames = new Map();
/** mount order, least recent first */
const lru = [];
/** name -> {row, name, meta, acts, ...} */
const boardRows = new Map();
/** job id -> {row, ...} */
const jobRows = new Map();

/* --------------------------------------------------------------- formatting */

function fmtBytes(n) {
  if (!n) return "-";
  const units = ["B", "K", "M", "G", "T"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)}${units[unit]}`;
}

function fmtAge(since) {
  if (!since) return "-";
  const secs = Math.max(0, Date.now() / 1000 - since);
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 172800) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

/** name -> board, rebuilt with each state so lookups in render loops stay O(1). */
let boardIndex = new Map();
const byName = (name) => boardIndex.get(name) || null;
const queue = () => state.queue;

/* --------------------------------------------------------------------- api */

async function request(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || response.statusText);
  }
  return response.json();
}

const post = (path, body) => request("POST", path, body);
const boardPath = (name) => `/api/boards/${encodeURIComponent(name)}`;
const act = (name, action) => post(`${boardPath(name)}/${action}`);

/* ------------------------------------------------------------------- icons */

// 24px grid, drawn as strokes so they take the text colour and stay crisp at 13px.
const ICONS = {
  play: "M8 5.5v13l10.5-6.5z",
  stop: "M7 7h10v10H7z",
  restart: "M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.6h-4.6",
  clock: "M12 4.5a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM12 8v4.2l2.8 1.8",
  log: "M5 7h14M5 12h14M5 17h9",
  external: "M13.5 5H19v5.5M19 5l-8 8M10 7H6v11h11v-4",
  trash: "M5 7.5h14M10 7.5V5h4v2.5M7 7.5l.9 11.5h8.2L17 7.5",
  plus: "M12 5.5v13M5.5 12h13",
  close: "M6.5 6.5l11 11M17.5 6.5l-11 11",
};
const SVG_NS = "http://www.w3.org/2000/svg";

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

/** Swap a button's icon only when it changes, so renders do not churn the DOM. */
function setIcon(button, name) {
  if (button.dataset.icon === name) return;
  button.dataset.icon = name;
  button.replaceChildren(icon(name));
}

function setLabel(button, label) {
  if (button.title === label) return;
  button.title = label;
  button.setAttribute("aria-label", label);
}

/* ------------------------------------------------------------------ toasts */

/**
 * A message that outlives the render that caused it, optionally with one action
 * (undo). Shown over the pane rather than in the sidebar: the sidebar's totals
 * line should not jump every time something is confirmed.
 */
function toast(text, { kind = "info", ttl = 5000, action = null } = {}) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const label = document.createElement("span");
  label.textContent = text;
  el.append(label);
  let timer = null;
  const dismiss = () => {
    clearTimeout(timer);
    el.remove();
  };
  if (action) {
    el.append(textButton(action.label, () => {
      dismiss();
      action.run();
    }));
  }
  el.append(iconButton("close", "dismiss", dismiss));
  el.dataset.kept = action ? "1" : "";
  els.toasts.append(el);
  // Keep the stack short, but never drop a pending undo to do it.
  while (els.toasts.children.length > 4) {
    const oldest = [...els.toasts.children].find((item) => !item.dataset.kept);
    if (!oldest) break;
    oldest.remove();
  }
  timer = setTimeout(dismiss, ttl);
}

const say = (text, ttl = 4000) => toast(text, { ttl });

function reportError(err) {
  toast(err && err.message ? err.message : String(err), { kind: "error", ttl: 9000 });
}

/* ------------------------------------------------------------------- panes */

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
  const at = lru.indexOf(name);
  if (at >= 0) lru.splice(at, 1);
  lru.push(name);
  evict();
  return frame;
}

function evict() {
  const keep = Math.max(1, state.server.keep_warm || 2);
  while (lru.length > keep) {
    const victim = lru.find((name) => name !== active);
    if (!victim) break;
    // renderPanes is mid-render; do not queue a second full pass for a pane swap.
    unmount(victim, { keepActive: true, quiet: true });
  }
}

/**
 * Drop a pane. `keepActive` keeps the selection when the pane went away for a
 * reason the user did not ask for (restart, crash, idle stop, LRU eviction), so
 * the status card explains it and the pane returns by itself.
 */
function unmount(name, { keepActive = false, quiet = false } = {}) {
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
  if (!quiet) schedule();
}

function select(name) {
  active = name;
  localStorage.setItem(ACTIVE_KEY, name);
  const board = byName(name);
  if (board) {
    // The heartbeat starts on_demand boards without pinning them running.
    // Only explicitly start a stopped board with a different policy.
    if (board.autostart !== "on_demand" && board.state !== "running") {
      act(name, "start").catch(reportError);
    }
  }
  scheduleHeartbeat(true);
  schedule();
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

/* ------------------------------------------------------------------ render */

function schedule() {
  if (framePending || document.hidden) return;
  framePending = true;
  requestAnimationFrame(() => {
    framePending = false;
    render();
  });
}

function render() {
  if (!chromeReady) {
    chromeReady = true;
    setIcon(els.reloadConfig, "restart");
    setIcon(els.addBoard, "plus");
  }
  renderBoards();
  renderQueue();
  renderPanes();
  renderTotals();
  scheduleHeartbeat();
}

function renderTotals() {
  if (streamDown) {
    els.totals.textContent = "reconnecting to tensorwatch…";
    return;
  }
  const running = state.boards.filter((b) => b.state === "running");
  const rss = running.reduce((sum, b) => sum + (b.rss_bytes || 0), 0);
  const cpu = running.reduce((sum, b) => sum + (b.cpu_percent || 0), 0);
  const broken = state.boards.filter((b) => b.state === "failed" || b.state === "backoff");
  els.totals.replaceChildren(
    document.createTextNode(
      `${running.length}/${state.boards.length} up · ${fmtBytes(rss)} · ${cpu.toFixed(0)}% cpu · ` +
        `${frames.size} pane${frames.size === 1 ? "" : "s"}`,
    ),
  );
  if (broken.length) {
    const flag = document.createElement("span");
    flag.className = "warn";
    flag.textContent = ` · ${broken.length} down`;
    els.totals.append(flag);
  }
  els.boardsCount.textContent = state.boards.length ? `${state.boards.length}` : "";
}

function boardRow(board) {
  let row = boardRows.get(board.name);
  if (row) return row;

  const el = document.createElement("div");
  el.className = "board";
  el.dataset.name = board.name;
  el.tabIndex = 0;
  el.setAttribute("role", "option");

  const dot = document.createElement("span");
  dot.className = "dot";

  const body = document.createElement("div");
  body.className = "body";
  const name = document.createElement("span");
  name.className = "name";
  const meta = document.createElement("span");
  meta.className = "meta";
  body.append(name, meta);

  const right = document.createElement("div");
  right.className = "right";
  const idx = document.createElement("span");
  idx.className = "idx";
  const live = document.createElement("span");
  live.className = "live";
  live.title = "pane mounted";
  const acts = document.createElement("div");
  acts.className = "acts";
  const power = iconButton("play", "start", (event) => {
    event.stopPropagation();
    const board_ = byName(board.name);
    const running = board_ && (board_.state === "running" || board_.state === "starting");
    act(board.name, running ? "stop" : "start").catch(reportError);
  });
  const demandToggle = iconButton("clock", "on demand", (event) => {
    event.stopPropagation();
    const board_ = byName(board.name);
    if (!board_) return;
    // Binary by design: "not on demand" means the manager keeps it up.
    const mode = board_.autostart === "on_demand" ? "always" : "on_demand";
    post(`${boardPath(board.name)}/autostart`, { mode })
      .then(() => say(`${board.name} now runs ${mode === "on_demand" ? "when opened" : "always"}`))
      .catch(reportError);
  });
  acts.append(
    power,
    iconButton("restart", "restart", (event) => {
      event.stopPropagation();
      act(board.name, "restart").catch(reportError);
    }),
    demandToggle,
    iconButton("log", "log tail", (event) => {
      event.stopPropagation();
      showLogs(board.name);
    }),
    iconButton("external", "open in its own window", (event) => {
      event.stopPropagation();
      detach(board.name);
    }),
    iconButton("trash", "remove from the registry", (event) => {
      event.stopPropagation();
      removeBoard(board.name);
    }),
  );
  // Queue activity for this board's project; stays visible on hover because it
  // answers "is this board's data moving right now?".
  const run = document.createElement("span");
  run.className = "run";
  const runq = document.createElement("span");
  runq.className = "runq";
  // Persistent policy mark: the toggle itself only appears on hover.
  const demand = document.createElement("span");
  demand.className = "demand";
  demand.title = "on demand: starts when opened, stops when idle";
  demand.append(icon("clock"));
  right.append(run, runq, demand, idx, live, acts);

  el.append(dot, body, right);
  el.addEventListener("click", () => select(board.name));
  el.addEventListener("keydown", (event) => {
    if (event.target !== el) return;  // a key on one of the row's own buttons
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select(board.name);
    } else if (event.key === "Delete") {
      event.preventDefault();
      removeBoard(board.name);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      focusNeighbour(el, event.key === "ArrowDown" ? 1 : -1);
    }
  });
  els.boards.appendChild(el);

  row = { el, name, meta, power, idx, run, runq, demand, demandToggle, key: "" };
  boardRows.set(board.name, row);
  return row;
}

function iconButton(name, label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  setIcon(button, name);
  setLabel(button, label);
  button.addEventListener("click", handler);
  return button;
}

/** Move focus to the next visible board row; the list is short, so no roving index. */
function focusNeighbour(el, step) {
  const rows = [...els.boards.children].filter((row) => !row.classList.contains("hidden"));
  const next = rows[rows.indexOf(el) + step];
  if (next) next.focus();
  return Boolean(next);
}

/** What a board is doing, in the few words a row has room for. */
function shortStatus(board) {
  if (board.state === "stopped" && board.autostart === "on_demand") return "on demand";
  const message = board.message || "";
  if (message.startsWith("logdir does not exist")) return "logdir missing";
  if (board.state === "failed" || board.state === "backoff") return message || board.state;
  return board.state;
}

/** board name -> {running: QueueJob[], queued: QueueJob[]} from the mlq snapshot. */
function activityByBoard() {
  const map = new Map();
  const snapshot = queue();
  if (!snapshot) return map;
  for (const [bucket, jobs] of [["running", snapshot.running], ["queued", snapshot.queued]]) {
    for (const job of jobs) {
      // A job can belong to more than one board (a project with a live logdir and
      // an archive): mark all of them.
      for (const name of job.boards || []) {
        const entry = map.get(name) || { running: [], queued: [] };
        entry[bucket].push(job);
        map.set(name, entry);
      }
    }
  }
  return map;
}

/**
 * Compact "is work happening here" mark for a board row.
 *
 * Ground truth first: `writing` means something is appending to this logdir right
 * now, whoever started it and whichever child run it writes to. mlq only adds the
 * job names, and a queued count in its own dim slot. Details go in the tooltip so
 * the row stays one line.
 */
function runMark(board, work, live) {
  const running = work ? work.running : [];
  const queued = work ? work.queued : [];
  let text = "";
  if (board.writing) {
    text = `▶ ${fmtAge(board.last_event)}`;
  } else if (running.length === 1) {
    // A job is running but no data has landed yet (or it stopped landing).
    text = live ? `▶ ${fmtAge(running[0].since)}` : "▶ –";
  } else if (running.length > 1) {
    text = `▶ ${running.length}`;
  }
  const queuedText = queued.length ? `○${queued.length}` : "";

  const lines = [];
  if (board.last_event) {
    lines.push(
      board.writing
        ? `receiving data — last event ${fmtAge(board.last_event)} ago`
        : `quiet — last event ${fmtAge(board.last_event)} ago`,
    );
  }
  if (!live && (running.length || queued.length)) lines.push("mlq offline — last known:");
  for (const job of running) lines.push(`mlq #${job.id} ${job.name} — running ${fmtAge(job.since)}`);
  for (const job of queued) {
    lines.push(`mlq #${job.id} ${job.name} — queued${job.reason ? ` (${job.reason})` : ""}`);
  }
  return { text, queued: queuedText, title: lines.join("\n") };
}


const needleNow = () => els.filter.value.trim().toLowerCase();
const matchesFilter = (board, needle) =>
  !needle || `${board.name} ${board.target}`.toLowerCase().includes(needle);

function renderBoards() {
  const needle = needleNow();
  let shown = 0;
  const activity = activityByBoard();
  const live = Boolean(queue() && queue().connected);
  const seen = new Set();

  state.boards.forEach((board, index) => {
    const row = boardRow(board);
    seen.add(board.name);

    const bits = [`:${board.port}`];
    if (board.state === "running") {
      bits.push(fmtAge(board.since));
      if (board.rss_bytes) bits.push(fmtBytes(board.rss_bytes));
      if (board.cpu_percent) bits.push(`${board.cpu_percent}%`);
    } else {
      bits.push(shortStatus(board));
    }
    const meta = bits.join(" · ");
    const running = board.state === "running" || board.state === "starting";
    const filtered = !matchesFilter(board, needle);
    // Hotkeys count what is on screen, so a hidden board is never selectable.
    const number = filtered ? 0 : ++shown;
    const work = activity.get(board.name);
    const mark = runMark(board, work, live);
    const onDemand = board.autostart === "on_demand";
    const key = [
      board.state, board.target, board.message, meta, board.name === active,
      frames.has(board.name), index, number, filtered, mark.text, mark.queued, mark.title, live,
      board.writing, board.autostart,
    ].join("|");
    if (row.key === key) return;
    row.key = key;

    row.name.textContent = board.name;
    row.meta.textContent = meta;
    row.run.textContent = mark.text;
    row.runq.textContent = mark.queued;
    row.run.title = mark.title;
    row.runq.title = mark.title;
    row.el.className =
      `board state-${board.state}` +
      (board.name === active ? " active" : "") +
      (frames.has(board.name) ? " mounted" : "") +
      (board.writing ? " training" : "") +
      (filtered ? " hidden" : "");
    row.el.title =
      `${board.target}\n${board.message || board.state}` + (mark.title ? `\n${mark.title}` : "");
    row.el.setAttribute("aria-selected", board.name === active ? "true" : "false");
    row.idx.textContent = number && number <= 10 ? (number === 10 ? "0" : String(number)) : "";
    setIcon(row.power, running ? "stop" : "play");
    setLabel(row.power, running ? "stop" : "start");
    row.demand.hidden = !onDemand;
    row.demandToggle.className = onDemand ? "on" : "";
    setLabel(
      row.demandToggle,
      onDemand
        ? "runs when opened — click to keep it always running"
        : "always running — click to run it only when opened",
    );
    // Keep DOM order in sync with registry order without rebuilding rows.
    const expected = els.boards.children[index];
    if (expected !== row.el) els.boards.insertBefore(row.el, expected || null);
  });

  for (const [name, row] of boardRows) {
    if (!seen.has(name)) {
      row.el.remove();
      boardRows.delete(name);
    }
  }

  const empty = !state.boards.length
    ? "No boards yet."
    : shown ? "" : `No board matches “${els.filter.value.trim()}”.`;
  els.boardsEmpty.hidden = !empty;
  els.boardsEmpty.textContent = empty;
}

/* ------------------------------------------------------------------- queue */

function renderQueue() {
  const snapshot = queue();
  if (!snapshot) {
    els.queue.hidden = true;
    for (const [id, row] of jobRows) {
      row.el.remove();
      jobRows.delete(id);
    }
    return;
  }
  els.queue.hidden = false;
  const collapsed = localStorage.getItem(QUEUE_KEY) === "1";
  els.queue.classList.toggle("collapsed", collapsed);
  els.queueHead.setAttribute("aria-expanded", String(!collapsed));

  const limit = snapshot.effective_limit;
  els.queueMeta.textContent = snapshot.connected
    ? `${snapshot.active_leases}${limit ? `/${limit}` : ""} run · ${snapshot.queued.length} queued`
    : snapshot.running.length || snapshot.queued.length
      ? "mlq offline · last known"
      : "mlq offline";
  els.queueMeta.className = snapshot.connected
    ? snapshot.admission_blocked ? "blocked" : ""
    : "offline";
  els.queueError.hidden = Boolean(snapshot.connected) || !snapshot.error;
  // Daemon errors can be paragraphs; keep the panel small and put the rest in the
  // tooltip.
  const detail = (snapshot.error || "").replace(/\s+/g, " ");
  els.queueError.textContent = detail;
  els.queueError.title = detail;

  const visible = Math.max(1, state.server.queue_visible || 5);
  const shownQueued = queueExpanded ? snapshot.queued : snapshot.queued.slice(0, visible);
  const jobs = [...snapshot.running, ...shownQueued];
  const seen = new Set();

  jobs.forEach((job, index) => {
    seen.add(job.id);
    const row = jobRow(job);
    const running = job.state === "running";
    const when = running
      ? fmtAge(job.since)
      : job.priority
        ? `p${job.priority}`
        : fmtAge(job.queued_at);
    const tries = Number.parseInt(job.attempts, 10) || 0;
    const boards = job.boards || [];
    const tag = job.board || boards[0] || "";
    // Do not repeat the project when the board tag already says it, and only flag
    // attempts when the job actually was retried.
    const detail = running
      ? [tries > 1 ? `try ${job.attempts}` : "", tag === job.project ? "" : job.project]
          .filter(Boolean).join(" · ")
      : [job.reason || job.state, tag === job.project ? "" : job.project]
          .filter(Boolean).join(" · ");
    const key = [job.state, when, detail, boards.join(","), tag, index, snapshot.connected].join("|");
    if (row.key !== key) {
      row.key = key;
      row.glyph.textContent = running ? "●" : job.state === "held" ? "❚❚" : "○";
      row.name.textContent = job.name;
      const reason = document.createElement("span");
      reason.className = "jreason";
      reason.textContent = detail;
      row.meta.replaceChildren(reason);
      if (tag) {
        const tagEl = document.createElement("span");
        tagEl.className = "board-tag";
        tagEl.textContent = `▸ ${tag}`;
        row.meta.append(tagEl);
      }
      row.when.textContent = when;
      row.el.className = `job ${job.state}${tag ? " linked" : ""}`;
      row.kill.hidden = !snapshot.connected;
      setLabel(row.kill, running ? `kill #${job.id} ${job.name}` : `cancel #${job.id} ${job.name}`);
      row.el.title =
        `#${job.id} ${job.name}\n${job.state}${job.reason ? ` (${job.reason})` : ""}\n${job.cwd}` +
        (boards.length ? `\nboards: ${boards.join(", ")}\nclick to open ${tag}` : "");
    }
    const expected = els.queueJobs.children[index];
    if (expected !== row.el) els.queueJobs.insertBefore(row.el, expected || null);
  });

  for (const [id, row] of jobRows) {
    if (!seen.has(id)) {
      row.el.remove();
      jobRows.delete(id);
    }
  }

  const hidden = snapshot.queued.length - shownQueued.length;
  els.queueMore.hidden = hidden <= 0 && (!queueExpanded || snapshot.queued.length <= visible);
  els.queueMore.textContent = queueExpanded
    ? "show fewer"
    : `+${hidden} more queued`;
}

function jobRow(job) {
  let row = jobRows.get(job.id);
  if (row) return row;

  const el = document.createElement("div");
  const glyph = document.createElement("span");
  glyph.className = "glyph";
  const label = document.createElement("div");
  label.className = "label";
  const name = document.createElement("span");
  name.className = "jname";
  const meta = document.createElement("span");
  meta.className = "jmeta";
  label.append(name, meta);
  const when = document.createElement("span");
  when.className = "when";
  const kill = iconButton("close", "cancel", (event) => {
    event.stopPropagation();
    const current = [...(queue()?.running || []), ...(queue()?.queued || [])]
      .find((candidate) => candidate.id === job.id);
    if (!current) return;
    const verb = current.state === "running" ? "kill" : "cancel";
    post(`/api/queue/${current.id}/cancel`)
      .then(() => say(`${verb === "kill" ? "Killed" : "Cancelled"} #${current.id} ${current.name}`))
      .catch(reportError);
  });
  kill.className = "kill";
  el.append(glyph, label, when, kill);
  el.addEventListener("click", () => {
    const current = [...(queue()?.running || []), ...(queue()?.queued || [])]
      .find((candidate) => candidate.id === job.id);
    const target = current && (current.board || (current.boards || [])[0]);
    if (target && byName(target)) select(target);
  });
  els.queueJobs.appendChild(el);

  row = { el, glyph, name, meta, when, kill, key: "" };
  jobRows.set(job.id, row);
  return row;
}


/* ------------------------------------------------------------- pane surface */

function renderPanes() {
  const board = active ? byName(active) : null;
  if (board && board.state === "running") mount(board.name);
  for (const [name, frame] of frames) frame.hidden = name !== active;

  if (board && board.state === "running") {
    if (statusPane) statusPane.hidden = true;
    statusKey = "";
    els.placeholder.hidden = true;
    return;
  }
  els.placeholder.hidden = Boolean(board);
  const none = !state.boards.length;
  els.placeholderTitle.textContent = none ? "No boards yet" : "Nothing open";
  els.placeholderText.textContent = none
    ? "Register a TensorBoard logdir and it is kept running, on its own port, from here on."
    : "Pick a board on the left. Only the last couple you viewed stay loaded.";
  els.placeholderAdd.hidden = !none;
  if (board) {
    showStatusPane(board);
  } else if (statusPane) {
    statusPane.hidden = true;
    statusKey = "";
  }
}

function showStatusPane(board) {
  if (!statusPane) {
    statusPane = document.createElement("div");
    statusPane.className = "pane card";
    els.panes.appendChild(statusPane);
  }
  statusPane.hidden = false;
  // Rebuilding on every frame would re-fetch the log tail and wipe the selection.
  const key = `${board.name}|${board.state}|${board.message}`;
  if (statusKey === key) return;
  statusKey = key;
  statusPane.replaceChildren();

  const title = document.createElement("h2");
  const chip = document.createElement("span");
  chip.className = `chip state-${board.state}`;
  chip.textContent = board.state;
  title.append(board.name, chip);
  const target = document.createElement("div");
  target.className = "muted";
  target.textContent = `${board.target} · port ${board.port} · autostart ${board.autostart}`;
  const message = document.createElement("p");
  message.className = "muted";
  const missing = (board.message || "").startsWith("logdir does not exist");
  message.textContent = missing
    ? "Its logdir does not exist. Remove the board if the runs are gone for good."
    : board.message || "";

  const row = document.createElement("div");
  row.className = "row";
  const starting = board.state === "starting";
  row.append(
    textButton(starting ? "stop" : "start", () =>
      act(board.name, starting ? "stop" : "start").catch(reportError),
    ),
    textButton("restart", () => act(board.name, "restart").catch(reportError)),
    textButton("log tail", () => showLogs(board.name)),
    textButton("open in new window", () => detach(board.name)),
    textButton("remove", () => removeBoard(board.name)),
  );
  statusPane.append(title, target, message, row);

  // A board that never started has no log worth showing.
  if (!starting && !missing) {
    const pre = document.createElement("pre");
    pre.textContent = "loading log tail…";
    statusPane.append(pre);
    fetchLogs(board.name, 60)
      .then((text) => {
        pre.textContent = text || "(no output yet)";
      })
      .catch((err) => {
        pre.textContent = String(err);
      });
  }
}

function textButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

/* -------------------------------------------------------------------- logs */

async function fetchLogs(name, lines) {
  const response = await fetch(`${boardPath(name)}/logs?lines=${lines}`);
  if (!response.ok) throw new Error(`log fetch failed: ${response.status}`);
  return response.text();
}

async function showLogs(name) {
  els.logsTitle.textContent = `${name} — log tail`;
  els.logsBody.textContent = "loading…";
  els.logs.showModal();
  try {
    els.logsBody.textContent = (await fetchLogs(name, 500)) || "(empty)";
  } catch (err) {
    els.logsBody.textContent = String(err);
  }
  els.logsBody.scrollTop = els.logsBody.scrollHeight;
}

/* ------------------------------------------------------- add / remove board */

/**
 * Unregister a board. No confirmation: the registry block is handed back by the
 * server and "Undo" puts it where it was, port and comments included.
 */
async function removeBoard(name) {
  const wasActive = active === name;
  // Keep the keyboard in the list: the row about to go has the focus.
  const row = boardRows.get(name);
  if (row && document.activeElement === row.el) {
    focusNeighbour(row.el, 1) || focusNeighbour(row.el, -1);
  }
  try {
    const { undo } = await request("DELETE", boardPath(name));
    toast(`Removed ${name}`, {
      ttl: 12000,
      action: {
        label: "Undo",
        run: () =>
          post("/api/boards", { undo })
            .then((result) => {
              if (wasActive && !active) pendingSelect = result.board;
              say(`Restored ${result.board}`);
            })
            .catch(reportError),
      },
    });
  } catch (err) {
    reportError(err);
  }
}

/** Completion state for the logdir field. */
const paths = { items: [], index: -1, seq: 0, timer: null };
let suggestSeq = 0;
/** One add at a time: a held Enter must not register the logdir twice. */
let submitting = false;

function showAddBoard() {
  els.addForm.reset();
  showAddError("");
  renderPaths([]);
  els.addDialog.showModal();
  els.addPath.focus();
  loadSuggestions();
}

function showAddError(message) {
  els.addError.textContent = message;
  els.addError.hidden = !message;
}

/** Ask for directories matching what is typed; only the latest answer is shown. */
function completePaths() {
  clearTimeout(paths.timer);
  const prefix = els.addPath.value;
  if (!prefix.trim()) {
    paths.seq += 1;
    renderPaths([]);
    return;
  }
  // Numbered when typed, not when asked: an answer to the previous keystroke
  // must not show up during this one's debounce.
  const seq = ++paths.seq;
  paths.timer = setTimeout(async () => {
    let items = [];
    try {
      const response = await fetch(`/api/paths?prefix=${encodeURIComponent(prefix)}`);
      items = response.ok ? (await response.json()).paths || [] : [];
    } catch {
      // The field still works as a plain text input.
    }
    if (seq === paths.seq) renderPaths(items);
  }, 70);
}

function renderPaths(items) {
  paths.items = items;
  paths.index = -1;
  els.addPaths.hidden = !items.length;
  els.addPath.setAttribute("aria-expanded", String(Boolean(items.length)));
  els.addPath.removeAttribute("aria-activedescendant");
  els.addPaths.replaceChildren(...items.map((item, index) => {
    const li = document.createElement("li");
    li.id = `add-board-path-${index}`;
    li.setAttribute("role", "option");
    const cut = item.path.lastIndexOf("/") + 1;
    const parent = document.createElement("span");
    parent.className = "parent";
    parent.textContent = item.path.slice(0, cut);
    const leaf = document.createElement("span");
    leaf.className = "leaf";
    leaf.textContent = item.path.slice(cut);
    li.append(parent, leaf);
    if (item.events) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "has runs";
      li.append(tag);
    }
    // mousedown, not click: the input must not lose focus to the list.
    li.addEventListener("mousedown", (event) => {
      event.preventDefault();
      acceptPath(index);
    });
    return li;
  }));
}

function highlightPath(index) {
  const count = paths.items.length;
  if (!count) return;
  paths.index = (index + count) % count;
  [...els.addPaths.children].forEach((li, at) => {
    li.classList.toggle("on", at === paths.index);
    li.setAttribute("aria-selected", String(at === paths.index));
  });
  const li = els.addPaths.children[paths.index];
  els.addPath.setAttribute("aria-activedescendant", li.id);
  li.scrollIntoView({ block: "nearest" });
}

/** Take a completion and keep going: the trailing slash lists what is inside. */
function acceptPath(index) {
  const item = paths.items[index];
  if (!item) return;
  els.addPath.value = `${item.path}/`;
  completePaths();
}

function onPathKey(event) {
  const open = paths.items.length > 0;
  if (event.key === "ArrowDown" && open) {
    event.preventDefault();
    highlightPath(paths.index + 1);
  } else if (event.key === "ArrowUp" && open) {
    event.preventDefault();
    highlightPath(paths.index - 1);
  } else if (event.key === "Tab" && open && !event.shiftKey) {
    event.preventDefault();
    acceptPath(Math.max(0, paths.index));
  } else if (event.key === "Enter" && open && paths.index >= 0) {
    event.preventDefault();
    acceptPath(paths.index);
  } else if (event.key === "Escape" && open) {
    // First Escape closes the list; the next one closes the dialog.
    event.preventDefault();
    paths.seq += 1;
    renderPaths([]);
  }
}

async function loadSuggestions() {
  const seq = ++suggestSeq;
  els.suggestions.hidden = false;
  els.suggestionsMeta.textContent = "looking…";
  els.suggestionsList.replaceChildren();
  let items = [];
  try {
    const response = await fetch("/api/suggestions");
    items = response.ok ? (await response.json()).suggestions || [] : [];
  } catch {
    // Suggestions are a convenience; the form does not depend on them.
  }
  if (seq !== suggestSeq) return;
  els.suggestionsList.replaceChildren(...items.map(suggestionRow));
  countSuggestions();
}

function countSuggestions() {
  const count = els.suggestionsList.children.length;
  els.suggestions.hidden = !count;
  els.suggestionsMeta.textContent = String(count);
}

function suggestionRow(item) {
  const li = document.createElement("li");
  const body = document.createElement("button");
  body.type = "button";
  body.className = "pick";
  body.title = "fill the form with this logdir";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = item.name;
  const meta = document.createElement("span");
  meta.className = "meta";
  const where = document.createElement("span");
  where.className = "where";
  // The left-to-right mark keeps "~/" in front while the box clips from the left.
  where.textContent = `\u200e${item.path}`;
  const files = document.createElement("span");
  files.textContent = `${item.events >= 1000 ? "1000+" : item.events} event file${item.events === 1 ? "" : "s"}`;
  meta.append(where, files);
  body.append(name, meta);
  body.addEventListener("click", () => {
    els.addPath.value = item.path;
    els.addName.value = item.name;
    renderPaths([]);
    els.addPath.focus();
  });
  const add = textButton("Add", async () => {
    add.disabled = true;
    try {
      const result = await post("/api/boards", {
        path: item.path,
        name: item.name,
        autostart: els.addAutostart.value,
      });
      li.remove();
      countSuggestions();
      // Said in the dialog: a toast would sit underneath it.
      showAddError("");
      els.suggestionsMeta.textContent = `added ${result.board}`;
    } catch (err) {
      add.disabled = false;
      showAddError(err && err.message ? err.message : String(err));
    }
  });
  li.append(body, add);
  return li;
}

async function submitAddBoard(event) {
  event.preventDefault();
  const path = els.addPath.value.trim();
  if (!path || submitting) return;
  submitting = true;
  try {
    const result = await post("/api/boards", {
      path,
      name: els.addName.value.trim(),
      autostart: els.addAutostart.value,
    });
    els.addDialog.close();
    pendingSelect = result.board;
    say(result.events ? `Added ${result.board}` : `Added ${result.board} — no event files there yet`);
  } catch (err) {
    showAddError(err && err.message ? err.message : String(err));
  } finally {
    submitting = false;
  }
}

/* ------------------------------------------------------------------ stream */

function absorb(payload) {
  state = payload;
  boardIndex = new Map(state.boards.map((board) => [board.name, board]));
  streamDown = false;
  if (pendingSelect && byName(pendingSelect)) {
    const name = pendingSelect;
    pendingSelect = null;
    select(name);
  }
  if (active && !byName(active)) {
    active = null;
    localStorage.removeItem(ACTIVE_KEY);
  }
  for (const name of [...frames.keys()]) {
    const board = byName(name);
    // A dead board's iframe would show a browser error page and hold its heap.
    if (!board) unmount(name);
    else if (board.state !== "running") unmount(name, { keepActive: true });
  }
  scheduleHeartbeat();
  schedule();
}

function connect() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => absorb(JSON.parse(event.data));
  source.onerror = () => {
    streamDown = true;
    schedule();
  };
}

/** Renew watched boards well before their idle lease expires. */
function scheduleHeartbeat(force = false) {
  const names = [...new Set([...lru, ...(active ? [active] : [])])]
    .filter((name) => byName(name));
  const watched = names.map(byName);
  const key = document.hidden ? "" : JSON.stringify(
    watched.map((board) => [board.name, board.autostart, board.idle_timeout]),
  );
  // SSE updates and local age renders must not continually postpone renewal.
  if (!force && key === heartbeatKey && heartbeatTimer !== null) return;
  clearTimeout(heartbeatTimer);
  heartbeatTimer = null;
  heartbeatKey = key;
  if (document.hidden || !names.length) return;

  let period = 60000;
  for (const board of watched) {
    if (board.autostart === "on_demand" && Number.isFinite(board.idle_timeout) && board.idle_timeout > 0) {
      period = Math.min(period, board.idle_timeout * 1000 / 3);
    }
  }
  // Keep room for delayed callbacks and failed requests without busy-looping
  // on an accidentally tiny timeout. Hidden windows release their leases.
  period = Math.max(250, period);
  const renew = () => {
    for (const name of names) act(name, "demand").catch(() => {});
    heartbeatTimer = setTimeout(renew, period);
  };
  // Renew immediately when restoring a selection, resuming the window, or
  // changing the watched set/policy, including newly selected short leases.
  renew();
}

/* ------------------------------------------------------------------ events */

els.filter.addEventListener("input", schedule);
els.logsClose.addEventListener("click", () => els.logs.close());
// The opener lives in a hover-only action row, so the UA cannot restore focus.
// It goes to a board row, never to the filter: a text field would swallow the
// next shortcut.
const restoreFocus = () => {
  const row = boardRows.get(active) || boardRows.values().next().value;
  if (row) row.el.focus();
  else document.activeElement?.blur();
};
els.logs.addEventListener("close", restoreFocus);
els.addBoard.addEventListener("click", showAddBoard);
els.placeholderAdd.addEventListener("click", showAddBoard);
els.addPath.addEventListener("input", completePaths);
els.addPath.addEventListener("keydown", onPathKey);
els.addPath.addEventListener("blur", () => {
  paths.seq += 1;  // an answer still in flight must not reopen the list
  clearTimeout(paths.timer);
  renderPaths([]);
});
els.addClose.addEventListener("click", () => els.addDialog.close());
els.addForm.addEventListener("submit", (event) => {
  submitAddBoard(event);
});
els.addDialog.addEventListener("close", restoreFocus);
els.queueMore.addEventListener("click", () => {
  queueExpanded = !queueExpanded;
  schedule();
});
els.queueHead.addEventListener("click", () => {
  localStorage.setItem(QUEUE_KEY, localStorage.getItem(QUEUE_KEY) === "1" ? "0" : "1");
  schedule();
});
els.reloadConfig.addEventListener("click", () => {
  post("/api/reload")
    .then(() => say("Registry reloaded"))
    .catch(reportError);
});
document.addEventListener("visibilitychange", () => {
  scheduleHeartbeat();
  if (!document.hidden) {
    // A frame requested while hidden may never have run; ask for a fresh one.
    framePending = false;
    schedule();
  }
});

document.addEventListener("keydown", (event) => {
  if (els.logs.open || els.addDialog.open) return;  // the modal owns the keyboard
  if (event.ctrlKey && !event.metaKey && !event.altKey && event.key >= "0" && event.key <= "9") {
    event.preventDefault();
    const needle = needleNow();
    const index = event.key === "0" ? 9 : Number(event.key) - 1;
    const board = state.boards.filter((b) => matchesFilter(b, needle))[index];
    if (board) select(board.name);
    return;
  }
  if (event.target instanceof HTMLInputElement) {
    if (event.key === "Escape") event.target.blur();
    return;
  }
  if (event.altKey || event.metaKey || event.ctrlKey) return;
  if (event.key === "/") {
    event.preventDefault();
    els.filter.focus();
    return;
  }
  if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    showAddBoard();
    return;
  }
  if (event.key >= "1" && event.key <= "9") {
    const needle = needleNow();
    const board = state.boards.filter((b) => matchesFilter(b, needle))[Number(event.key) - 1];
    if (board) select(board.name);
    return;
  }
  if (event.key === "q") {
    els.queueHead.click();
    return;
  }
  if (event.key === "r") {
    els.reloadConfig.click();
    return;
  }
  if (!active) return;
  if (event.key === "w") unmount(active);
  else if (event.key === "d") detach(active);
  else if (event.key === "R") reloadPane(active);
});

fetch("/api/state")
  .then((response) => response.json())
  .then(absorb)
  .catch(reportError)
  .finally(connect);

// Durations tick locally; the server does not push frames for elapsed time.
setInterval(() => {
  if (!document.hidden) schedule();
}, 5000);
