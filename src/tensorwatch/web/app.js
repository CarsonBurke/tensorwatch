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
};

const ACTIVE_KEY = "tensorwatch.active";
const QUEUE_KEY = "tensorwatch.queue.collapsed";

let state = { boards: [], queue: null, server: { keep_warm: 2, queue_visible: 5 } };
let active = localStorage.getItem(ACTIVE_KEY) || null;
let queueExpanded = false;
let statusPane = null;
let statusKey = "";
let framePending = false;
/** Transient message shown instead of the totals line; survives renders. */
let notice = null;
let streamDown = false;

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

const byName = (name) => state.boards.find((b) => b.name === name) || null;
const queue = () => state.queue;

/* --------------------------------------------------------------------- api */

async function post(path) {
  const response = await fetch(path, { method: "POST" });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || response.statusText);
  }
  return response.json();
}

const act = (name, action) => post(`/api/boards/${encodeURIComponent(name)}/${action}`);

function say(text, ttl = 8000) {
  notice = { text, until: Date.now() + ttl };
  schedule();
}

function reportError(err) {
  say(`error: ${err && err.message ? err.message : err}`);
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
    // `demand` starts an on_demand board and keeps its idle timeout; `start`
    // would pin it running, silently turning it into an always board.
    const action = board.autostart === "on_demand" || board.state === "running" ? "demand" : "start";
    act(name, action).catch(action === "demand" ? () => {} : reportError);
  }
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
  renderBoards();
  renderQueue();
  renderPanes();
  renderTotals();
}

function renderTotals() {
  if (streamDown) {
    els.totals.textContent = "reconnecting to tensorwatch…";
    return;
  }
  if (notice && Date.now() < notice.until) {
    els.totals.textContent = notice.text;
    return;
  }
  notice = null;
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
  const power = iconButton("", "start/stop", (event) => {
    event.stopPropagation();
    const board_ = byName(board.name);
    const running = board_ && (board_.state === "running" || board_.state === "starting");
    act(board.name, running ? "stop" : "start").catch(reportError);
  });
  acts.append(
    power,
    iconButton("↻", "restart", (event) => {
      event.stopPropagation();
      act(board.name, "restart").catch(reportError);
    }),
    iconButton("▤", "log tail", (event) => {
      event.stopPropagation();
      showLogs(board.name);
    }),
    iconButton("↗", "open in its own window", (event) => {
      event.stopPropagation();
      detach(board.name);
    }),
  );
  // Queue activity for this board's project; stays visible on hover because it
  // answers "is this board's data moving right now?".
  const run = document.createElement("span");
  run.className = "run";
  const runq = document.createElement("span");
  runq.className = "runq";
  right.append(run, runq, idx, live, acts);

  el.append(dot, body, right);
  el.addEventListener("click", () => select(board.name));
  el.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select(board.name);
    }
  });
  els.boards.appendChild(el);

  row = { el, name, meta, power, idx, run, runq, key: "" };
  boardRows.set(board.name, row);
  return row;
}

function iconButton(glyph, title, handler) {
  const button = document.createElement("button");
  button.textContent = glyph;
  button.title = title;
  button.addEventListener("click", handler);
  return button;
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
      bits.push(board.state === "stopped" && board.autostart === "on_demand"
        ? "on demand"
        : board.message || board.state);
    }
    const meta = bits.join(" · ");
    const running = board.state === "running" || board.state === "starting";
    const filtered = !matchesFilter(board, needle);
    // Hotkeys count what is on screen, so a hidden board is never selectable.
    const number = filtered ? 0 : ++shown;
    const work = activity.get(board.name);
    const mark = runMark(board, work, live);
    const key = [
      board.state, board.target, board.message, meta, board.name === active,
      frames.has(board.name), index, number, filtered, mark.text, mark.queued, mark.title, live,
      board.writing,
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
    row.power.textContent = running ? "■" : "▶";
    row.power.title = running ? "stop" : "start";
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
      row.kill.title = running ? `kill #${job.id} ${job.name}` : `cancel #${job.id} ${job.name}`;
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
  const kill = iconButton("×", "cancel", (event) => {
    event.stopPropagation();
    const current = [...(queue()?.running || []), ...(queue()?.queued || [])]
      .find((candidate) => candidate.id === job.id);
    if (!current) return;
    const verb = current.state === "running" ? "kill" : "cancel";
    post(`/api/queue/${current.id}/cancel`)
      .then(() => say(`${verb} #${current.id} ${current.name}`, 4000))
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
  title.textContent = `${board.name} — ${board.state}`;
  const target = document.createElement("div");
  target.className = "muted";
  target.textContent = `${board.target} · port ${board.port} · autostart ${board.autostart}`;
  const message = document.createElement("p");
  message.className = "muted";
  message.textContent = board.message || "";

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
  );
  statusPane.append(title, target, message, row);

  if (!starting) {
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
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

/* -------------------------------------------------------------------- logs */

async function fetchLogs(name, lines) {
  const response = await fetch(`/api/boards/${encodeURIComponent(name)}/logs?lines=${lines}`);
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

/* ------------------------------------------------------------------ stream */

function absorb(payload) {
  state = payload;
  streamDown = false;
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

/** Tell the manager a human is watching, so on_demand boards stay up. */
function heartbeat() {
  if (document.hidden) return;
  const names = new Set(lru);
  if (active) names.add(active);
  for (const name of names) act(name, "demand").catch(() => {});
}

/* ------------------------------------------------------------------ events */

els.filter.addEventListener("input", schedule);
els.logsClose.addEventListener("click", () => els.logs.close());
// The opener lives in a hover-only action row, so the UA cannot restore focus.
els.logs.addEventListener("close", () => els.filter.focus());
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
    .then(() => say("registry reloaded", 4000))
    .catch(reportError);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    // A frame requested while hidden may never have run; ask for a fresh one.
    framePending = false;
    schedule();
  }
});

document.addEventListener("keydown", (event) => {
  if (els.logs.open) return;  // the modal owns the keyboard
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
setInterval(heartbeat, 60000);
