"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/tensorwatch/web/app.js"), "utf8");

function dashboard(selected = "short") {
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const requests = [];
  const listeners = new Map();
  const element = () => ({ addEventListener() {} });
  const document = {
    hidden: false,
    getElementById: element,
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
      if (!options) return new Promise(() => {}); // Initial state is supplied below.
      requests.push({ url, at: now });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    },
  });
  vm.runInContext(source, context);
  return {
    requests,
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
