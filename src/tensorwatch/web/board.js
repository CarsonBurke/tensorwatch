"use strict";

/**
 * One board in its own window.
 *
 * The page lives on the dashboard origin rather than the board's port so that
 * TensorWatch knows a window is open: it exits once none is. Instead of holding
 * an event stream, as the dashboard does, the page polls. Browsers allow six
 * HTTP/1.1 connections per host across all windows, so one held stream per
 * pop-out would stall every request after the fifth. Each poll counts as
 * presence, and the page renews the board's on-demand lease like an open pane.
 */

const $ = (id) => document.getElementById(id);
const els = {
  banner: $("solo-banner"),
  status: $("solo-status"),
  title: $("solo-title"),
  message: $("solo-message"),
  start: $("solo-start"),
};
const name = decodeURIComponent(location.pathname.replace(/\/+$/, "").split("/").pop() || "");
document.title = `${name} · TensorWatch`;

/** Poll this often while the board is coming up, to show it the moment it serves. */
const FAST_POLL_MS = 1000;
/** Upper bound once it serves; also bounded by the manager's idle grace. */
const SLOW_POLL_MS = 10000;

let board = null;
let frame = null;
let woken = false;
let reachable = true;
let poller = null;
let inflight = false;
let stopped = false;
let heartbeat = null;
let idleExit = null;

const act = (action) =>
  fetch(`/api/boards/${encodeURIComponent(name)}/${action}`, { method: "POST" }).catch(() => {});

const stoppedByUser = (b) => b.state === "stopped" && b.message === "stopped on request";

/**
 * Bring the board up the way its policy means it. An always board that is merely
 * waiting for its staggered start is left alone: `start` would pin it on.
 */
function wake() {
  if (!board || board.state === "running" || board.state === "starting") return;
  if (board.autostart === "on_demand") return act("demand");
  if (board.autostart === "manual" || stoppedByUser(board) || board.state === "failed") {
    return act("start");
  }
}

function show(title, message, { canStart = false } = {}) {
  if (frame) {
    // A dead board's frame would show a browser error page and hold its heap.
    frame.remove();
    frame = null;
  }
  els.status.hidden = false;
  els.title.textContent = title;
  els.message.textContent = message;
  els.start.hidden = !canStart;
}

function render() {
  if (!board) {
    show(`No board named ${name}`, "It may have been removed from the registry.");
    return;
  }
  if (board.state === "running") {
    if (!frame) {
      frame = document.createElement("iframe");
      frame.className = "pane";
      frame.src = board.url;
      frame.title = `TensorBoard: ${name}`;
      frame.setAttribute("referrerpolicy", "no-referrer");
      document.body.append(frame);
    }
    els.status.hidden = true;
    return;
  }
  const waking = board.autostart === "on_demand" ? "Waking up" : "Starting";
  switch (board.state) {
    case "starting":
      return show(`${waking} ${name}…`, "TensorBoard is reading the logdir.");
    case "backoff":
      return show(`${name} is crashing`, board.message || "");
    case "failed":
      return show(`${name} can't start`, board.message || "", { canStart: true });
    case "disabled":
      return show(`${name} is disabled`, "Enable it in the dashboard or with `tensorwatch enable`.");
    default:
      if (board.autostart === "always" && !stoppedByUser(board)) {
        return show(`${waking} ${name}…`, "Waiting for its turn to start.");
      }
      return show(`${name} is stopped`, board.message || "", { canStart: true });
  }
}

/** Renew the lease while the window is visible; a hidden window lets it lapse. */
function scheduleHeartbeat() {
  clearTimeout(heartbeat);
  heartbeat = null;
  if (document.hidden || !board || board.autostart !== "on_demand") return;
  const period = Math.max(250, Math.min(60000, (board.idle_timeout * 1000) / 3 || 60000));
  const renew = () => {
    act("renew");
    heartbeat = setTimeout(renew, period);
  };
  renew();
}

function absorb(payload) {
  const previous = board;
  board = payload.boards.find((b) => b.name === name) || null;
  idleExit = payload.server?.idle_exit ?? null;
  if (board && !woken) {
    // Opening the window is the request to see the board, and so is coming back
    // after TensorWatch restarted underneath it.
    woken = true;
    wake();
  }
  if (!previous || !board || previous.autostart !== board.autostart || previous.idle_timeout !== board.idle_timeout) {
    scheduleHeartbeat();
  }
  render();
}

function pollDelay() {
  if (!reachable || !board || board.state !== "running") return FAST_POLL_MS;
  // Stay well inside the grace TensorWatch gives a window before it exits.
  const grace = idleExit ? (idleExit * 1000) / 4 : SLOW_POLL_MS;
  return Math.max(FAST_POLL_MS, Math.min(SLOW_POLL_MS, grace));
}

async function poll() {
  poller = null;
  inflight = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(response.statusText);
    const payload = await response.json();
    if (!reachable) woken = false;  // a restarted manager forgot the request
    reachable = true;
    els.banner.hidden = true;
    absorb(payload);
  } catch {
    reachable = false;
    els.banner.textContent = "Reconnecting to TensorWatch…";
    els.banner.hidden = false;
  }
  inflight = false;
  if (!stopped) poller = setTimeout(poll, pollDelay());
}

function resume() {
  stopped = false;
  if (poller === null && !inflight) poll();
}

resume();
els.start.addEventListener("click", () => {
  if (board) act(board.autostart === "on_demand" ? "demand" : "start");
});
document.addEventListener("visibilitychange", scheduleHeartbeat);
// A page the browser keeps for back/forward navigation must not count as open.
window.addEventListener("pagehide", () => {
  stopped = true;
  clearTimeout(poller);
  poller = null;
  clearTimeout(heartbeat);
  heartbeat = null;
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    resume();
    scheduleHeartbeat();
  }
});
