"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/tensorwatch/web/board.js"), "utf8");

const flush = () => new Promise((resolve) => setImmediate(resolve));

/** Load board.js for `/boards/<name>` against a fake DOM, timers and fetch. */
async function load(name = "cleanrl", boards = [], { hidden = false } = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) {
      const listeners = new Map();
      elements.set(id, {
        id, hidden: false, textContent: "", attributes: {},
        addEventListener: (event, callback) => listeners.set(event, callback),
        click: () => listeners.get("click")?.(),
        setAttribute(key, value) { this.attributes[key] = value; },
        remove() { this.removed = true; },
      });
    }
    return elements.get(id);
  };
  const page = {
    boards,
    server: { idle_exit: 120 },
    down: false,
    posts: [],
    polls: 0,
    timers: new Map(),
    appended: [],
    winListeners: new Map(),
    element,
  };
  let nextTimer = 1;
  const document = {
    title: "",
    hidden,
    getElementById: element,
    createElement: () => element(`frame-${page.appended.length}`),
    addEventListener() {},
    body: { append: (node) => page.appended.push(node) },
  };
  page.document = document;
  const context = {
    document,
    location: { pathname: `/boards/${encodeURIComponent(name)}` },
    window: { addEventListener: (event, callback) => page.winListeners.set(event, callback) },
    fetch: (url, options = {}) => {
      if (url === "/api/state") {
        page.polls += 1;
        if (page.down) return Promise.reject(new Error("connection refused"));
        const body = JSON.parse(JSON.stringify({ boards: page.boards, server: page.server }));
        return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
      }
      page.posts.push(url.split("/").pop());
      assert.equal(options.method, "POST");
      return Promise.resolve({ ok: true });
    },
    setTimeout: (callback, ms) => {
      const id = nextTimer++;
      page.timers.set(id, { callback, ms });
      return id;
    },
    clearTimeout: (id) => page.timers.delete(id),
    decodeURIComponent,
    encodeURIComponent,
    Math,
  };
  /** The pending poll timer, if any. */
  page.pollTimer = () => [...page.timers.entries()].find(([, t]) => t.callback.name === "poll");
  /** Run the pending poll and let it settle. */
  page.tick = async () => {
    const [id, timer] = page.pollTimer();
    page.timers.delete(id);
    timer.callback();
    await flush();
  };
  page.fire = (event, detail = {}) => page.winListeners.get(event)?.(detail);
  vm.runInNewContext(source, context);
  await flush();
  return page;
}

const board = (fields) => ({
  name: "cleanrl", url: "http://127.0.0.1:6100/", autostart: "on_demand",
  idle_timeout: 900, state: "stopped", message: "", ...fields,
});

test("opening a sleeping on-demand board wakes it and keeps its lease", async () => {
  const page = await load("cleanrl", [board({})]);
  assert.deepEqual(page.posts, ["demand", "renew"]);
  assert.equal(page.document.title, "cleanrl · TensorWatch");
  assert.equal(page.element("solo-status").hidden, false);
  assert.equal(page.pollTimer()[1].ms, 1000);  // watch closely while it comes up

  page.boards = [board({ state: "running" })];
  await page.tick();
  assert.equal(page.appended.length, 1);
  assert.equal(page.appended[0].src, "http://127.0.0.1:6100/");
  assert.equal(page.element("solo-status").hidden, true);
  // Serving: poll only as often as presence needs, well inside the idle grace.
  assert.equal(page.pollTimer()[1].ms, 10000);
  page.server = { idle_exit: 8 };
  await page.tick();
  assert.equal(page.pollTimer()[1].ms, 2000);
  // Later polls neither wake it again nor restart the heartbeat.
  assert.deepEqual(page.posts, ["demand", "renew"]);
});

test("each policy is woken the way it means", async () => {
  const manual = await load("cleanrl", [board({ autostart: "manual" })]);
  assert.deepEqual(manual.posts, ["start"]);

  // Waiting for its staggered start: `start` would pin it on, so leave it.
  const pending = await load("cleanrl", [board({ autostart: "always" })]);
  assert.deepEqual(pending.posts, []);
  assert.match(pending.element("solo-title").textContent, /Starting cleanrl/);

  const stopped = await load("cleanrl", [board({ autostart: "always", message: "stopped on request" })]);
  assert.deepEqual(stopped.posts, ["start"]);

  const running = await load("cleanrl", [board({ autostart: "always", state: "running" })]);
  assert.deepEqual(running.posts, []);
  assert.equal(running.appended.length, 1);
});

test("a board that stops loses its frame and offers Start", async () => {
  const page = await load("cleanrl", [board({ autostart: "manual", state: "running" })]);
  page.boards = [board({ autostart: "manual", message: "stopped on request" })];
  await page.tick();
  assert.equal(page.appended[0].removed, true);
  assert.equal(page.element("solo-start").hidden, false);
  page.element("solo-start").click();
  assert.deepEqual(page.posts, ["start"]);
});

test("after TensorWatch restarts, the board is asked for again", async () => {
  const page = await load("cleanrl", [board({ autostart: "manual", state: "running" })]);
  assert.deepEqual(page.posts, []);
  page.down = true;
  await page.tick();
  assert.equal(page.element("solo-banner").hidden, false);
  // The new manager starts with the manual board stopped.
  page.down = false;
  page.boards = [board({ autostart: "manual" })];
  await page.tick();
  assert.equal(page.element("solo-banner").hidden, true);
  assert.deepEqual(page.posts, ["start"]);
});

test("an unknown board says so instead of waking anything", async () => {
  const page = await load("gone", [board({})]);
  assert.deepEqual(page.posts, []);
  assert.match(page.element("solo-title").textContent, /No board named gone/);
});

test("a hidden window lets the lease lapse", async () => {
  const page = await load("cleanrl", [board({})], { hidden: true });
  assert.deepEqual(page.posts, ["demand"]);
});

test("leaving the page stops polling, and coming back resumes it", async () => {
  const page = await load("cleanrl", [board({ state: "running" })]);
  page.fire("pagehide", { persisted: true });
  // A page kept for back/forward navigation must not count as an open window.
  assert.equal(page.pollTimer(), undefined);
  const polls = page.polls;
  page.fire("pageshow", { persisted: false });
  assert.equal(page.polls, polls);  // a fresh load polls on its own
  page.fire("pageshow", { persisted: true });
  await flush();
  assert.equal(page.polls, polls + 1);
  assert.ok(page.pollTimer());
});
