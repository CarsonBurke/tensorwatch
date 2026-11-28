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
        node.remove?.();  // like the DOM, a node lives in one place
        node.parent = el;
        el.children.push(node);
      }
    },
    appendChild: (node) => el.append(node),
    insertBefore(node, before) {
      node.remove?.();
      const at = before ? el.children.indexOf(before) : -1;
      node.parent = el;
      if (at < 0) el.children.push(node);
      else el.children.splice(at, 0, node);
    },
    replaceChildren(...nodes) {
      el.children.length = 0;
      el.append(...nodes);
    },
    remove() {
      if (!el.parent) return;
      const at = el.parent.children.indexOf(el);
      if (at >= 0) el.parent.children.splice(at, 1);
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
    location: { protocol: "http:", port: "6005" },
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
        text: () => Promise.resolve(typeof body === "string" ? body : ""),
      }));
    },
  });
  vm.runInContext(source, context);
  return {
    requests,
    el: (id) => document.getElementById(id),
    update(boards, extra = {}) {
      context.payload = { boards, server: { keep_warm: 2 }, ...extra };
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
  .filter(({ url }) => url === `/api/boards/${name}/renew`).map(({ at }) => at);

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
  const summary = (fields) => app.run(`summary(${JSON.stringify({ autostart: "always", port: 6100, ...fields })})`);
  assert.equal(summary({ state: "failed", message: "logdir does not exist: /very/long/path/runs" }), "Log directory is missing");
  assert.equal(summary({ state: "failed", message: "port 6100 is already in use" }), "Port 6100 is taken");
  assert.equal(summary({ state: "failed", message: "cannot start tensorboard: No such file" }), "Can't launch its command");
  assert.equal(summary({ state: "backoff", last_exit: 1, message: "exited with code 1; retrying in 4s - boom" }),
    "Crashed (exit 1) · retrying");
  assert.equal(summary({ state: "backoff", last_exit: -9, message: "exited with code -9; retrying in 60s" }),
    "Crashed (killed) · retrying");
  assert.equal(summary({ state: "stopped", autostart: "on_demand", message: "stopped: idle" }), "Asleep · wakes when opened");
  assert.equal(summary({ state: "stopped", autostart: "manual", message: "stopped on request" }), "Stopped by you");
  assert.equal(summary({ state: "starting", autostart: "on_demand" }), "Waking up…");
  assert.equal(summary({ state: "disabled" }), "Disabled in the registry");
});

test("state words separate a user stop from sleep, and crashing from can't start", () => {
  const app = dashboard();
  const word = (fields) => app.run(`stateWord(${JSON.stringify({ autostart: "always", ...fields })})`);
  assert.equal(word({ state: "stopped", autostart: "on_demand", message: "stopped: idle" }), "Asleep");
  assert.equal(word({ state: "stopped", autostart: "on_demand", message: "stopped on request" }), "Stopped by you");
  assert.equal(word({ state: "stopped", message: "" }), "Stopped");
  assert.equal(word({ state: "backoff" }), "Crashing");
  assert.equal(word({ state: "failed" }), "Can't start");
  assert.equal(word({ state: "starting", autostart: "on_demand" }), "Waking up");
  assert.equal(word({ state: "starting" }), "Starting");
  assert.equal(app.run(`stateTone({ state: "failed" })`), "bad");
  assert.equal(app.run(`stateTone({ state: "backoff" })`), "warn");
});

test("negative exit codes read as signals, and the frozen retry delay is dropped", () => {
  const app = dashboard();
  assert.equal(app.run("exitText(-9)"), "killed by SIGKILL");
  assert.equal(app.run("exitText(-15)"), "killed by SIGTERM");
  assert.equal(app.run("exitText(-31)"), "killed by signal 31");
  assert.equal(app.run("exitText(2)"), "exit code 2");
  const detail = (message) => app.run(`crashDetail(${JSON.stringify({ message })})`);
  assert.equal(detail("exited with code 1; retrying in 16s - OSError: [Errno 24] Too many open files"),
    "OSError: [Errno 24] Too many open files");
  assert.equal(detail("exited with code -9; retrying in 60s"), "");
  assert.equal(detail("something else"), "something else");
});

test("hints name what they matched and stay quiet otherwise", () => {
  const app = dashboard();
  const titles = (fields, log = "") =>
    app.run(`hints(${JSON.stringify({ port: 6100, ...fields })}, ${JSON.stringify(log)}).map((h) => h.title)`);
  assert.deepEqual([...titles({ state: "backoff", last_exit: 1 }, "OSError: [Errno 24] Too many open files")],
    ["It ran out of open files."]);
  assert.deepEqual([...titles({ state: "backoff", last_exit: -9 })], ["It was killed by the system."]);
  assert.deepEqual([...titles({ state: "failed", message: "logdir does not exist: /x" })], ["The log directory is gone."]);
  assert.deepEqual([...titles({ state: "failed", message: "port 6100 is already in use" })],
    ["Another program is using port 6100."]);
  assert.deepEqual([...titles({ state: "backoff", last_exit: 1 }, "ValueError: bad")], []);
});

test("paths shorten by whole folders", () => {
  const app = dashboard();
  assert.equal(app.run('shortPath("~/Documents/repositories/cleanrl/runs")'), "~/…/cleanrl/runs");
  assert.equal(app.run('shortPath("/data/a/b/c/d")'), "/…/c/d");
  assert.equal(app.run('shortPath("~/work/runs")'), "~/work/runs");
});

test("an on-demand board nobody is watching says when it will sleep", () => {
  const app = dashboard("other");
  const now = Date.now() / 1000;
  app.run(`state = { now: ${now}, boards: [], server: { keep_warm: 2 } }`);
  const sleeps = (fields) => app.run(`sleepsAt(${JSON.stringify({ name: "x", state: "running", autostart: "on_demand",
    idle_timeout: 900, demanded_ago: 300, ...fields })})`);
  assert.ok(Math.abs(sleeps({}) - (now + 600)) < 1);
  assert.equal(sleeps({ autostart: "always" }), null);
  assert.equal(sleeps({ name: "other" }), null);  // open, so the heartbeat renews it
  assert.equal(sleeps({ demanded_ago: null }), null);
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

test("an added board opens even when the stream announced it before the request returned", async () => {
  let app;
  app = dashboard("", (method, url) => {
    if (url !== "/api/boards") return undefined;
    app.update([{ name: "fresh", state: "stopped", autostart: "on_demand", idle_timeout: 900 }]);
    return { ok: true, board: "fresh", events: true };
  });
  app.update([]);
  app.el("add-board-path").value = "/x";
  app.run("submitAddBoard({ preventDefault() {} })");
  await settle();
  assert.equal(app.run("active"), "fresh");
});

test("opening a crashing board does not reset its backoff with a start", () => {
  const app = dashboard("");
  app.update([{ ...board("bad", 60, "always"), state: "backoff", restarts: 4 },
    { ...board("off", 60, "manual"), state: "stopped" }]);
  app.run('select("bad")');
  app.run('select("off")');
  const starts = app.requests.filter(({ url }) => url.endsWith("/start")).map(({ url }) => url);
  assert.deepEqual(starts, ["/api/boards/off/start"]);
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

test("a new path prefix immediately invalidates the displayed suggestions", () => {
  const app = dashboard();
  const input = app.el("add-board-path");
  app.run('renderPaths([{ path: "~/old" }])');
  input.value = "~/new";
  app.run('completePaths(); onPathKey({ key: "Tab", preventDefault() {} })');
  assert.equal(input.value, "~/new");
  assert.equal(app.el("add-board-paths").hidden, true);
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

const queueOf = (running, queued) => ({ connected: true, running, queued, active_leases: running.length, effective_limit: 2 });
const job = (id, fields) => ({ id, name: `job${id}`, state: "queued", boards: [], project: "p", cwd: "/", since: 0,
  queued_at: 0, ...fields });

test("every icon drawn in the sidebar and overview exists", () => {
  const app = dashboard("short");
  app.update([board("short", 60), { ...board("idle", 60), state: "stopped" },
    { ...board("bad", 60, "always"), state: "backoff", last_exit: 1, restarts: 2, message: "exited with code 1; retrying in 4s" }],
  { queue: queueOf([job(1, { state: "running", boards: ["short"] })], [job(2, { state: "held" })]) });
  app.run("render(); active = null; render()");
  const drawn = [];
  const walk = (node) => {
    if (node.tag === "path") drawn.push(node.attributes.d);
    node.children.forEach(walk);
  };
  for (const id of ["boards", "queue-jobs", "add-board", "view", "theme-toggle", "collapse-rail"]) walk(app.el(id));
  assert.ok(drawn.length > 20, `only ${drawn.length} icons`);
  assert.ok(drawn.every((d) => typeof d === "string" && d.startsWith("M")), drawn.join(" | "));
});

test("the sidebar queue lists running jobs, then the open board's, then others up to the limit", () => {
  const app = dashboard("mine");
  app.update([board("mine", 60, "always"), board("other", 60, "always")], {
    server: { keep_warm: 2, queue_visible: 4 },
    queue: queueOf(
      [job(1, { state: "running", boards: ["other"] })],
      [job(2, { boards: ["other"] }), job(3, { boards: ["other"] }), job(4, { boards: ["mine"] }), job(5, {})],
    ),
  });
  app.run("render()");
  const nodes = app.el("queue-jobs").children
    .map((node) => (node.find((child) => child.className === "jname") || node).textContent);
  assert.deepEqual(nodes, ["Running", "job1", "mine's jobs", "job4", "Other jobs", "job2", "job3"]);
  assert.match(app.el("queue-more").text, /1 more/);

  const firstRow = app.el("queue-jobs").children[1];
  const stop = firstRow.children[1];
  assert.equal(stop.hidden, true);
  app.el("queue-more").fire("click");
  app.run("render()");
  assert.equal(app.el("queue-more").attributes["aria-expanded"], "true");
  assert.equal(firstRow, app.el("queue-jobs").children[1]);
  assert.equal(stop.hidden, false);
  assert.equal(stop.textContent, "Stop");
  assert.ok(app.el("queue-jobs").find((node) => node.textContent === "job5"));

  app.el("queue-more").fire("click");
  app.run("render()");
  assert.equal(app.el("queue-more").attributes["aria-expanded"], "false");
  assert.equal(stop.hidden, true);
  assert.ok(!app.el("queue-jobs").find((node) => node.textContent === "job5"));
});

test("a queued job that starts needs a fresh confirmation to stop", () => {
  const app = dashboard("");
  app.update([], { queue: queueOf([], [job(7, {})]) });
  app.run("toggleQueueExpanded(); render()");
  const action = app.el("queue-jobs").children[1].children[1];
  action.fire("click");
  app.run("render()");
  assert.equal(action.textContent, "Cancel?");

  app.update([], { queue: queueOf([job(7, { state: "running" })], []) });
  app.run("render()");
  assert.equal(action.textContent, "Stop");
  action.fire("click");
  app.run("render()");
  assert.equal(action.textContent, "Stop?");
  assert.ok(!app.requests.some(({ url }) => url.startsWith("/api/queue/")));
});

test("stopping a job takes a second click, and the first one wears off", async () => {
  const app = dashboard("", (method, url) => (url.startsWith("/api/queue/") ? { ok: true } : undefined));
  app.update([], { queue: queueOf([job(7, { state: "running" })], []) });
  app.run('cancelJob("7")');
  assert.equal(app.requests.filter(({ url }) => url.startsWith("/api/queue/")).length, 0);
  app.advance(3000);
  app.run('cancelJob("7")');
  assert.equal(app.requests.filter(({ url }) => url.startsWith("/api/queue/")).length, 0);
  app.run('cancelJob("7")');
  await settle();
  assert.deepEqual(app.requests.filter(({ url }) => url.startsWith("/api/queue/")).map(({ url }) => url),
    ["/api/queue/7/cancel"]);
  assert.match(app.el("toasts").text, /Stopping #7 job7/);
});

test("the open board's mode switch posts the new policy and ignores the current one", () => {
  const app = dashboard("short");
  app.update([board("short", 60)]);
  app.run('setMode("short", "on_demand"); setMode("short", "manual")');
  const posts = app.requests.filter(({ url }) => url.endsWith("/autostart"));
  assert.deepEqual(posts.map(({ body }) => body), [{ mode: "manual" }]);
});

test("a crashing board's page counts down to the next try and hides the frozen delay", () => {
  const app = dashboard("bad");
  const next = Date.now() / 1000 + 42;
  app.update([{ ...board("bad", 60, "always"), state: "backoff", last_exit: -9, restarts: 3, next_attempt: next,
    message: "exited with code -9; retrying in 8s", target: "/runs", port: 6100, log_path: "/l" }]);
  app.run("render()");
  const view = app.el("view");
  const countdown = view.find((node) => node.dataset.until !== undefined);
  assert.equal(Number(countdown.dataset.until), next);
  assert.match(countdown.textContent, /^4[12]s$/);
  assert.match(view.text, /Killed by SIGKILL3 crashes in a row · next try in/);
  assert.match(view.text, /killed by the system/);
  assert.doesNotMatch(view.text, /retrying in 8s/);
});

test("an on-demand crash says it retries only while open", () => {
  const app = dashboard("bad");
  app.update([{ ...board("bad", 60), state: "backoff", last_exit: 1, restarts: 1, message: "exited with code 1; retrying in 2s - boom" }]);
  app.run("render()");
  assert.match(app.el("view").text, /retries while the board is open/);
  assert.match(app.el("view").text, /boom/);
});

test("the overview is not rebuilt for memory, CPU or queue poll stamps", () => {
  const app = dashboard("");
  const running = { ...board("a", 60, "always"), rss_bytes: 1e8, cpu_percent: 1, since: 1, last_event: 5 };
  const snapshot = queueOf([job(1, { state: "running", since: 1 })], []);
  app.update([running], { queue: { ...snapshot, updated_at: 1 } });
  app.run("render()");
  const page = app.el("view").children[0];
  assert.ok(page);
  app.update([{ ...running, rss_bytes: 2e8, cpu_percent: 7, last_event: 9 }], { queue: { ...snapshot, updated_at: 2 } });
  app.run("render()");
  assert.equal(app.el("view").children[0], page);
  // A change in what the page shows still rebuilds it.
  app.update([{ ...running, state: "backoff", restarts: 1, last_exit: 1, message: "exited with code 1; retrying in 2s" }]);
  app.run("render()");
  assert.notEqual(app.el("view").children[0], page);
});

test("the footer leaves the reconnecting notice once the stream is back", () => {
  const app = dashboard("");
  const boards = [{ ...board("a", 60, "always"), rss_bytes: 1e8 }];
  app.update(boards);
  app.run("render()");
  app.run("streamDown = true; render()");
  assert.match(app.el("totals").text, /Reconnecting/);
  app.update(boards);
  app.run("render()");
  assert.doesNotMatch(app.el("totals").text, /Reconnecting/);
  assert.match(app.el("totals").text, /1 running/);
});

test("a slow log answer for an earlier board never replaces the current one", async () => {
  const pending = [];
  const app = dashboard("", (method, url) =>
    (url.includes("/logs") ? new Promise((resolve) => pending.push({ url, resolve })) : undefined));
  app.update([board("a", 60, "always"), board("b", 60, "always")]);
  app.run('showLogs("a"); showLogs("b")');
  pending.find(({ url }) => url.includes("/b/")).resolve("log of b");
  await settle();
  pending.find(({ url }) => url.includes("/a/")).resolve("log of a");
  await settle();
  assert.equal(app.run("logsText"), "log of b");
  assert.match(app.el("logs-body").text, /log of b/);
});

test("copy is unavailable while another board's log loads or fails", async () => {
  let fail;
  const app = dashboard("", (method, url) => {
    if (url.includes("/a/logs")) return "log of a";
    if (url.includes("/b/logs")) return new Promise((resolve) => { fail = resolve; });
  });
  await app.run('showLogs("a")');
  assert.equal(app.el("logs-copy").disabled, false);
  const loading = app.run('showLogs("b")');
  assert.equal(app.el("logs-copy").disabled, true);
  assert.equal(app.run("logsText"), "");
  fail({ error: "missing log" });
  await loading;
  assert.equal(app.el("logs-copy").disabled, true);
});

test("a board the user stopped is not woken again by the open pane's heartbeat", () => {
  const app = dashboard("a");
  app.update([board("a", 60)]);
  app.advance(1000);
  app.update([{ ...board("a", 60), state: "stopped", message: "stopped on request" }]);
  app.advance(60000);
  assert.deepEqual(times(app, "a"), [0]);
});

test("starting an on-demand board wakes it instead of pinning it on", () => {
  const app = dashboard("");
  app.update([{ ...board("lazy", 60), state: "stopped", message: "stopped: idle" },
    { ...board("pinned", 60, "manual"), state: "stopped" }]);
  app.run('startBoard("lazy"); startBoard("pinned")');
  const asked = app.requests.filter(({ method }) => method === "POST").map(({ url }) => url);
  assert.deepEqual(asked, ["/api/boards/lazy/demand", "/api/boards/pinned/start"]);
});

test("a start timeout reads as a timeout, not as the SIGTERM that ended it", () => {
  const app = dashboard("slow");
  const timedOut = { ...board("slow", 60, "always"), state: "backoff", restarts: 1, last_exit: -15,
    message: "start timed out after 60s", next_attempt: Date.now() / 1000 + 2 };
  app.update([timedOut]);
  assert.equal(app.run(`summary(${JSON.stringify(timedOut)})`), "Timed out starting · retrying");
  app.run("render()");
  assert.match(app.el("view").text, /Start timed out after 60s/);
  assert.doesNotMatch(app.el("view").text, /SIGTERM/);
  // The cause survives the "starting" half of the loop, whose message says nothing about it.
  const retrying = { ...timedOut, state: "starting", message: "starting: scanning logdir", next_attempt: null };
  assert.equal(app.run(`summary(${JSON.stringify(retrying)})`), "Timed out starting · retrying");
});

test("jobs without timestamps show a dash, not a nonsense age", () => {
  const app = dashboard("");
  app.update([], { queue: queueOf([job(1, { state: "running", since: null })], [job(2, { queued_at: null })]) });
  const text = app.run("queueTable(queues[0])").text;
  assert.doesNotMatch(text, /NaN|days/);
});

/* ------------------------------------------------------------------ remotes */

const remote = (fields = {}) => ({
  name: "gpu", label: "GPU box", ssh: "gpu", enabled: true, state: "connected", error: null,
  next_attempt: null, hostname: "gpu-host", version: "0.2.0", latency_ms: 4, boards: [], queue: null, ...fields,
});
const remoteBoard = (name, fields = {}) => ({ ...board(name, 60, "always"), host: name, target: `/runs/${name}`, ...fields });
const frameSources = (app) => app.el("panes").children.filter((node) => node.className === "pane").map((node) => node.src);

test("boards are grouped by machine and each is framed at its own origin", () => {
  const app = dashboard("cleanrl@gpu");
  app.update([{ ...remoteBoard("local"), host: "local" }],
    { remotes: [remote({ boards: [remoteBoard("cleanrl", { host: "clean-rl" })] })], server: { keep_warm: 2, hostname: "here" } });
  app.run("render()");
  const list = app.el("boards").children;
  assert.deepEqual(list.map((node) => node.dataset.machine ?? node.dataset.name),
    ["", "local", "gpu", "cleanrl@gpu"]);
  assert.match(list[0].text, /This machinehere/);
  assert.match(list[2].text, /GPU boxgpu-host/);
  // The row shows the board's own name; the group says where it lives.
  assert.match(list[3].text, /^cleanrl/);
  assert.deepEqual(frameSources(app), ["http://clean-rl.gpu.localhost:6005/"]);
});

test("a remote board's controls go to its machine", async () => {
  const app = dashboard("cleanrl@gpu", (method) => (method === "POST" ? { ok: true } : undefined));
  app.update([], { remotes: [remote({ boards: [{ ...remoteBoard("cleanrl"), autostart: "on_demand" }] })] });
  app.run('setMode("cleanrl@gpu", "manual"); act("cleanrl@gpu", "restart")');
  await settle();
  const urls = app.requests.map(({ url }) => url);
  assert.ok(urls.includes("/api/remotes/gpu/api/boards/cleanrl/renew"), urls.join(" "));
  assert.ok(urls.includes("/api/remotes/gpu/api/boards/cleanrl/autostart"));
  assert.ok(urls.includes("/api/remotes/gpu/api/boards/cleanrl/restart"));
  assert.ok(!urls.some((url) => url.startsWith("/api/boards/")));
});

test("a machine that can't be reached says why, offers a retry and frames nothing", async () => {
  const app = dashboard("cleanrl@gpu", (method) => (method === "POST" ? { ok: true } : undefined));
  const up = remote({ boards: [remoteBoard("cleanrl")] });
  app.update([], { remotes: [up] });
  app.run("render()");
  assert.equal(frameSources(app).length, 1);

  const down = { ...up, state: "error", error: { kind: "auth", message: "Permission denied (publickey)." }, next_attempt: Date.now() / 1000 + 60 };
  const before = app.requests.length;
  app.update([], { remotes: [down] });
  app.run("render()");
  // The last known boards stay listed, as offline; their panes are dropped.
  assert.equal(frameSources(app).length, 0);
  const [head, row] = app.el("boards").children;
  assert.match(head.text, /SSH login refused/);
  assert.match(row.className, /state-offline/);
  assert.match(row.text, /Offline/);
  const view = app.el("view").text;
  assert.match(view, /Can't log in to GPU box/);
  assert.match(view, /Permission denied \(publickey\)/);
  assert.match(view, /key or an agent/);
  // Nothing is renewed through a link that is down.
  app.advance(120000);
  assert.ok(!app.requests.slice(before).some(({ url }) => url.startsWith("/api/remotes/gpu/api/")));

  head.find((node) => node.tag === "button").fire("click");
  await settle();
  assert.equal(app.requests.at(-1).url, "/api/remotes/gpu/retry");
  assert.equal(app.requests.at(-1).method, "POST");
});

test("a remote board stays selected while its machine connects", () => {
  const app = dashboard("cleanrl@gpu");
  app.update([], { remotes: [remote({ state: "connecting", boards: [] })] });
  app.run("render()");
  assert.equal(app.run("active"), "cleanrl@gpu");
  assert.match(app.el("view").text, /Connecting to GPU box/);
  app.update([], { remotes: [remote({ boards: [remoteBoard("cleanrl")] })] });
  app.run("render()");
  assert.deepEqual(frameSources(app), ["http://cleanrl.gpu.localhost:6005/"]);
  // Once connected, a board it no longer has is let go.
  app.update([], { remotes: [remote({ boards: [] })] });
  assert.equal(app.run("active"), null);
});

test("a window with no boards of its own shows another machine's", () => {
  const app = dashboard("");
  app.update([], { remotes: [remote({ boards: [remoteBoard("cleanrl")] })] });
  app.run("render()");
  assert.deepEqual(app.el("boards").children.map((node) => node.dataset.machine ?? node.dataset.name), ["gpu", "cleanrl@gpu"]);
  assert.equal(app.el("boards-empty").hidden, true);
  assert.match(app.el("view").text, /1 board on 2 machines/);
});

test("each machine's queue is listed under it, and cancelling goes to the right one", async () => {
  const app = dashboard("", (method, url) => (url.includes("/queue/") ? { ok: true } : undefined));
  app.update([remoteBoard("local")], {
    queue: queueOf([job(7, { state: "running", boards: ["local"] })], []),
    remotes: [remote({ boards: [remoteBoard("cleanrl")], queue: queueOf([job(7, { state: "running", boards: ["cleanrl"] })], [job(8, {})]) })],
  });
  app.run("toggleQueueExpanded(); render()");
  const nodes = app.el("queue-jobs").children;
  assert.deepEqual(nodes.map((node) => (node.className === "rq-machine"
    ? `[${node.children[0].textContent}]`
    : node.find((c) => c.className === "jname")?.textContent ?? node.textContent)),
  ["[This machine]", "Running", "job7", "[GPU box]", "Running", "job7", "Waiting", "job8"]);
  // Same job id on both machines: distinct rows, and the remote's goes to the remote.
  assert.equal(app.run("jobRows.size"), 3);
  app.run('cancelJob("7@gpu"); cancelJob("7@gpu")');
  await settle();
  assert.deepEqual(app.requests.filter(({ url }) => url.includes("/queue/")).map(({ url }) => url),
    ["/api/remotes/gpu/api/queue/7/cancel"]);
  // The remote job's board link opens the remote board.
  assert.deepEqual(app.run('jobByKey("7@gpu").boards'), ["cleanrl@gpu"]);
});

test("a remote queue reads as offline while its link is down", () => {
  const app = dashboard("");
  app.update([], {
    remotes: [remote({ state: "error", error: { kind: "unreachable", message: "timed out" }, queue: queueOf([job(1, { state: "running" })], []) })],
  });
  app.run("toggleQueueExpanded(); render()");
  assert.match(app.el("queue-jobs").text, /GPU box: mlq isn't responding/);
  assert.equal(app.run('queueOf(jobByKey("1@gpu")).snapshot.connected'), false);
});
