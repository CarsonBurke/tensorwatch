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
 *  - Sidebar rows are created once and patched in place. The overview, status
 *    pages and queue table are rebuilt only when what they show changes, and
 *    elapsed times tick by patching their own text. Renders are coalesced into
 *    an animation frame, and everything stops while the window is hidden.
 */

const $ = (id) => document.getElementById(id);
const els = {
  boards: $("boards"),
  boardsCount: $("boards-count"),
  boardsEmpty: $("boards-empty"),
  panes: $("panes"),
  view: $("view"),
  filter: $("filter"),
  filterIcon: $("filter-icon"),
  filterKey: $("filter-key"),
  filterCount: $("filter-count"),
  navOverview: $("nav-overview"),
  navOverviewIcon: $("nav-overview-icon"),
  ovCount: $("ov-count"),
  totals: $("totals"),
  openPalette: $("open-palette"),
  collapseRail: $("collapse-rail"),
  themeToggle: $("theme-toggle"),
  queue: $("queue"),
  queueHead: $("queue-head"),
  queueMeta: $("queue-meta"),
  queueChev: $("queue-chev"),
  queueJobs: $("queue-jobs"),
  queueMore: $("queue-more"),
  queueError: $("queue-error"),
  palette: $("palette"),
  paletteIcon: $("palette-icon"),
  paletteInput: $("palette-input"),
  paletteList: $("palette-list"),
  menu: $("menu"),
  logs: $("logs"),
  logsTitle: $("logs-title"),
  logsPath: $("logs-path"),
  logsBody: $("logs-body"),
  logsClose: $("logs-close"),
  logsRefresh: $("logs-refresh"),
  logsCopy: $("logs-copy"),
  addBoard: $("add-board"),
  addDialog: $("add-board-dialog"),
  addClose: $("add-board-close"),
  addCancel: $("add-board-cancel"),
  addForm: $("add-board-form"),
  addPath: $("add-board-path"),
  addPaths: $("add-board-paths"),
  addName: $("add-board-name"),
  addAutostart: $("add-board-autostart"),
  addModes: $("add-board-modes"),
  addError: $("add-board-error"),
  suggestions: $("suggestions"),
  suggestionsMeta: $("suggestions-meta"),
  suggestionsList: $("suggestions-list"),
  toasts: $("toasts"),
};

const ACTIVE_KEY = "tensorwatch.active";
const QUEUE_KEY = "tensorwatch.queue.collapsed";
const THEME_KEY = "tensorwatch.theme";
const RAIL_KEY = "tensorwatch.rail";

let state = { boards: [], queue: null, server: { keep_warm: 2, queue_visible: 5 } };
let active = localStorage.getItem(ACTIVE_KEY) || null;
let viewKey = "";
let framePending = false;
let streamDown = false;
/** Static buttons get their icons on the first render, not at load. */
let chromeReady = false;
/** A board the user just added; opened as soon as the manager reports it. */
let pendingSelect = null;
/** Scroll the open board's row into view after the next render (it grows when opened). */
let revealActive = true;
let heartbeatTimer = null;
let heartbeatKey = "";
let queueExpanded = false;
/** Queue job id -> confirmation state and timer. */
const armed = new Map();
/**
 * The log tail shown on the open status page and matched for hints:
 * {name, state, restarts, text, error, pending}. Fetched again when the board
 * changes state or crashes once more, never on an unrelated render.
 */
let statusLog = null;

/** name -> HTMLIFrameElement */
const frames = new Map();
/** mount order, least recent first */
const lru = [];
/** name -> {el, name, meta, ...} */
const boardRows = new Map();
/** job id -> {el, ...} */
const jobRows = new Map();

/* --------------------------------------------------------------- formatting */

const nowSec = () => Date.now() / 1000;
const capital = (text) => (text ? text[0].toUpperCase() + text.slice(1) : "");

function fmtBytes(n) {
  if (!n) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** A duration in the largest unit that reads naturally ("8s", "3 min", "2 h 5 min", "4 days"). */
function dur(secs, long = false) {
  const s = Math.max(0, secs);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 172800) {
    const hours = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    return long && mins ? `${hours} h ${mins} min` : `${hours} h`;
  }
  return `${Math.floor(s / 86400)} days`;
}

const fmtAge = (since) => (since ? dur(nowSec() - since) : "–");

/** Shorten a path by whole folders, never mid-word: "~/…/cleanrl/runs". */
function shortPath(path) {
  const parts = String(path || "").split("/");
  if (parts.length <= 4) return path;
  return `${parts[0]}/…/${parts.slice(-2).join("/")}`;
}

/** name -> board, rebuilt with each state so lookups in render loops stay O(1). */
let boardIndex = new Map();
const byName = (name) => boardIndex.get(name) || null;
const queue = () => state.queue;
const allJobs = () => (queue() ? [...queue().running, ...queue().queued] : []);

/* ------------------------------------------------------------------ words */

const SIGNALS = { 1: "SIGHUP", 2: "SIGINT", 6: "SIGABRT", 9: "SIGKILL", 11: "SIGSEGV", 15: "SIGTERM" };

/** Negative exit codes are signals (Popen convention). */
function exitText(code) {
  if (code === null || code === undefined) return "";
  return code < 0 ? `killed by ${SIGNALS[-code] || `signal ${-code}`}` : `exit code ${code}`;
}
const exitShort = (code) => (code < 0 ? "killed" : `exit ${code}`);

/** The child's own error line from a backoff message, without the frozen "retrying in Ns". */
function crashDetail(board) {
  const match = /^exited with code -?\d+; retrying in \d+s(?: - ([\s\S]*))?$/.exec(board.message || "");
  return match ? match[1] || "" : board.message || "";
}

/** board name -> its last crash cause, kept through the "starting" half of a crash loop. */
const lastCrash = new Map();

/**
 * Why a crashing board went down. Timeouts and dead ports are restarts too,
 * but their exit code is the SIGTERM TensorWatch sent, so it is not the story.
 */
function crashCause(board) {
  const message = board.message || "";
  let cause = null;
  if (/^exited with code/.test(message)) {
    cause = { exited: true, short: `Crashed (${exitShort(board.last_exit)})`, detail: crashDetail(board) };
  } else if (message.startsWith("start timed out")) {
    cause = { exited: false, short: "Timed out starting", detail: capital(message) };
  } else if (message.startsWith("restarting: port stopped")) {
    cause = { exited: false, short: "Stopped responding", detail: "It stopped accepting connections, so TensorWatch restarted it." };
  }
  if (cause && board.state === "backoff") lastCrash.set(board.name, cause);
  return cause || lastCrash.get(board.name)
    || { exited: board.last_exit !== null && board.last_exit !== undefined, short: `Crashed (${exitShort(board.last_exit)})`, detail: "" };
}

/**
 * A crash loop alternates between backoff and starting; restarts only resets
 * after a stable run or an explicit start, so a start with restarts > 0 is
 * another attempt, not a fresh start. Treating it as crashing keeps rows and
 * pages from flickering through "Starting…" every few seconds.
 */
const isRetry = (b) => b.state === "starting" && b.restarts > 0;
const isCrashing = (b) => b.state === "backoff" || isRetry(b);
/** The state a board is shown in: a retry inside a crash loop still reads as crashing. */
const shownState = (b) => (isRetry(b) ? "backoff" : b.state);
/** Wanted running: a crashing or failing board is still on, and Stop is what ends its retries. */
const isOn = (b) => b.state !== "stopped" && b.state !== "disabled";
const isStoppedByUser = (b) => b.state === "stopped" && b.message === "stopped on request";
const isAsleep = (b) => b.state === "stopped" && b.autostart === "on_demand" && !isStoppedByUser(b);
const preSpawn = (b) => /^(logdir does not exist|port \d+ is already in use|cannot start)/.test(b.message || "");

function stateWord(board) {
  switch (board.state) {
    case "running": return "Running";
    case "starting": return isRetry(board) ? "Crashing" : board.autostart === "on_demand" ? "Waking up" : "Starting";
    case "backoff": return "Crashing";
    case "failed": return "Can't start";
    case "disabled": return "Disabled";
    default: return isAsleep(board) ? "Asleep" : isStoppedByUser(board) ? "Stopped by you" : "Stopped";
  }
}

const stateTone = (board) =>
  ({ running: "ok", starting: "warn", backoff: "warn", failed: "bad" })[board.state] || "soft";

const MODE_WORD = { always: "Always", on_demand: "On demand", manual: "Manual" };
const MODE_HINT = {
  always: "Runs whenever TensorWatch runs",
  on_demand: "Starts when you open it and sleeps once nobody is watching",
  manual: "Runs only when you start it",
};

const REASONS = {
  gpu_full: "Waiting for a free slot",
  protected_drain: "Paused while a protected job drains",
  protected_job: "A protected job holds the slots",
  held: "On hold",
};
const reasonText = (reason) => REASONS[reason] || reason || "Waiting";

/** When an on_demand board nobody is watching will be stopped, in epoch seconds. */
function sleepsAt(board) {
  if (board.autostart !== "on_demand" || board.state !== "running") return null;
  if (board.demanded_ago === null || board.demanded_ago === undefined || !board.idle_timeout) return null;
  // Open and kept-warm panes are renewed by the heartbeat, so they never sleep.
  if (board.name === active || lru.includes(board.name)) return null;
  return (state.now || nowSec()) - board.demanded_ago + board.idle_timeout;
}

/** What a board is doing, in the few words a sidebar row has room for. */
function summary(board) {
  switch (board.state) {
    case "running": {
      const bits = [`up ${fmtAge(board.since)}`];
      if (board.rss_bytes) bits.push(fmtBytes(board.rss_bytes));
      const sleeps = sleepsAt(board);
      if (sleeps) bits.push(`sleeps in ${dur(sleeps - nowSec())}`);
      return bits.join(" · ");
    }
    case "starting":
      if (isRetry(board)) return `${crashCause(board).short} · retrying`;
      return board.autostart === "on_demand" ? "Waking up…" : "Starting…";
    case "backoff":
      return `${crashCause(board).short} · retrying`;
    case "failed": {
      const message = board.message || "";
      if (message.startsWith("logdir does not exist")) return "Log directory is missing";
      if (/already in use/.test(message)) return `Port ${board.port} is taken`;
      if (message.startsWith("cannot start")) return "Can't launch its command";
      if (message.startsWith("start timed out")) return "Timed out while starting";
      return message || "Can't start";
    }
    case "disabled":
      return "Disabled in the registry";
    default:
      if (isStoppedByUser(board)) return "Stopped by you";
      if (isAsleep(board)) return "Asleep · wakes when opened";
      return "Stopped · starts when opened";
  }
}

/**
 * Likely causes, guessed from the message, the exit code and the log tail.
 * Each says what it matched, so a wrong guess is easy to dismiss.
 */
function hints(board, logText = "") {
  const out = [];
  const message = board.message || "";
  if (/Too many open files|os error 24|EMFILE/.test(logText)) {
    out.push({
      title: "It ran out of open files.",
      body: ["Each event file holds a file descriptor. Run ", code("tensorwatch doctor"),
        ": the systemd service raises the limit, but a copy started from a shell keeps the default 1,024."],
      source: "Matched “Too many open files” in the log",
    });
  }
  if (board.last_exit === -9 && isCrashing(board) && crashCause(board).exited) {
    out.push({
      title: "It was killed by the system.",
      body: ["SIGKILL with no traceback usually means it ran out of memory. Check with ",
        code("journalctl -k -g oom --since -1h"), "."],
      source: "From the exit code",
    });
  }
  if (message.startsWith("logdir does not exist")) {
    out.push({
      title: "The log directory is gone.",
      body: ["Point the board at another directory in the registry, or remove it if the runs are gone for good."],
      source: "From the supervisor message",
    });
  }
  if (/already in use/.test(message)) {
    out.push({
      title: `Another program is using port ${board.port}.`,
      body: [code(`ss -ltnp 'sport = :${board.port}'`), " shows which one."],
      source: "From the supervisor message",
    });
  }
  return out;
}

/* ---------------------------------------------------------------------- dom */

/** Build an element: h("button", {class: "btn", onclick}, "text", child). */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "title") el.title = value;
    else if (key === "hidden") el.hidden = true;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Left-to-right marks around a path shown in an rtl box (which clips its start, not its end). */
const ltr = (text) => `\u200e${text}\u200e`;
const code = (text) => h("code", { text });
const kbd = (text) => h("kbd", { text });

/** Elapsed time since `t`, kept current by `tick()` without a render. */
const since = (t, long = false) =>
  h("span", { dataset: long ? { since: t, long: "1" } : { since: t }, text: dur(nowSec() - t, long) });
/** Time left until `t`; a retry that is due reads "a moment" until the next state frame. */
const untilText = (secs) => (secs > 0.5 ? dur(secs) : "a moment");
const until = (t) => h("span", { dataset: { until: t }, text: untilText(t - nowSec()) });
/** Elapsed time with no timestamp to count from. */
const sinceOrDash = (t, long = false) => (t ? since(t, long) : h("span", { text: "–" }));

/**
 * Values that change every stats frame (memory, CPU, data age) are marked
 * `data-live` and patched where they stand, so the overview and status pages
 * are rebuilt only when their structure changes. A rebuild drops hover,
 * selection, horizontal scroll and the anchor of an open menu.
 */
const liveField = (kind, id, el) => {
  el.dataset.live = `${kind}:${id}`;
  return el;
};
const LIVE = {
  mem: (b) => (b.state === "running" ? fmtBytes(b.rss_bytes) : "–"),
  cpu: (b) => (b.state === "running" ? `${(b.cpu_percent || 0).toFixed(1)}%` : "–"),
};

function patchLive(container) {
  if (typeof container.querySelectorAll !== "function") return;
  const t = nowSec();
  for (const el of container.querySelectorAll("[data-live]")) {
    const [kind, id] = el.dataset.live.split(/:(.*)/s);
    if (kind === "memtotal") {
      el.textContent = fmtBytes(state.boards.reduce((sum, b) => sum + (b.state === "running" ? b.rss_bytes || 0 : 0), 0));
      continue;
    }
    const board = byName(id);
    if (!board) continue;
    if (kind === "event") {
      if (board.last_event && Number(el.dataset.since) !== board.last_event) {
        el.dataset.since = board.last_event;
        el.textContent = dur(t - board.last_event);
      }
    } else if (LIVE[kind]) {
      const text = LIVE[kind](board);
      if (el.textContent !== text) el.textContent = text;
    }
  }
}

function tick() {
  if (typeof document.querySelectorAll !== "function") return;
  const t = nowSec();
  for (const el of document.querySelectorAll("[data-since]")) {
    el.textContent = dur(t - Number(el.dataset.since), Boolean(el.dataset.long));
  }
  for (const el of document.querySelectorAll("[data-until]")) {
    el.textContent = untilText(Number(el.dataset.until) - t);
  }
  for (const el of document.querySelectorAll("[data-limit-from]")) {
    const fraction = Math.min(1, (t - Number(el.dataset.limitFrom)) / Number(el.dataset.limitSpan));
    el.style.width = `${(fraction * 100).toFixed(0)}%`;
  }
}

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

/**
 * Start a board the way its policy means it. An on_demand board is woken with
 * `demand`, which also lifts a stop; `start` would pin it on so it never sleeps.
 */
function startBoard(name) {
  const board = byName(name);
  return act(name, board && board.autostart === "on_demand" ? "demand" : "start").catch(reportError);
}
const stopBoard = (name) => act(name, "stop").catch(reportError);

function setMode(name, mode) {
  const board = byName(name);
  if (!board || board.autostart === mode) return;
  post(`${boardPath(name)}/autostart`, { mode })
    .then(() => say(`${name} now runs ${({ always: "always", on_demand: "on demand", manual: "only when started" })[mode]}`))
    .catch(reportError);
}

function copy(text, what = `“${text}”`) {
  const clip = typeof navigator !== "undefined" && navigator.clipboard;
  if (!clip) return;
  clip.writeText(text).then(() => say(`Copied ${what}`), reportError);
}

/* ------------------------------------------------------------------- icons */

// 24px grid, drawn as strokes so they take the text colour and stay crisp small.
const ICONS = {
  play: "M8 5.5v13l10.5-6.5z",
  stop: "M7 7h10v10H7z",
  pause: "M9 6v12M15 6v12",
  circle: "M12 6.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11z",
  restart: "M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.6h-4.6",
  refresh: "M4.5 12a7.5 7.5 0 0 1 12.8-5.3L19.5 9M19.5 4.5V9H15M19.5 12a7.5 7.5 0 0 1-12.8 5.3L4.5 15M4.5 19.5V15H9",
  log: "M6 3.5h8.5l3.5 3.5v13.5H6zM9.5 11h5M9.5 14.5h5M9.5 18h3",
  external: "M13.5 5H19v5.5M19 5l-8 8M10 7H6v11h11v-4",
  trash: "M5 7.5h14M10 7.5V5h4v2.5M7 7.5l.9 11.5h8.2L17 7.5",
  plus: "M12 5.5v13M5.5 12h13",
  close: "M6.5 6.5l11 11M17.5 6.5l-11 11",
  home: "M3.5 10.5 12 3.5l8.5 7V20h-5.5v-6h-6v6H3.5z",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-3.5-3.5",
  command: "M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z",
  side: "M4 4.5h16v15H4zM9.5 4.5v15",
  wave: "M3 12h4l3-7 4 14 3-7h4",
  pane: "M3.5 5h17v14h-17zM3.5 9.5h17",
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4",
  more: "M5.5 12h.01M12 12h.01M18.5 12h.01",
  copy: "M8.5 8.5h11v11h-11zM15.5 8.5v-4h-11v11h4",
  alert: "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM12 7.5v5.5M12 16.5v.01",
  bulb: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z",
  chev: "M6 9l6 6 6-6",
  check: "M5 12.5 9.5 17 19 7.5",
  queue: "M4 6h16M4 12h16M4 18h10",
};
const SVG_NS = "http://www.w3.org/2000/svg";

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `icon i-${name}`);
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

function iconButton(name, label, handler, className = "icon-btn") {
  const button = h("button", { type: "button", class: className, onclick: handler });
  setIcon(button, name);
  setLabel(button, label);
  return button;
}

function textButton(label, handler, className = "") {
  return h("button", { type: "button", class: className || null, onclick: handler, text: label });
}

/** A labelled status-page button, optionally with an icon and its shortcut. */
function button(label, handler, { iconName = null, primary = false, key = null, act = null } = {}) {
  // data-act lets a rebuilt page put the focus back on the same button.
  return h("button", { type: "button", class: `btn${primary ? " primary" : ""}`, onclick: handler,
    dataset: { act: act || (typeof label === "string" ? label : "") } },
    iconName && icon(iconName), label, key && kbd(key));
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
  if (kind === "error") el.setAttribute("role", "alert");
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
  el.append(iconButton("close", "Dismiss", dismiss));
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
    els.panes.insertBefore(frame, els.view);
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
 * the status page explains it and the pane returns by itself.
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
  closeMenu();
  revealActive = true;
  const board = byName(name);
  if (board) {
    // The heartbeat starts on_demand boards without pinning them running.
    // Only a stopped board with another policy needs an explicit start: a
    // crashing or failing one is already wanted, and `start` would reset its
    // backoff, so merely looking at a crash loop would make it spin faster.
    if (board.autostart !== "on_demand" && board.state === "stopped") {
      act(name, "start").catch(reportError);
    }
  }
  scheduleHeartbeat(true);
  schedule();
}

/** Show the overview; open panes stay mounted (and warm) behind it. */
function showOverview() {
  active = null;
  localStorage.removeItem(ACTIVE_KEY);
  closeMenu();
  scheduleHeartbeat(true);
  schedule();
}

/**
 * Open a board in its own window. The window loads a dashboard page that frames
 * the board, so it keeps TensorWatch running after this window closes.
 */
function detach(name) {
  if (byName(name)) window.open(`/boards/${encodeURIComponent(name)}`, `tb-${name}`, "noopener");
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
    setIcon(els.addBoard, "plus");
    setLabel(els.addBoard, "Add a board (+)");
    setIcon(els.openPalette, "command");
    setLabel(els.openPalette, "Search and commands (Ctrl K)");
    setIcon(els.collapseRail, "side");
    renderRailLabel();
    setIcon(els.logsClose, "close");
    setLabel(els.logsClose, "Close the log (Esc)");
    setIcon(els.addClose, "close");
    setLabel(els.addClose, "Close");
    els.filterIcon.replaceChildren(icon("search"));
    els.navOverviewIcon.replaceChildren(icon("home"));
    els.paletteIcon.replaceChildren(icon("search"));
    els.queueChev.replaceChildren(icon("chev"));
  }
  renderPanes();
  renderBoards();
  if (revealActive && active && boardRows.has(active)) {
    revealActive = false;
    boardRows.get(active).el.scrollIntoView?.({ block: "nearest" });
  }
  renderQueue();
  renderTotals();
  renderThemeToggle();
  scheduleHeartbeat();
}

function renderTotals() {
  els.boardsCount.textContent = state.boards.length ? `${state.boards.length}` : "";
  const failed = state.boards.filter((b) => b.state === "failed").length;
  const crashing = state.boards.filter(isCrashing).length;
  const running = state.boards.filter((b) => b.state === "running");
  els.ovCount.textContent = failed
    ? `${failed} can't start`
    : crashing
      ? `${crashing} crashing`
      : state.boards.length ? `${running.length} of ${state.boards.length} running` : "";
  els.ovCount.className = `count${failed ? " bad" : crashing ? " warn" : ""}`;
  els.navOverview.classList.toggle("active", active === null);

  if (streamDown) {
    els.totals.replaceChildren(h("span", { class: "down", text: "Reconnecting to TensorWatch…" }));
    delete els.totals.dataset.key;
    return;
  }
  const rss = running.reduce((sum, b) => sum + (b.rss_bytes || 0), 0);
  const cpu = running.reduce((sum, b) => sum + (b.cpu_percent || 0), 0);
  const text = `${running.length}|${fmtBytes(rss)}|${cpu.toFixed(0)}`;
  if (els.totals.dataset.key === text) return;
  els.totals.dataset.key = text;
  els.totals.replaceChildren(
    h("span", {}, h("b", { text: String(running.length) }), " running"),
    h("span", { title: "Memory used by running boards" }, h("b", { text: fmtBytes(rss) })),
    h("span", {}, h("b", { text: `${cpu.toFixed(0)}%` }), " CPU"),
  );
}

/* ------------------------------------------------------------- board rows */

function boardRow(board) {
  let row = boardRows.get(board.name);
  if (row) return row;
  const name = board.name;

  const dot = h("span", { class: "dot", "aria-hidden": "true" });
  const label = h("span", { class: "b-name", text: name });
  const live = h("span", { class: "act", hidden: true });
  const jobs = h("span", { class: "pill", hidden: true });
  const idx = h("span", { class: "b-key", "aria-hidden": "true" });
  const meta = h("span", { class: "b-meta" });
  const warm = h("span", { title: "Kept loaded for instant switching", hidden: true }, icon("pane"));
  const demand = h("span", { title: MODE_HINT.on_demand, hidden: true }, icon("moon"));
  const more = iconButton("more", `Actions for ${name}`, (event) => {
    event.stopPropagation();
    toggleMenu(name, more);
  }, "icon-btn b-more");
  more.setAttribute("aria-haspopup", "menu");
  const ext = h("div", { class: "b-ext", hidden: true, onclick: (event) => event.stopPropagation() });

  const el = h("div", { class: "board", role: "option", tabindex: "0", dataset: { name } },
    dot,
    h("span", { class: "b-l1" }, label, h("span", { class: "b-r1" }, live, jobs, idx)),
    h("span", { class: "b-l2" }, meta, h("span", { class: "b-r2" }, warm, demand)),
    more, ext);
  el.addEventListener("click", () => {
    if (active !== name) select(name);
  });
  el.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    openMenu(name, event.clientX, event.clientY, el);
  });
  el.addEventListener("keydown", (event) => {
    if (event.target !== el) return;  // a key on one of the row's own buttons
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select(name);
    } else if (event.key === "Delete") {
      event.preventDefault();
      removeBoard(name);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!focusNeighbour(el, event.key === "ArrowDown" ? 1 : -1) && event.key === "ArrowUp") els.filter.focus();
    } else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
      event.preventDefault();
      const box = el.getBoundingClientRect();
      openMenu(name, box.left + 40, box.bottom, el);
    }
  });
  els.boards.appendChild(el);

  row = { el, dot, live, jobs, idx, meta, warm, demand, ext, key: "", extKey: "" };
  boardRows.set(name, row);
  return row;
}

/** Move focus to the next visible board row; the list is short, so no roving index. */
function focusNeighbour(el, step) {
  const rows = [...els.boards.children].filter((row) => !row.classList.contains("hidden"));
  const next = rows[rows.indexOf(el) + step];
  if (next) next.focus();
  return Boolean(next);
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

const EMPTY_WORK = { running: [], queued: [] };

/** Tooltip for a row: where it reads from, what the supervisor says, and its jobs. */
function rowTitle(board, work, live, number) {
  const lines = [board.target, `${stateWord(board)}: ${board.message || board.state}`];
  if (board.last_event) {
    lines.push(board.writing ? `Receiving data, last write ${fmtAge(board.last_event)} ago`
      : `Quiet, last write ${fmtAge(board.last_event)} ago`);
  }
  if (!live && (work.running.length || work.queued.length)) lines.push("mlq is offline; last known jobs:");
  for (const job of work.running) lines.push(`mlq #${job.id} ${job.name}: running for ${fmtAge(job.since)}`);
  for (const job of work.queued) lines.push(`mlq #${job.id} ${job.name}: ${reasonText(job.reason).toLowerCase()}`);
  if (number && number <= 10) lines.push(`Press ${number % 10} to open`);
  return lines.join("\n");
}

const needleNow = () => els.filter.value.trim().toLowerCase();
const matchesFilter = (board, needle) =>
  !needle || `${board.name} ${board.target}`.toLowerCase().includes(needle);
const visibleBoards = () => state.boards.filter((board) => matchesFilter(board, needleNow()));

function renderBoards() {
  const needle = needleNow();
  let shown = 0;
  const activity = activityByBoard();
  const live = Boolean(queue() && queue().connected);
  const seen = new Set();

  state.boards.forEach((board, index) => {
    const row = boardRow(board);
    seen.add(board.name);
    const filtered = !matchesFilter(board, needle);
    // Hotkeys count what is on screen, so a hidden board is never selectable.
    const number = filtered ? 0 : ++shown;
    const work = activity.get(board.name) || EMPTY_WORK;
    const isActive = board.name === active;
    const meta = summary(board);
    const age = board.writing ? fmtAge(board.last_event) : "";
    const title = rowTitle(board, work, live, number);
    const key = [
      shownState(board), meta, isActive, frames.has(board.name), index, number, filtered, age, title,
      work.running.length, work.queued.length, board.autostart,
    ].join("|");
    if (row.key !== key) {
      row.key = key;
      row.el.className = `board state-${shownState(board)}${isActive ? " active" : ""}${filtered ? " hidden" : ""}`;
      row.el.setAttribute("aria-selected", String(isActive));
      row.el.title = title;
      row.dot.className = `dot ${shownState(board)}`;
      row.meta.textContent = meta;
      row.live.hidden = !board.writing;
      if (board.writing) row.live.replaceChildren(icon("wave"), age);
      const running = work.running.length;
      const waiting = work.queued.length;
      row.jobs.hidden = !running && !waiting;
      row.jobs.className = `pill${running ? "" : " soft"}`;
      row.jobs.title = `mlq: ${running} running, ${waiting} waiting`;
      row.jobs.replaceChildren(
        ...(running ? [icon("play"), String(running)] : []),
        ...(running && waiting ? [h("span", { class: "pw", text: `+${waiting}` })] : []),
        ...(!running && waiting ? [`${waiting} queued`] : []),
      );
      row.idx.textContent = number && number <= 10 ? String(number % 10) : "";
      row.warm.hidden = !frames.has(board.name) || isActive;
      row.demand.hidden = board.autostart !== "on_demand";
    }
    renderExt(row, board, isActive);
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
    ? "No boards yet. Add a log directory with +."
    : shown ? "" : `No board matches “${els.filter.value.trim()}”.`;
  els.boardsEmpty.hidden = !empty;
  els.boardsEmpty.textContent = empty;
  els.filterKey.hidden = Boolean(needle);
  els.filterCount.hidden = !needle;
  els.filterCount.textContent = `${shown} of ${state.boards.length}`;
}

/** The open board's row also carries its path, run policy and actions. */
function renderExt(row, board, isActive) {
  const running = isOn(board);
  const key = isActive ? [board.state, board.autostart, board.target, board.port].join("|") : "";
  if (row.extKey === key) return;
  row.extKey = key;
  row.ext.hidden = !isActive;
  if (!isActive) {
    row.ext.replaceChildren();
    return;
  }
  const name = board.name;
  const modes = h("span", { class: "seg", role: "group", "aria-label": "When it runs" },
    ...Object.entries(MODE_WORD).map(([mode, word]) => h("button", {
      type: "button", title: MODE_HINT[mode], "aria-pressed": String(board.autostart === mode),
      onclick: () => setMode(name, mode),
    }, word)));
  const power = board.state === "disabled" ? null
    : iconButton(running ? "stop" : "play", running ? "Stop" : "Start", () => (running ? stopBoard(name) : startBoard(name)),
      `icon-btn${running ? " danger" : ""}`);
  row.ext.replaceChildren(
    h("div", { class: "b-path" },
      h("button", { type: "button", title: `${board.target}\nClick to copy`, onclick: () => copy(board.target, "the path") }, shortPath(board.target)),
      h("span", { text: `port ${board.port}` })),
    modes,
    h("div", { class: "b-acts" },
      iconButton("restart", "Restart TensorBoard", () => act(name, "restart").then(() => say(`Restarting ${name}`)).catch(reportError)),
      power,
      iconButton("log", "Log (l)", () => showLogs(name)),
      iconButton("external", "Open in its own window (d)", () => detach(name)),
      h("span", { class: "grow" }),
      iconButton("close", "Close and unload this board (w)", () => closeActive())),
  );
}

function closeActive() {
  if (!active) return;
  const name = active;
  unmount(name);
  if (active === null) els.navOverview.focus?.();
}

/* ------------------------------------------------------------------- queue */

const subHeads = new Map();
const subHead = (text) => {
  if (!subHeads.has(text)) subHeads.set(text, h("div", { class: "rq-sub", text }));
  return subHeads.get(text);
};

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
  els.queue.classList.toggle("expanded", queueExpanded);
  els.queueHead.setAttribute("aria-expanded", String(!collapsed));
  els.queueMore.setAttribute("aria-expanded", String(queueExpanded));

  const limit = snapshot.effective_limit;
  const slots = snapshot.connected && limit
    ? h("span", { class: "slots", title: `${snapshot.active_leases} of ${limit} slots in use` },
      ...Array.from({ length: Math.min(limit, 8) }, (_, i) => h("i", { class: i < snapshot.active_leases ? "on" : null })))
    : null;
  const metaKey = [snapshot.connected, snapshot.admission_blocked, snapshot.active_leases, limit, snapshot.queued.length].join("|");
  if (els.queueMeta.dataset.key !== metaKey) {
    els.queueMeta.dataset.key = metaKey;
    els.queueMeta.className = `sect-meta${!snapshot.connected ? " bad" : snapshot.admission_blocked ? " warn" : ""}`;
    els.queueMeta.replaceChildren(
      ...(slots ? [slots] : []),
      !snapshot.connected ? "offline" : snapshot.admission_blocked ? "admission blocked" : `${snapshot.queued.length} waiting`,
    );
  }
  // Daemon errors can be paragraphs; keep the panel small and put the rest in the tooltip.
  const detail = (snapshot.error || "").replace(/\s+/g, " ");
  els.queueError.hidden = Boolean(snapshot.connected);
  els.queueError.textContent = `mlq isn't responding. Showing the last known state.${detail ? ` ${detail}` : ""}`;
  els.queueError.title = detail;

  // Running jobs always show (there are only as many as slots), then the open
  // board's waiting jobs, then the remaining jobs within the preview limit.
  const visible = Math.max(1, state.server.queue_visible || 5);
  const mine = [];
  const rest = [];
  for (const job of snapshot.queued) {
    (active && job.boards?.includes(active) ? mine : rest).push(job);
  }
  const budget = Math.max(0, visible - snapshot.running.length - mine.length);
  const shownRest = queueExpanded ? rest : rest.slice(0, budget);
  const focused = document.activeElement;
  const hadFocus = focused && els.queueJobs.contains?.(focused);
  const nodes = [];
  const seen = new Set();
  const add = (job) => {
    seen.add(job.id);
    nodes.push(jobRow(job, snapshot).el);
  };
  if (snapshot.running.length) {
    nodes.push(subHead("Running"));
    snapshot.running.forEach(add);
  }
  if (mine.length) {
    nodes.push(subHead(`${active}'s jobs`));
    mine.forEach(add);
  }
  if (shownRest.length) {
    nodes.push(subHead(mine.length ? "Other jobs" : "Waiting"));
    shownRest.forEach(add);
  }
  for (const [id] of jobRows) if (!seen.has(id)) jobRows.delete(id);
  const current = els.queueJobs.children;
  if (current.length !== nodes.length || nodes.some((node, i) => current[i] !== node)) {
    const scroll = els.queueJobs.scrollTop;
    els.queueJobs.replaceChildren(...nodes);
    els.queueJobs.scrollTop = scroll;
  }
  if (hadFocus) {
    if (!els.queueJobs.contains(focused) || focused.hidden || focused.disabled) {
      els.queueMore.focus();
    } else if (document.activeElement !== focused) {
      focused.focus({ preventScroll: true });
    }
  }

  const hidden = rest.length - shownRest.length;
  const moreText = queueExpanded ? "Show compact queue" : `${hidden > 0 ? `${hidden} more · ` : ""}Show full queue`;
  if (els.queueMore.dataset.key !== moreText) {
    els.queueMore.dataset.key = moreText;
    els.queueMore.replaceChildren(h("span", { text: moreText }), kbd("⇧ Q"));
  }
}

function toggleQueueExpanded(expand = !queueExpanded) {
  queueExpanded = expand;
  if (expand) {
    localStorage.setItem(QUEUE_KEY, "0");
    if (railCollapsed()) setRail(false);
  }
  schedule();
  requestAnimationFrame(() => els.queueMore.focus());
}

/** One line per job: its name, then whatever is unusual about it. The rest is in the tooltip. */
function jobRow(job, snapshot) {
  let row = jobRows.get(job.id);
  if (!row) {
    const glyph = h("span", { class: "glyph" });
    const name = h("span", { class: "jname" });
    const when = h("span", { class: "when" });
    const link = h("button", { type: "button", class: "job", onclick: () => openJobBoard(job.id) }, glyph, name, when);
    const cancel = h("button", { type: "button", class: "btn sm", onclick: () => cancelJob(job.id) });
    const el = h("div", { class: "queue-job" }, link, cancel);
    row = { el, link, cancel, glyph, name, when, key: "", actionKey: "" };
    jobRows.set(job.id, row);
  }
  const running = job.state === "running";
  const tries = Number.parseInt(job.attempts, 10) || 0;
  const aside = running
    ? `${tries > 1 ? `try ${tries} · ` : ""}${job.since ? fmtAge(job.since) : ""}`
    : job.state === "held" ? "on hold"
      : job.reason === "protected_drain" ? "draining"
        : job.priority ? `priority ${job.priority}` : "";
  const tag = job.board || (job.boards || [])[0] || "";
  const title = [
    `#${job.id} ${job.name}`,
    running ? (job.since ? `Running for ${dur(nowSec() - job.since, true)}` : "Running") : reasonText(job.reason),
    running && tries > 1 ? `Attempt ${job.attempts}` : "",
    `${tag ? `Board: ${tag}` : `Project: ${job.project}`} · ${job.cwd}`,
    snapshot.connected ? "" : "mlq is offline; this is the last known state",
  ].filter(Boolean).join("\n");
  const linked = Boolean(tag && byName(tag));
  const key = [job.state, job.name, aside, title, linked].join("|");
  if (row.key !== key) {
    row.key = key;
    row.link.className = `job ${job.state}${linked ? " linked" : ""}`;
    row.link.disabled = !linked;
    row.link.title = title;
    row.glyph.replaceChildren(icon(running ? "play" : job.state === "held" ? "pause" : "circle"));
    row.name.textContent = job.name;
    row.when.textContent = aside;
  }
  const confirm = isJobArmed(job);
  const action = running ? "Stop" : "Cancel";
  const hideAction = !queueExpanded || !snapshot.connected;
  const actionKey = [hideAction, confirm, action, job.name].join("|");
  if (row.actionKey !== actionKey) {
    row.actionKey = actionKey;
    row.cancel.hidden = hideAction;
    row.cancel.classList.toggle("confirm", confirm);
    row.cancel.textContent = confirm ? `${action}?` : action;
    setLabel(row.cancel, `${action}${confirm ? " confirmation for" : ""} job #${job.id} ${job.name}`);
  }
  return row;
}

function openJobBoard(id) {
  const job = allJobs().find((candidate) => candidate.id === id);
  const target = job && (job.board || (job.boards || [])[0]);
  if (target && byName(target)) select(target);
}

/** Kill and cancel take a second click within 3 s; a stray click costs a job hours. */
const isJobArmed = (job) => armed.get(job.id)?.running === (job.state === "running");

function cancelJob(id) {
  const job = allJobs().find((candidate) => candidate.id === id);
  if (!job || !queue()?.connected) return;
  const running = job.state === "running";
  clearTimeout(armed.get(id)?.timer);
  if (!isJobArmed(job)) {
    const timer = setTimeout(() => {
      armed.delete(id);
      viewKey = "";
      schedule();
    }, 3000);
    armed.set(id, { running, timer });
    viewKey = "";
    schedule();
    return;
  }
  armed.delete(id);
  viewKey = "";
  post(`/api/queue/${job.id}/cancel`)
    .then(() => say(running ? `Stopping #${job.id} ${job.name}. Its slot frees up once it exits.` : `Cancelled #${job.id} ${job.name}`))
    .catch(reportError);
  schedule();
}

function queueTable() {
  const snapshot = queue();
  if (!snapshot) return h("p", { class: "muted", text: "mlq is not set up." });
  const rows = allJobs().map((job) => {
    const running = job.state === "running";
    const tries = Number.parseInt(job.attempts, 10) || 0;
    const tag = job.board || (job.boards || [])[0] || "";
    const fraction = running && job.time_limit && job.since ? Math.min(1, (nowSec() - job.since) / job.time_limit) : null;
    const isArmed = isJobArmed(job);
    const details = running
      ? (tries > 1 ? `Attempt ${job.attempts}` : "")
      : job.state === "held" ? "Held until released"
      : [reasonText(job.reason), job.priority ? `priority ${job.priority}` : ""].filter(Boolean).join(" · ");
    return h("tr", { class: tag && byName(tag) ? "link" : null, tabindex: "0", dataset: { job: String(job.id) },
      onclick: (event) => { if (!event.target.closest?.("button")) openJobBoard(job.id); },
      onkeydown: (event) => { if (event.key === "Enter" && event.target === event.currentTarget) openJobBoard(job.id); } },
      h("td", {}, h("span", { class: `pill ${running ? "ok" : job.state === "held" ? "warn" : "soft"}`,
        text: running ? "Running" : job.state === "held" ? "On hold" : "Waiting" })),
      h("td", {}, h("b", { style: "font-weight:500", text: job.name }), " ", h("span", { class: "muted", text: `#${job.id}` })),
      h("td", {}, tag && byName(tag)
        ? h("button", { type: "button", class: "boardlink", text: tag, dataset: { act: `open:${job.id}` }, onclick: () => select(tag) })
        : h("span", { class: "muted", text: tag || job.project })),
      h("td", {}, h("span", { class: "muted", text: details })),
      h("td", { class: "num when" }, running ? sinceOrDash(job.since, true)
        : job.queued_at ? h("span", { class: "muted" }, since(job.queued_at), " waiting") : h("span", { class: "muted", text: "–" })),
      h("td", { class: "c-limit" }, fraction !== null
        ? h("span", { class: "limit" }, h("span", { class: "bar" }, h("i", {
          dataset: { limitFrom: job.since, limitSpan: job.time_limit }, style: `width:${(fraction * 100).toFixed(0)}%` })),
          h("span", { class: "muted", text: dur(job.time_limit) }))
        : h("span", { class: "muted", text: job.time_limit ? dur(job.time_limit) : "–" })),
      h("td", { class: "rowacts" }, snapshot.connected
        ? h("button", { type: "button", class: `btn sm${isArmed ? " confirm" : ""}`, dataset: { cancel: String(job.id) },
          onclick: () => cancelJob(job.id) },
        isArmed ? (running ? "Really stop?" : "Really cancel?") : running ? "Stop job" : "Cancel")
        : null));
  });
  const error = !snapshot.connected
    ? h("div", { class: "msg" }, icon("alert"), h("div", {}, h("b", { text: "mlq isn't responding." }),
      " This is the last known state, and jobs can't be cancelled until it's back.",
      h("span", { class: "src", text: (snapshot.error || "").replace(/\s+/g, " ") })))
    : null;
  if (!rows.length) return h("div", {}, error, h("p", { class: "muted", text: "No jobs running or waiting." }));
  return h("div", {}, error, h("div", { class: "tw" }, h("table", { class: "t" },
    h("thead", {}, h("tr", {}, ...["Status", "Job", "Board", "Details"].map((t) => h("th", { text: t })),
      h("th", { class: "num", text: "Time" }), h("th", { class: "c-limit", text: "Time limit" }), h("th", { class: "rowacts" }))),
    h("tbody", {}, ...rows))));
}

/** What the queue shows, without the timestamp the subscriber stamps on every poll. */
const queueKey = (snapshot) => (snapshot ? { ...snapshot, updated_at: undefined } : null);

function queueSummary(snapshot) {
  if (!snapshot) return "";
  if (!snapshot.connected) return "mlq offline · last known state";
  const limit = snapshot.effective_limit;
  return `${snapshot.active_leases}${limit ? ` of ${limit}` : ""} running · ${snapshot.queued.length} waiting`;
}

/* ------------------------------------------------------------- pane surface */

function renderPanes() {
  const board = active ? byName(active) : null;
  if (board && board.state === "running") mount(board.name);
  for (const [name, frame] of frames) frame.hidden = name !== active;

  if (board && board.state === "running") {
    els.view.hidden = true;
    viewKey = "";
    statusLog = null;
    return;
  }
  els.view.hidden = false;
  if (board) showStatusPage(board);
  else showOverviewPage();
}

/**
 * Replace a container's content and put focus and scroll back where they were,
 * so a state update never yanks the keyboard or the page out from under you.
 */
function rebuild(container, content) {
  const focused = document.activeElement;
  let focusSelector = null;
  if (focused && container.contains?.(focused)) {
    for (const attr of ["data-cancel", "data-act", "data-menu", "data-job", "data-board"]) {
      const host = focused.closest?.(`[${attr}]`);
      if (host) {
        focusSelector = `[${attr}="${CSS.escape(host.getAttribute(attr))}"]`;
        break;
      }
    }
  }
  const scroll = container.scrollTop;
  container.replaceChildren(content);
  container.scrollTop = scroll;
  if (focusSelector) container.querySelector?.(focusSelector)?.focus();
}

/* status pages */

function showStatusPage(board) {
  const log = statusLog;
  if (!log || log.name !== board.name || log.state !== board.state || log.restarts !== board.restarts) {
    // Keep showing the previous tail while the new one loads, instead of flashing "Loading…".
    const previous = log && log.name === board.name ? log.text : "";
    const entry = { name: board.name, state: board.state, restarts: board.restarts, text: previous, error: "", pending: true };
    statusLog = entry;
    fetchLogs(board.name, 60)
      .then((text) => {
        entry.text = text || "";
      })
      .catch((err) => {
        entry.error = String(err);
      })
      .finally(() => {
        entry.pending = false;
        if (statusLog === entry) {
          viewKey = "";
          schedule();
        }
      });
  }
  // Rebuilding on every frame would wipe a text selection in the log box.
  const key = ["status", board.name, board.state, board.message, board.autostart, board.next_attempt,
    board.restarts, Boolean(board.last_event), board.port, board.target, statusLog.pending, statusLog.text.length].join("|");
  if (viewKey === key) {
    patchLive(els.view);
    return;
  }
  viewKey = key;
  // Follow the end of the log unless the reader scrolled up into it.
  const old = els.view.querySelector?.(".logbox pre");
  const following = !old || old.scrollTop + old.clientHeight >= old.scrollHeight - 4;
  const offset = old ? old.scrollTop : 0;
  rebuild(els.view, statusPage(board));
  const pre = els.view.querySelector?.(".logbox pre");
  if (pre) pre.scrollTop = following ? pre.scrollHeight : offset;
}

function logLines(text) {
  const lines = String(text || "").replace(/^\n+|\n+$/g, "").split("\n");
  return lines.map((line) => {
    const kind = /^--- tensorwatch/.test(line) ? "t"
      : /^(E\d|Traceback|\s+File |\s+raise |\w+(Error|Exception)\b)/.test(line) ? "e"
        : /^W\d/.test(line) ? "w" : null;
    return h("span", { class: kind }, line || " ");
  });
}

function statusPage(board) {
  const name = board.name;
  const log = statusLog && statusLog.name === name ? statusLog : null;
  const B = (label, handler, opts) => button(label, handler, opts);
  const restart = () => act(name, "restart").then(() => say(`Restarting ${name}`)).catch(reportError);
  const facts = h("dl", { class: "facts" },
    h("div", {}, h("dt", { text: "Log directory" }), h("dd", { title: board.target, text: shortPath(board.target) })),
    h("div", {}, h("dt", { text: "Last new data" }), h("dd", {}, board.last_event ? [liveField("event", name, since(board.last_event)), " ago"] : "Never")),
    h("div", {}, h("dt", { text: "Mode" }), h("dd", { text: MODE_WORD[board.autostart] || board.autostart })),
    h("div", {}, h("dt", { text: "Port" }), h("dd", { text: String(board.port) })));
  const logbox = (label = "Recent log") => h("div", { class: "logbox" },
    h("header", {}, h("span", { text: label }), h("span", { class: "sub", title: board.log_path, text: board.log_path }),
      h("button", { type: "button", class: "btn sm", dataset: { act: "full-log" }, onclick: () => showLogs(name) }, "Open full log")),
    h("pre", { class: "log", tabindex: "0" }, !log || (log.pending && !log.text)
      ? "Loading…"
      : log.error || (log.text ? logLines(log.text) : "No output yet.")));
  const hintBoxes = hints(board, log ? log.text : "").map((hint) => h("div", { class: "msg plain" }, icon("bulb"),
    h("div", {}, h("b", { text: hint.title }), " ", ...hint.body, h("span", { class: "src", text: hint.source }))));
  const message = (tone, text, ...src) => h("div", { class: `msg ${tone}` }, icon("alert"),
    h("div", {}, text, h("span", { class: "src" }, ...src)));
  const retry = board.next_attempt ? ["next try in ", until(board.next_attempt)]
    : isRetry(board) ? ["trying again now"] : [];
  const page = ({ title, lead, msg = null, acts, extra = null }) => h("div", { class: "status" },
    h("div", { class: "eyebrow" }, h("span", { class: `pill ${stateTone(board)}`, text: stateWord(board) })),
    h("h2", { text: title }),
    lead ? h("p", { class: "lead" }, lead) : null,
    msg, ...hintBoxes,
    h("div", { class: "acts" }, ...acts),
    facts, extra);

  switch (isRetry(board) ? "backoff" : board.state) {
    case "backoff": {
      const pinned = board.autostart !== "on_demand";
      const crashes = `${board.restarts} crash${board.restarts === 1 ? "" : "es"} in a row`;
      return page({
        title: `${name} keeps crashing`,
        lead: pinned
          ? "TensorWatch restarts it on its own, waiting longer after each crash, up to a minute. Restart now skips the wait; Stop leaves it off until you start it."
          : "TensorWatch retries while the board is open, waiting longer after each crash, up to a minute. Restart now skips the wait; Stop leaves it off until you start it.",
        msg: (() => {
          const cause = crashCause(board);
          const tail = [crashes, ...(retry.length ? [" · ", ...retry] : [])];
          if (!cause.exited) return message("warn", cause.detail || cause.short, ...tail);
          // A killed process wrote no error of its own; its last stderr line would mislead.
          if (board.last_exit < 0 || !cause.detail) return message("warn", capital(exitText(board.last_exit)), ...tail);
          return message("warn", cause.detail, capital(exitText(board.last_exit)), " · ", ...tail);
        })(),
        acts: [B("Restart now", restart, { iconName: "restart", primary: true, key: "Enter" }),
          B("Stop", () => stopBoard(name), { iconName: "stop" }),
          B("Full log", () => showLogs(name), { iconName: "log" })],
        extra: logbox(),
      });
    }
    case "failed": {
      const early = preSpawn(board);
      const missing = (board.message || "").startsWith("logdir does not exist");
      return page({
        title: `${name} can't start`,
        lead: early
          ? "TensorWatch checks this before launching TensorBoard and tries again every 30 seconds, so fixing the problem below is enough. Try again skips the wait."
          : "TensorBoard didn't start listening in time. TensorWatch tries again on its own, waiting longer each time.",
        msg: message("", capital(board.message), "Supervisor message", ...(retry.length ? [" · ", ...retry] : [])),
        acts: [B("Try again", restart, { iconName: "restart", primary: true, key: "Enter" }),
          B("Full log", () => showLogs(name), { iconName: "log" }),
          missing ? B("Remove board", () => removeBoard(name), { iconName: "trash" }) : null],
        extra: logbox(early ? "Log from its last run" : "Recent log"),
      });
    }
    case "starting":
      return page({
        title: board.autostart === "on_demand" ? `Waking up ${name}…` : `Starting ${name}…`,
        lead: "TensorBoard is reading the log directory. This page switches to it as soon as it's ready; big directories can take a minute.",
        acts: [B("Cancel", () => stopBoard(name), { iconName: "stop" })],
        extra: logbox(),
      });
    case "disabled":
      return page({
        title: `${name} is turned off`,
        lead: ["It's still in the registry, but ", code("enabled = false"), " keeps TensorWatch from running it."],
        acts: [B(["Copy ", code(`tensorwatch enable ${name}`)], () => copy(`tensorwatch enable ${name}`), { iconName: "copy", primary: true, act: "copy-enable" }),
          B("Remove board", () => removeBoard(name), { iconName: "trash" })],
      });
    default:
      return page({
        title: isAsleep(board) ? `${name} is asleep` : `${name} is stopped`,
        lead: board.autostart === "on_demand"
          ? `It runs on demand: it starts while you have it open and sleeps ${dur(board.idle_timeout)} after you leave.`
          : board.autostart === "always"
            ? "It's set to always run, so it comes back when TensorWatch restarts."
            : "Manual boards run only when you start them.",
        acts: [B(isAsleep(board) ? "Wake up" : "Start", () => startBoard(name), { iconName: "play", primary: true, key: "Enter" }),
          B("Close", closeActive, { iconName: "close", key: "w" })],
        extra: board.message && !/^stopped/.test(board.message) ? logbox() : null,
      });
  }
}

/* overview */

/** The one line an attention card gives for a board: a likely cause, else what went wrong. */
function attentionWhy(board) {
  const hint = hints(board)[0];
  if (hint) return hint.title;
  if (!isCrashing(board)) return capital(board.message);
  const cause = crashCause(board);
  if (!cause.exited) return cause.detail || cause.short;
  return (board.last_exit >= 0 && cause.detail) || capital(exitText(board.last_exit));
}

/**
 * What the overview shows of a board, minus anything patched in place. A crash
 * loop flips between backoff and starting every few seconds with a new message
 * each time; none of that changes the page, so none of it is in the key.
 */
function overviewKey(board) {
  const attention = board.state === "failed" || isCrashing(board);
  return [board.name, shownState(board), stateWord(board), attention ? `${summary(board)}|${attentionWhy(board)}` : "",
    board.writing, board.autostart, Boolean(board.last_event), board.target, board.port,
    board.state === "running" ? board.since : null];
}

function showOverviewPage() {
  statusLog = null;
  const snapshot = queue();
  const key = JSON.stringify(["overview", state.boards.map(overviewKey), queueKey(snapshot), [...armed.keys()],
  state.server.config_path, state.server.keep_warm]);
  if (viewKey === key) {
    patchLive(els.view);
    return;
  }
  viewKey = key;
  rebuild(els.view, overview());
}

function overview() {
  const boards = state.boards;
  if (!boards.length) {
    return h("div", { class: "status" },
      h("h2", { text: "No boards yet" }),
      h("p", { class: "lead", text: "Register a TensorBoard log directory and TensorWatch keeps it running on its own port from then on." }),
      h("div", { class: "acts" }, button("Add a board", showAddBoard, { iconName: "plus", primary: true, key: "+" })));
  }
  const running = boards.filter((b) => b.state === "running");
  const receiving = boards.filter((b) => b.writing);
  const failed = boards.filter((b) => b.state === "failed");
  const crashing = boards.filter(isCrashing);
  const snapshot = queue();
  const activity = activityByBoard();
  const limit = snapshot && snapshot.effective_limit;
  const tile = (label, value, tone = "", small = "") =>
    h("div", { class: `tile ${tone}` }, h("dt", { text: label }),
      h("dd", {}, typeof value === "object" ? value : String(value), small ? h("small", { text: small }) : null));
  const attentionTone = failed.length ? "bad" : crashing.length ? "warn" : "";

  const cards = [...failed, ...crashing].map((board) => {
    const why = attentionWhy(board);
    return h("div", { class: `attn-card${isCrashing(board) ? " warn" : ""}` },
      h("span", { class: `dot ${shownState(board)}`, "aria-hidden": "true" }),
      h("div", { class: "ttl" }, board.name, h("span", { text: summary(board) })),
      h("div", { class: "acts" },
        h("button", { type: "button", class: "btn sm", dataset: { act: `details:${board.name}` }, onclick: () => select(board.name) }, "Details"),
        h("button", { type: "button", class: "btn sm", dataset: { act: `restart:${board.name}` }, onclick: () => act(board.name, "restart").then(() => say(`Restarting ${board.name}`)).catch(reportError) },
          board.state === "failed" ? "Try again" : "Restart now")),
      h("div", { class: "why", title: board.message, text: why }));
  });

  const rows = boards.map((board) => {
    const work = activity.get(board.name) || EMPTY_WORK;
    const up = board.state === "running";
    return h("tr", { class: "link", tabindex: "0", dataset: { board: board.name },
      onclick: (event) => { if (!event.target.closest?.("button")) select(board.name); },
      onkeydown: (event) => { if (event.key === "Enter" && event.target === event.currentTarget) select(board.name); },
      oncontextmenu: (event) => { event.preventDefault(); openMenu(board.name, event.clientX, event.clientY, event.currentTarget); } },
      h("td", { class: "nm" }, h("b", { text: board.name }), h("small", { title: board.target, text: board.target })),
      h("td", {}, h("span", { class: `pill ${stateTone(board)}`, text: stateWord(board) })),
      h("td", {}, board.writing
        ? h("span", { class: "act" }, icon("wave"), liveField("event", board.name, since(board.last_event)), " ago")
        : h("span", { class: "muted" }, board.last_event ? [liveField("event", board.name, since(board.last_event)), " ago"] : "Never")),
      h("td", {}, work.running.length || work.queued.length
        ? [work.running.length ? h("span", { class: "pill" }, icon("play"), `${work.running.length} running`) : null, " ",
          work.queued.length ? h("span", { class: "pill soft", text: `${work.queued.length} queued` }) : null]
        : h("span", { class: "muted", text: "–" })),
      h("td", {}, h("span", { class: "mode", title: MODE_HINT[board.autostart] },
        board.autostart === "on_demand" ? icon("moon") : null, MODE_WORD[board.autostart] || board.autostart)),
      h("td", { class: "num dim c-up" }, up ? since(board.since) : "–"),
      h("td", { class: "num" }, liveField("mem", board.name, h("span", { text: LIVE.mem(board) }))),
      h("td", { class: "num dim c-cpu" }, liveField("cpu", board.name, h("span", { text: LIVE.cpu(board) }))),
      h("td", { class: "num dim c-port", text: String(board.port) }),
      h("td", { class: "rowacts" }, (() => {
        const more = iconButton("more", `Actions for ${board.name}`, (event) => {
          event.stopPropagation();
          toggleMenu(board.name, more);
        });
        more.dataset.menu = board.name;
        more.setAttribute("aria-haspopup", "menu");
        return more;
      })()));
  });

  return h("div", { class: "ov" },
    h("div", { class: "ov-head" },
      h("div", {}, h("h2", { text: "Overview" }),
        h("div", { class: "sub", text: `${boards.length} board${boards.length === 1 ? "" : "s"} from ${state.server.config_path || "the registry"}` })),
      h("span", { class: "grow" }),
      button("Reload registry", reloadRegistry, { iconName: "refresh" }),
      button("Add board", showAddBoard, { iconName: "plus", primary: true })),
    h("dl", { class: "tiles" },
      tile("Running", running.length, "", `of ${boards.length}`),
      tile("Receiving data", receiving.length, receiving.length ? "ok" : ""),
      tile("Need attention", failed.length + crashing.length, attentionTone),
      tile("Memory", liveField("memtotal", "", h("span", { text: fmtBytes(running.reduce((sum, b) => sum + (b.rss_bytes || 0), 0)) }))),
      tile("Jobs running", snapshot && snapshot.connected ? snapshot.active_leases : "–", "",
        snapshot ? (snapshot.connected ? (limit ? `of ${limit} slots` : "") : "mlq offline") : "no mlq"),
      tile("Jobs waiting", snapshot ? snapshot.queued.length : "–")),
    cards.length ? h("div", { class: "attn" }, ...cards) : null,
    h("h3", { class: "sh" }, "Boards", h("span", { class: "note", text: "Same order as the sidebar · press 1–9 to open" })),
    h("div", { class: "tw" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {},
        h("th", { text: "Board" }), h("th", { text: "Status" }), h("th", { text: "Last new data" }), h("th", { text: "Jobs" }),
        h("th", { text: "Mode" }), h("th", { class: "num c-up", text: "Up for" }), h("th", { class: "num", text: "Memory" }),
        h("th", { class: "num c-cpu", text: "CPU" }), h("th", { class: "num c-port", text: "Port" }), h("th", { class: "rowacts" }))),
      h("tbody", {}, ...rows))),
    snapshot ? h("h3", { class: "sh" }, "Queue", h("span", { class: "note", text: queueSummary(snapshot) })) : null,
    snapshot ? queueTable() : null,
    h("p", { class: "legend" },
      kbd("Ctrl K"), " search everything   ", kbd("/"), " filter   ", kbd("1"), "–", kbd("9"), " open a board   ",
      kbd("g"), " overview   ", kbd("["), " sidebar   ", kbd("q"), " fold queue   ", kbd("⇧ Q"), " full queue   ",
      kbd("l"), " log   ", kbd("w"), " close   ", kbd("d"), " own window   ", kbd("Delete"), " remove the focused board",
      h("br"),
      `To save memory, only the last ${state.server.keep_warm || 2} boards you viewed stay loaded; closing one with `,
      kbd("w"), " unloads it right away."));
}

/* -------------------------------------------------------------------- menu */

let menuFor = null;
let menuReturn = null;

function toggleMenu(name, anchor) {
  if (menuFor === name) {
    closeMenu(true);
    return;
  }
  const box = anchor.getBoundingClientRect();
  openMenu(name, box.right - 4, box.bottom + 4, anchor);
}

function openMenu(name, x, y, returnTo) {
  const board = byName(name);
  if (!board) return;
  closeMenu();
  menuFor = name;
  menuReturn = returnTo || document.activeElement;
  const running = isOn(board);
  const item = (label, iconName, run, { key = null, danger = false } = {}) =>
    h("button", { type: "button", role: "menuitem", tabindex: "-1", class: danger ? "danger" : null,
      onclick: () => { closeMenu(true); run(); } },
    icon(iconName), h("span", { class: "label", text: label }), key ? kbd(key) : null);
  const mode = (value) => h("button", { type: "button", role: "menuitemradio", tabindex: "-1",
    "aria-checked": String(board.autostart === value), title: MODE_HINT[value],
    onclick: () => { closeMenu(true); setMode(name, value); } },
  h("span", { class: "check" }, board.autostart === value ? icon("check") : null), h("span", { class: "label", text: MODE_WORD[value] }));
  els.menu.replaceChildren(
    h("div", { class: "lbl", role: "presentation", text: name }),
    item("Open", "pane", () => select(name), { key: "Enter" }),
    item("Open in its own window", "external", () => detach(name), { key: "d" }),
    h("hr"),
    board.state !== "disabled" ? item(running ? "Stop" : "Start", running ? "stop" : "play", () => (running ? stopBoard(name) : startBoard(name))) : null,
    item("Restart", "restart", () => act(name, "restart").then(() => say(`Restarting ${name}`)).catch(reportError)),
    h("hr"),
    h("div", { class: "lbl", role: "presentation", text: "Mode" }),
    mode("always"), mode("on_demand"), mode("manual"),
    h("hr"),
    item("View log", "log", () => showLogs(name), { key: "l" }),
    item("Copy log directory path", "copy", () => copy(board.target, "the path")),
    h("hr"),
    item("Remove board", "trash", () => removeBoard(name), { key: "Delete", danger: true }),
  );
  els.menu.hidden = false;
  const box = els.menu.getBoundingClientRect();
  els.menu.style.left = `${Math.max(8, Math.min(x, innerWidth - box.width - 8))}px`;
  els.menu.style.top = `${Math.max(8, Math.min(y, innerHeight - box.height - 8))}px`;
  els.menu.querySelector("[role^=menuitem]")?.focus();
  boardRows.get(name)?.el.classList.add("menu-open");
}

function closeMenu(restore = false) {
  if (!menuFor) return;
  boardRows.get(menuFor)?.el.classList.remove("menu-open");
  menuFor = null;
  els.menu.hidden = true;
  if (restore) returnFocus(menuReturn);
}

els.menu.addEventListener("keydown", (event) => {
  const items = [...els.menu.querySelectorAll("[role^=menuitem]")];
  const at = items.indexOf(document.activeElement);
  const go = (i) => {
    event.preventDefault();
    items[(i + items.length) % items.length].focus();
  };
  if (event.key === "ArrowDown") go(at + 1);
  else if (event.key === "ArrowUp") go(at - 1);
  else if (event.key === "Home") go(0);
  else if (event.key === "End") go(items.length - 1);
  else if (event.key === "Escape" || event.key === "Tab") {
    event.preventDefault();
    event.stopPropagation();
    closeMenu(true);
  }
});

/* ----------------------------------------------------------------- palette */

const pal = { items: [], index: 0, returnTo: null };

function openPalette() {
  pal.returnTo = document.activeElement;
  els.palette.hidden = false;
  els.paletteInput.value = "";
  pal.index = 0;
  renderPalette();
  els.paletteInput.focus();
}

function closePalette() {
  if (els.palette.hidden) return;
  els.palette.hidden = true;
  returnFocus(pal.returnTo);
}

/** In-order letters: "pgm" finds parameter-golf-minicpm5. */
function subsequence(haystack, needle) {
  let i = 0;
  for (const c of haystack) if (c === needle[i]) i += 1;
  return i === needle.length;
}

function renderPalette() {
  const q = els.paletteInput.value.trim().toLowerCase();
  // Board names also match in-order letters; longer text (jobs, actions) only
  // matches substrings, or a two-letter query would match everything.
  const hit = (text, fuzzy = false) => !q || q.split(/\s+/).every((word) =>
    text.toLowerCase().includes(word) || (fuzzy && subsequence(text.toLowerCase(), word)));
  const boards = state.boards.filter((b) => hit(b.name, true) || hit(b.target)).map((b) => ({
    group: "Boards", label: b.name, aside: stateWord(b), dot: shownState(b), board: b.name, run: () => select(b.name) }));
  const jobs = q ? allJobs().filter((job) => hit(`${job.name} #${job.id} ${job.project}`)).map((job) => ({
    group: "Jobs", label: job.name, aside: `${job.state === "running" ? "running" : "waiting"} · ${job.board || job.project}`,
    iconName: "queue", run: () => openJobBoard(job.id) })) : [];
  const cur = active && byName(active);
  const up = cur && isOn(cur);
  const dark = currentTheme() === "dark";
  const actions = [
    cur && { label: `Restart ${cur.name}`, iconName: "restart", run: () => act(cur.name, "restart").catch(reportError) },
    cur && cur.state !== "disabled" && { label: `${up ? "Stop" : "Start"} ${cur.name}`, iconName: up ? "stop" : "play",
      run: () => (up ? stopBoard(cur.name) : startBoard(cur.name)) },
    cur && { label: `View ${cur.name} log`, iconName: "log", aside: "l", run: () => showLogs(cur.name) },
    ...(cur ? Object.keys(MODE_WORD).filter((m) => m !== cur.autostart).map((m) => ({
      label: `Run ${cur.name} ${({ always: "always", on_demand: "on demand", manual: "only when started" })[m]}`,
      iconName: "moon", run: () => setMode(cur.name, m) })) : []),
    cur && { label: `Close ${cur.name}`, iconName: "close", aside: "w", run: closeActive },
    cur && { label: `Remove ${cur.name}`, iconName: "trash", run: () => removeBoard(cur.name) },
    ...state.boards.filter((b) => (b.state === "failed" || isCrashing(b)) && b !== cur).map((b) => ({
      label: `Restart ${b.name}`, iconName: "restart", aside: stateWord(b), run: () => act(b.name, "restart").catch(reportError) })),
    { label: "Go to overview", iconName: "home", aside: "g", run: showOverview },
    queue() && { label: queueExpanded ? "Show compact queue" : "Show full queue", iconName: "queue", aside: "⇧ Q", run: () => toggleQueueExpanded() },
    { label: "Add a board…", iconName: "plus", aside: "+", run: showAddBoard },
    { label: "Reload registry", iconName: "refresh", aside: "r", run: reloadRegistry },
    { label: "Show or hide the sidebar", iconName: "side", aside: "[", run: toggleRail },
    { label: dark ? "Use the light theme" : "Use the dark theme", iconName: dark ? "sun" : "moon", run: toggleTheme },
  ].filter(Boolean).filter((a) => hit(a.label)).map((a) => ({ group: "Actions", ...a }));
  pal.items = [...boards, ...jobs, ...actions];
  pal.index = Math.min(pal.index, Math.max(0, pal.items.length - 1));
  let group = "";
  const nodes = [];
  pal.items.forEach((item, i) => {
    if (item.group !== group) {
      group = item.group;
      nodes.push(h("div", { class: "pal-group", role: "presentation", text: group }));
    }
    nodes.push(h("div", { class: "pal-item", role: "option", id: `pal-${i}`, "aria-selected": String(i === pal.index),
      onmousedown: (event) => { event.preventDefault(); runPalette(i, event.shiftKey); } },
    item.dot ? h("span", { class: `dot ${item.dot}` }) : icon(item.iconName),
    h("span", { class: "label", text: item.label }), h("span", { class: "aside", text: item.aside || "" })));
  });
  els.paletteList.replaceChildren(...(nodes.length ? nodes : [h("div", { class: "pal-group", text: "No matches" })]));
  if (pal.items.length) els.paletteInput.setAttribute("aria-activedescendant", `pal-${pal.index}`);
  else els.paletteInput.removeAttribute("aria-activedescendant");
  $(`pal-${pal.index}`)?.scrollIntoView({ block: "nearest" });
}

function runPalette(i, detached = false) {
  const item = pal.items[i];
  closePalette();
  if (!item) return;
  if (detached && item.board) detach(item.board);
  else item.run();
}

els.paletteInput.addEventListener("input", () => {
  pal.index = 0;
  renderPalette();
});
els.paletteInput.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    pal.index = Math.max(0, Math.min(pal.items.length - 1, pal.index + step));
    renderPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    runPalette(pal.index, event.shiftKey);
  } else if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closePalette();
  } else if (event.key === "Tab") {
    event.preventDefault();
  }
});
els.palette.addEventListener("mousedown", (event) => {
  if (event.target === els.palette) closePalette();
});

/* ------------------------------------------------------------ theme & rail */

function currentTheme() {
  const set = document.documentElement?.dataset.theme;
  if (set) return set;
  return typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
  renderThemeToggle();
}

function renderThemeToggle() {
  const dark = currentTheme() === "dark";
  setIcon(els.themeToggle, dark ? "sun" : "moon");
  setLabel(els.themeToggle, dark ? "Use the light theme" : "Use the dark theme");
}

function setRail(collapsed) {
  document.documentElement.classList.toggle("rail-collapsed", collapsed);
  localStorage.setItem(RAIL_KEY, collapsed ? "collapsed" : "open");
  renderRailLabel();
}
const railCollapsed = () => Boolean(document.documentElement?.classList.contains("rail-collapsed"));
const toggleRail = () => setRail(!railCollapsed());
const renderRailLabel = () =>
  setLabel(els.collapseRail, railCollapsed() ? "Expand the sidebar ([)" : "Collapse the sidebar ([)");

function reloadRegistry() {
  post("/api/reload")
    .then(() => say("Registry reloaded"))
    .catch(reportError);
}

/* -------------------------------------------------------------------- logs */

async function fetchLogs(name, lines) {
  const response = await fetch(`${boardPath(name)}/logs?lines=${lines}`);
  if (!response.ok) throw new Error(`log fetch failed: ${response.status}`);
  return response.text();
}

let logsFor = null;
let logsSeq = 0;
let logsText = "";
let logsReturn = null;

async function showLogs(name) {
  const board = byName(name);
  if (!logsFor) logsReturn = document.activeElement;
  logsFor = name;
  logsText = "";
  els.logsCopy.disabled = true;
  els.logsTitle.textContent = `${name} log`;
  els.logsPath.textContent = board ? board.log_path : "";
  els.logsBody.textContent = "Loading…";
  els.logs.inert = false;
  els.logs.classList.add("open");
  els.logsBody.focus();
  const seq = ++logsSeq;
  let text;
  try {
    text = (await fetchLogs(name, 500)) || "";
  } catch (err) {
    if (seq === logsSeq) els.logsBody.textContent = String(err);
    return;
  }
  // A slower answer for an earlier board (or an earlier Refresh) must not land last.
  if (seq !== logsSeq || logsFor !== name) return;
  logsText = text;
  els.logsCopy.disabled = !text;
  els.logsBody.replaceChildren(...(text ? logLines(text) : [document.createTextNode("No output yet.")]));
  els.logsBody.scrollTop = els.logsBody.scrollHeight;
}

function closeLogs() {
  if (!logsFor) return;
  logsFor = null;
  els.logs.classList.remove("open");
  els.logs.inert = true;
  returnFocus(logsReturn);
}

/* ------------------------------------------------------- add / remove board */

/**
 * Unregister a board. No confirmation: the registry block stays with the server
 * and "Undo" puts it back where it was, port and comments included.
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
              if (wasActive && !active) openWhenKnown(result.board);
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

function setAddMode(mode) {
  els.addAutostart.value = mode;
  els.addModes.replaceChildren(...Object.entries(MODE_WORD).map(([value, word]) => h("button", {
    type: "button", title: MODE_HINT[value], "aria-pressed": String(value === mode), onclick: () => setAddMode(value),
  }, word)));
}

function showAddBoard() {
  closePalette();
  closeMenu();
  els.addForm.reset();
  setAddMode("always");
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
  renderPaths([]);
  const prefix = els.addPath.value;
  // Numbered when typed, not when asked: an answer to the previous keystroke
  // must not show up during this one's debounce.
  const seq = ++paths.seq;
  if (!prefix.trim()) return;
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
    const cut = item.path.lastIndexOf("/") + 1;
    const li = h("li", { id: `add-board-path-${index}`, role: "option" },
      // The box clips from the left (rtl); the marks keep "~/" and the trailing "/" in place.
      h("span", { class: "parent", text: ltr(item.path.slice(0, cut)) }),
      h("span", { class: "leaf", text: item.path.slice(cut) }),
      item.events ? h("span", { class: "tag", text: "has runs" }) : null);
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
  els.suggestionsMeta.textContent = "Looking…";
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
  const files = `${item.events >= 1000 ? "1000+" : item.events} event file${item.events === 1 ? "" : "s"}`;
  const pick = h("button", { type: "button", class: "pick", title: "Fill the form with this directory",
    onclick: () => {
      els.addPath.value = item.path;
      els.addName.value = item.name;
      renderPaths([]);
      els.addPath.focus();
    } },
  h("span", { class: "name", text: item.name }),
  h("span", { class: "meta" }, h("span", { class: "where", text: ltr(item.path) }), h("span", { text: files })));
  const li = h("li", {}, pick);
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
      els.suggestionsMeta.textContent = `Added ${result.board}`;
    } catch (err) {
      add.disabled = false;
      showAddError(err && err.message ? err.message : String(err));
    }
  }, "btn sm");
  li.append(add);
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
    openWhenKnown(result.board);
    say(result.events ? `Added ${result.board}` : `Added ${result.board}. There are no event files there yet.`);
  } catch (err) {
    showAddError(err && err.message ? err.message : String(err));
  } finally {
    submitting = false;
  }
}

/* ------------------------------------------------------------------ stream */

/**
 * Open a board the manager has just been asked to register. The stream update
 * that announces it can arrive before or after the request resolves.
 */
function openWhenKnown(name) {
  if (byName(name)) select(name);
  else pendingSelect = name;
}

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
  if (menuFor && !byName(menuFor)) closeMenu();
  for (const name of [...frames.keys()]) {
    const board = byName(name);
    // A dead board's iframe would show a browser error page and hold its heap.
    if (!board) unmount(name);
    else if (board.state !== "running") unmount(name, { keepActive: true });
  }
  scheduleHeartbeat();
  schedule();
}

let source = null;

function connect() {
  source = new EventSource("/api/events");
  source.onmessage = (event) => absorb(JSON.parse(event.data));
  source.onerror = () => {
    streamDown = true;
    schedule();
  };
}

// The stream is what tells TensorWatch a window is open, so a page the browser
// keeps after navigating away (back/forward cache) must not hold it.
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => source?.close());
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) connect();
  });
}

/** Renew watched boards well before their idle lease expires. */
function scheduleHeartbeat(force = false) {
  const names = [...new Set([...lru, ...(active ? [active] : [])])]
    .filter((name) => byName(name) && !isStoppedByUser(byName(name)));
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
    for (const name of names) act(name, "renew").catch(() => {});
    heartbeatTimer = setTimeout(renew, period);
  };
  // Renew immediately when restoring a selection, resuming the window, or
  // changing the watched set/policy, including newly selected short leases.
  renew();
}

/* ------------------------------------------------------------------ events */

els.filter.addEventListener("input", schedule);
els.filter.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    if (els.filter.value) {
      els.filter.value = "";
      schedule();
    } else {
      els.filter.blur();
    }
  } else if (event.key === "Enter") {
    const board = visibleBoards()[0];
    if (board) {
      select(board.name);
      els.filter.blur();
    }
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    [...els.boards.children].find((row) => !row.classList.contains("hidden"))?.focus();
  }
});
els.navOverview.addEventListener("click", showOverview);
els.openPalette.addEventListener("click", openPalette);
els.collapseRail.addEventListener("click", toggleRail);
els.themeToggle.addEventListener("click", toggleTheme);
els.logsClose.addEventListener("click", closeLogs);
els.logsRefresh.addEventListener("click", () => logsFor && showLogs(logsFor));
els.logsCopy.addEventListener("click", () => {
  if (logsText) copy(logsText, "the log");
});
// Focus goes back to a board row, never to the filter: a text field would
// swallow the next shortcut.
function restoreFocus() {
  const row = boardRows.get(active) || boardRows.values().next().value;
  if (row) row.el.focus();
  else document.activeElement?.blur();
}

/** Return focus to where a layer was opened from, if that element survived the renders since. */
function returnFocus(el) {
  if (el && el.isConnected && el !== document.body && !el.closest?.("[inert]")) el.focus();
  else restoreFocus();
}
els.addBoard.addEventListener("click", showAddBoard);
els.addPath.addEventListener("input", completePaths);
els.addPath.addEventListener("keydown", onPathKey);
els.addPath.addEventListener("blur", () => {
  paths.seq += 1;  // an answer still in flight must not reopen the list
  clearTimeout(paths.timer);
  renderPaths([]);
});
els.addClose.addEventListener("click", () => els.addDialog.close());
els.addCancel.addEventListener("click", () => els.addDialog.close());
els.addForm.addEventListener("submit", (event) => {
  submitAddBoard(event);
});
els.addDialog.addEventListener("close", restoreFocus);
els.queueMore.addEventListener("click", () => toggleQueueExpanded());
els.queueHead.addEventListener("click", () => {
  queueExpanded = false;
  localStorage.setItem(QUEUE_KEY, localStorage.getItem(QUEUE_KEY) === "1" ? "0" : "1");
  schedule();
});
document.addEventListener("visibilitychange", () => {
  scheduleHeartbeat();
  if (!document.hidden) {
    // A frame requested while hidden may never have run; ask for a fresh one.
    framePending = false;
    schedule();
  }
});
if (typeof window !== "undefined") window.addEventListener("blur", () => closeMenu());
document.addEventListener("mousedown", (event) => {
  if (menuFor && !els.menu.contains(event.target) && !event.target.closest?.("[aria-haspopup=menu]")) closeMenu();
});

document.addEventListener("keydown", (event) => {
  if (els.addDialog.open) return;  // the modal owns the keyboard
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (els.palette.hidden) openPalette();
    else closePalette();
    return;
  }
  if (!els.palette.hidden) {
    // The input handles its own keys; this catches focus that strayed onto the panel.
    if (event.key === "Escape") closePalette();
    return;
  }
  if (event.key === "Escape") {
    // Closes only the topmost layer.
    if (menuFor) closeMenu(true);
    else if (logsFor) closeLogs();
    else if (queueExpanded && els.queue.contains(document.activeElement)) toggleQueueExpanded(false);
    return;
  }
  if (event.ctrlKey && !event.metaKey && !event.altKey && event.key >= "0" && event.key <= "9") {
    event.preventDefault();
    const board = visibleBoards()[event.key === "0" ? 9 : Number(event.key) - 1];
    if (board) select(board.name);
    return;
  }
  if (event.target instanceof HTMLInputElement || menuFor) return;
  if (event.altKey || event.metaKey || event.ctrlKey) return;
  // Digits follow what is on screen, so filtering renumbers them.
  if (event.key >= "1" && event.key <= "9") {
    const board = visibleBoards()[Number(event.key) - 1];
    if (board) select(board.name);
    return;
  }
  const global = {
    "/": () => {
      if (railCollapsed()) setRail(false);
      els.filter.focus();
      els.filter.select();
    },
    "+": showAddBoard,
    "=": showAddBoard,
    g: showOverview,
    "[": toggleRail,
    q: () => els.queueHead.click(),
    Q: () => queue() && toggleQueueExpanded(),
    r: reloadRegistry,
  }[event.key];
  if (global) {
    event.preventDefault();
    global();
    return;
  }
  if (!active) return;
  const board = byName(active);
  if (event.key === "w") closeActive();
  else if (event.key === "d") detach(active);
  else if (event.key === "l") showLogs(active);
  else if (event.key === "R") reloadPane(active);
  else if (event.key === "Enter" && board && event.target === document.body) {
    // The status page's primary action.
    if (board.state === "failed" || isCrashing(board)) act(active, "restart").catch(reportError);
    else if (board.state === "stopped") startBoard(active);
  }
});

fetch("/api/state")
  .then((response) => response.json())
  .then(absorb)
  .catch(reportError)
  .finally(connect);

// Durations tick locally; the server does not push frames for elapsed time.
// Rows re-render every 5 s (minute-level ages); visible clocks tick each second.
setInterval(() => {
  if (!document.hidden) schedule();
}, 5000);
setInterval(() => {
  if (!document.hidden) tick();
}, 1000);
