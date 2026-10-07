"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/tensorwatch/web/app.js"), "utf8");

/** Just enough DOM for the dashboard to build rows, dialogs and toasts. */
function fakeElement(tag = "div") {
  const listeners = new Map();
  const el = {
    tag,
    children: [],
    parent: null,
    dataset: {},
    attributes: {},
    hidden: false,
    textContent: "",
    value: "",
    className: "",
    title: "",
    classList: {
      add: (name) => { el.className += ` ${name}`; },
      toggle() {},
      contains: () => false,
    },
    addEventListener: (event, callback) => listeners.set(event, callback),
    fire: (event, detail = {}) => listeners.get(event)?.({
      preventDefault() {}, stopPropagation() {}, target: el, ...detail,
    }),
    setAttribute: (name, value) => { el.attributes[name] = value; },
    removeAttribute: (name) => { delete el.attributes[name]; },
    append(...nodes) {
      for (const node of nodes) {
        if (typeof node === "string") continue;
        node.parent = el;
        el.children.push(node);
      }
    },
    appendChild: (node) => el.append(node),
    insertBefore: (node) => el.append(node),
    replaceChildren(...nodes) {
      el.children.length = 0;
      el.append(...nodes);
    },
    remove() {
      if (!el.parent) return;
      el.parent.children.splice(el.parent.children.indexOf(el), 1);
      el.parent = null;
    },
    get firstElementChild() { return el.children[0]; },
    focus() {},
    reset() {},
    showModal() { el.open = true; },
    close() { el.open = false; },
    scrollIntoView() {},
    /** Text of this node and everything under it, for assertions. */
    get text() { return el.textContent + el.children.map((child) => child.text).join(""); },
    find: (test) => (test(el) ? el : el.children.map((child) => child.find(test)).find(Boolean)),
  };
  return el;
}

function dashboard(selected = "short", routes = () => undefined) {
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const requests = [];
  const listeners = new Map();
  const elements = new Map();
  const document = {
    hidden: false,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, fakeElement());
      return elements.get(id);
    },
    createElement: fakeElement,
    createElementNS: (_namespace, tag) => fakeElement(tag),
    createTextNode: (text) => Object.assign(fakeElement("#text"), { textContent: text }),
    addEventListener: (event, callback) => listeners.set(event, callback),
  };
  const context = vm.createContext({
    document,
    localStorage: { getItem: () => selected, setItem() {}, removeItem() {} },
    requestAnimationFrame() {},
    setInterval() {},
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { at: now + delay, callback });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    fetch(url, options) {
      const method = options?.method || "GET";
      const routed = routes(method, url, options?.body && JSON.parse(options.body));
      if (routed === undefined && !options) return new Promise(() => {}); // Initial state is supplied below.
      requests.push({ url, at: now, method, body: options?.body && JSON.parse(options.body) });
      // A route may answer later, to exercise out-of-order responses.
      return Promise.resolve(routed).then((body = {}) => ({
        ok: !body.error,
        statusText: "error",
        json: () => Promise.resolve(body),
      }));
    },
  });
  vm.runInContext(source, context);
  return {
    requests,
    el: (id) => document.getElementById(id),
    update(boards) {
      context.payload = { boards, server: { keep_warm: 2 } };
      vm.runInContext("absorb(payload)", context);
    },
    run: (code) => vm.runInContext(code, context),
    visible(visible) {
      document.hidden = !visible;
      listeners.get("visibilitychange")();
    },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [id, timer] = due;
        timers.delete(id);
        now = timer.at;
        timer.callback();
      }
      now = end;
    },
  };
}

const board = (name, idle_timeout, autostart = "on_demand") =>
  ({ name, idle_timeout, autostart, state: "running" });
const times = (app, name) => app.requests
  .filter(({ url }) => url === `/api/boards/${name}/demand`).map(({ at }) => at);

test("60-second idle leases renew before expiry despite frequent state updates", () => {
  const app = dashboard();
  const boards = [board("short", 60)];
  app.update(boards);
  for (let i = 0; i < 120; i++) {
    app.advance(2000);
    app.update(boards);
  }
  assert.deepEqual(times(app, "short"), Array.from({ length: 13 }, (_, i) => i * 20000));
});

test("selecting a short lease immediately shortens the renewal period", () => {
  const app = dashboard("long");
  app.update([board("long", 900), board("short", 3)]);
  app.advance(100);
  app.run('select("short")');
  app.advance(2000);
  assert.deepEqual(times(app, "short"), [100, 1100, 2100]);
});

test("explicitly reselecting the current board renews demand immediately", () => {
  const app = dashboard();
  app.update([board("short", 60)]);
  app.advance(1000);
  app.update([{ ...board("short", 60), state: "stopped" }]);
  app.run('select("short")');
  assert.deepEqual(times(app, "short"), [0, 1000]);
  app.advance(20000);
  assert.deepEqual(times(app, "short"), [0, 1000, 21000]);
});

test("the shortest mounted lease controls renewal even when inactive", () => {
  const app = dashboard("long");
  app.update([board("long", 900), board("short", 6)]);
  app.run('lru.push("short"); scheduleHeartbeat()');
  app.advance(6000);
  assert.deepEqual(times(app, "short"), [0, 2000, 4000, 6000]);
});

test("policy updates reschedule leases without waiting for the old period", () => {
  const app = dashboard();
  app.update([board("short", 900)]);
  app.advance(1000);
  app.update([board("short", 9)]);
  app.advance(3000);
  assert.deepEqual(times(app, "short"), [0, 1000, 4000]);
});

test("hidden dashboards release leases and renew immediately on return", () => {
  const app = dashboard();
  app.update([board("short", 60)]);
  app.advance(1000);
  app.visible(false);
  app.advance(90000);
  assert.deepEqual(times(app, "short"), [0]);
  app.visible(true);
  app.advance(20000);
  assert.deepEqual(times(app, "short"), [0, 91000, 111000]);
});

test("removed boards stop receiving demand and permanent boards use the upper bound", () => {
  const app = dashboard();
  app.update([board("short", 1, "always")]);
  app.advance(60000);
  assert.deepEqual(times(app, "short"), [0, 60000]);
  app.update([]);
  app.advance(120000);
  assert.deepEqual(times(app, "short"), [0, 60000]);
});

/** Let pending promise callbacks inside the dashboard run. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a row says what is wrong in the words it has room for", () => {
  const app = dashboard();
  const status = (fields) => app.run(`shortStatus(${JSON.stringify({ autostart: "always", ...fields })})`);
  assert.equal(status({ state: "failed", message: "logdir does not exist: /very/long/path/runs" }), "logdir missing");
  assert.equal(status({ state: "failed", message: "port 6100 is already in use" }), "port 6100 is already in use");
  assert.equal(status({ state: "stopped", autostart: "on_demand", message: "" }), "on demand");
  assert.equal(status({ state: "stopped", autostart: "manual", message: "stopped by request" }), "stopped");
});

test("removing a board offers an undo that hands back the server's token", async () => {
  const undo = "token-123";
  const app = dashboard("short", (method, url) => {
    if (method === "DELETE") return { ok: true, board: "short", undo };
    if (url === "/api/boards") return { ok: true, board: "short" };
    return undefined;
  });
  app.update([board("short", 60)]);
  app.run('removeBoard("short")');
  await settle();
  assert.deepEqual(app.requests.at(-1), { url: "/api/boards/short", at: 0, method: "DELETE", body: undefined });

  const toasts = app.el("toasts");
  assert.equal(toasts.children.length, 1);
  assert.match(toasts.text, /Removed short/);
  toasts.find((node) => node.textContent === "Undo").fire("click");
  await settle();
  assert.deepEqual(app.requests.at(-1).body, { undo });
  assert.match(toasts.text, /Restored short/);
  assert.doesNotMatch(toasts.text, /Removed short/);
});

test("a failed request is reported as a toast and leaves the totals line alone", async () => {
  const app = dashboard("short", (method) => (method === "DELETE" ? { error: "no board 'short'" } : undefined));
  app.update([board("short", 60)]);
  app.el("totals").textContent = "1/1 up";
  app.run('removeBoard("short")');
  await settle();
  assert.match(app.el("toasts").text, /no board 'short'/);
  assert.equal(app.el("totals").textContent, "1/1 up");
});

test("toasts expire, and only a few are kept", () => {
  const app = dashboard();
  for (let i = 0; i < 7; i++) app.run(`say("message ${i}")`);
  assert.equal(app.el("toasts").children.length, 4);
  assert.match(app.el("toasts").text, /message 6/);
  app.advance(4000);
  assert.equal(app.el("toasts").children.length, 0);
});

test("a pending undo is never pushed out by later messages", () => {
  const app = dashboard();
  for (let i = 0; i < 6; i++) {
    app.run(`toast("Removed board${i}", { ttl: 12000, action: { label: "Undo", run() {} } })`);
  }
  for (let i = 0; i < 6; i++) app.run(`say("message ${i}")`);
  const text = app.el("toasts").text;
  for (let i = 0; i < 6; i++) assert.match(text, new RegExp(`Removed board${i}`));
  assert.doesNotMatch(text, /message/);
});

test("a board added from the form opens once the manager reports it", async () => {
  const app = dashboard("", (method, url) =>
    (url === "/api/boards" ? { ok: true, board: "fresh", events: true } : undefined));
  app.update([]);
  app.el("add-board-path").value = " ~/work/fresh/runs ";
  app.el("add-board-name").value = "";
  app.el("add-board-autostart").value = "manual";
  app.run("submitAddBoard({ preventDefault() {} })");
  await settle();
  assert.deepEqual(app.requests.at(-1).body, { path: "~/work/fresh/runs", name: "", autostart: "manual" });
  assert.equal(app.run("active"), null);

  app.update([{ name: "fresh", state: "stopped", autostart: "manual", idle_timeout: 900 }]);
  assert.equal(app.run("active"), "fresh");
  assert.equal(app.requests.at(-2).url, "/api/boards/fresh/start");
});

test("a rejected add stays in the dialog with the server's reason", async () => {
  const app = dashboard("", (method, url) =>
    (url === "/api/boards" ? { error: "/x is already registered as 'x'" } : undefined));
  app.update([]);
  app.el("add-board-dialog").open = true;
  app.el("add-board-path").value = "/x";
  app.run("submitAddBoard({ preventDefault() {} })");
  await settle();
  assert.equal(app.el("add-board-error").textContent, "/x is already registered as 'x'");
  assert.equal(app.el("add-board-error").hidden, false);
  assert.equal(app.el("add-board-dialog").open, true);
});

test("path completion shows the answer to what is typed now, not a slower earlier one", async () => {
  const pending = [];
  const app = dashboard("", (method, url) => {
    if (!url.startsWith("/api/paths")) return undefined;
    return new Promise((resolve) => pending.push({ url, resolve }));
  });
  const input = app.el("add-board-path");
  const type = (value) => {
    input.value = value;
    app.run("completePaths()");
    app.advance(70);
  };
  type("~/a");
  type("~/ab");
  assert.deepEqual(pending.map(({ url }) => url), ["/api/paths?prefix=~%2Fa", "/api/paths?prefix=~%2Fab"]);
  pending[1].resolve({ paths: [{ path: "~/abc", events: true }] });
  await settle();
  pending[0].resolve({ paths: [{ path: "~/a1", events: false }, { path: "~/a2", events: false }] });
  await settle();
  assert.equal(app.el("add-board-paths").children.length, 1);
  assert.match(app.el("add-board-paths").text, /abc.*has runs/);

  // Accepting a completion descends into it and lists what is inside.
  app.run("acceptPath(0)");
  assert.equal(input.value, "~/abc/");
  app.advance(70);
  assert.equal(pending.at(-1).url, "/api/paths?prefix=~%2Fabc%2F");
  // Clearing the field closes the list without asking the server.
  const asked = pending.length;
  type("");
  assert.equal(app.el("add-board-paths").hidden, true);
  assert.equal(pending.length, asked);
});

test("undoing the removal of the open board opens it again", async () => {
  const app = dashboard("short", (method, url) => {
    if (method === "DELETE") return { ok: true, board: "short", undo: "t" };
    if (url === "/api/boards") return { ok: true, board: "short" };
    return undefined;
  });
  app.update([board("short", 60), board("other", 60)]);
  app.run('removeBoard("short")');
  await settle();
  app.update([board("other", 60)]);
  assert.equal(app.run("active"), null);

  app.el("toasts").find((node) => node.textContent === "Undo").fire("click");
  await settle();
  app.update([board("short", 60), board("other", 60)]);
  assert.equal(app.run("active"), "short");
});

test("an answer to the previous keystroke is dropped even if it lands mid-debounce", async () => {
  const pending = [];
  const app = dashboard("", (method, url) => {
    if (!url.startsWith("/api/paths")) return undefined;
    return new Promise((resolve) => pending.push({ url, resolve }));
  });
  const input = app.el("add-board-path");
  input.value = "~/a";
  app.run("completePaths()");
  app.advance(70);
  // The next key is typed, but its lookup has not been sent yet.
  input.value = "~/ab";
  app.run("completePaths()");
  pending[0].resolve({ paths: [{ path: "~/a1", events: false }] });
  await settle();
  assert.equal(app.el("add-board-paths").children.length, 0);
});

test("holding Enter on the add form registers the logdir once", async () => {
  const app = dashboard("", (method, url) =>
    (url === "/api/boards" ? { ok: true, board: "fresh", events: true } : undefined));
  app.update([]);
  app.el("add-board-path").value = "/x";
  for (let i = 0; i < 3; i++) app.run("submitAddBoard({ preventDefault() {} })");
  await settle();
  assert.equal(app.requests.filter(({ url }) => url === "/api/boards").length, 1);
});

test("every row control is drawn with an icon that exists", () => {
  const app = dashboard();
  app.update([board("short", 60), { ...board("idle", 60), state: "stopped" }]);
  app.run(`state.queue = { connected: true, running: [{ id: 1, name: "job", state: "running",
    since: 0, boards: ["short"], project: "p", cwd: "/" }], queued: [], active_leases: 1 };
    render()`);
  const drawn = [];
  const walk = (node) => {
    if (node.tag === "path") drawn.push(node.attributes.d);
    node.children.forEach(walk);
  };
  for (const id of ["boards", "queue-jobs", "reload-config", "add-board"]) walk(app.el(id));
  // 2 rows x (6 controls + the on-demand mark) + 1 queue cancel + 2 header buttons.
  assert.equal(drawn.length, 17);
  assert.ok(drawn.every((d) => typeof d === "string" && d.startsWith("M")), drawn.join(" | "));
});
