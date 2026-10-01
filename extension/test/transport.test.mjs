import assert from "node:assert/strict";
import test from "node:test";
import {
  createMemoryStorage,
  loadClassicScripts,
  settle,
} from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    this.sent = [];
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  emit(type, event = {}) {
    for (const handler of this.listeners.get(type) || []) {
      handler(event);
    }
  }

  send(payload) {
    this.sent.push(payload);
  }

  close() {
    this.emit("close", { code: 1000 });
  }
}

function promptMessage(text = "prompt") {
  return JSON.stringify({
    version: 1,
    type: "prompt",
    delivery_id: DELIVERY_ID,
    payload: {
      session: "DevCockpit:DEV:DC-012",
      text,
    },
  });
}

async function setup({ failStorage = false } = {}) {
  const storage = createMemoryStorage();
  storage.failNextSet = failStorage;

  const timers = [];
  const states = [];
  const sockets = [];

  const context = await loadClassicScripts([
    "src/protocol.js",
    "src/queue-store.js",
    "src/transport.js",
  ]);
  const { QueueStore } = context.DevCockpitCompanion.queue;
  const { CompanionTransport } = context.DevCockpitCompanion.transport;
  const store = new QueueStore(storage);

  const transport = new CompanionTransport({
    url: "ws://127.0.0.1:8000/api/companion/ws",
    webSocketFactory: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    onPrompt: (prompt) => store.acceptPrompt(prompt),
    onState: (state) => states.push({ ...state }),
    setTimeoutFn: (callback, delay) => {
      timers.push({ callback, delay });
      return timers.length;
    },
    clearTimeoutFn: () => {},
  });

  transport.connect();
  return { store, timers, states, sockets, transport };
}

test("ACK is sent only after prompt persistence succeeds", async () => {
  const { store, sockets } = await setup();
  const socket = sockets[0];
  socket.emit("open");
  socket.emit("message", { data: promptMessage() });
  await settle();

  assert.equal((await store.list()).length, 1);
  assert.equal(socket.sent.length, 1);
  assert.deepEqual(JSON.parse(socket.sent[0]), {
    version: 1,
    type: "ack",
    delivery_id: DELIVERY_ID,
  });
});

test("storage failure produces no ACK", async () => {
  const { store, sockets } = await setup({ failStorage: true });
  const socket = sockets[0];
  socket.emit("open");
  socket.emit("message", { data: promptMessage() });
  await settle();

  assert.equal((await store.list()).length, 0);
  assert.equal(socket.sent.length, 0);
});

test("identical replay gets a new ACK without queue duplication", async () => {
  const { store, sockets } = await setup();
  const socket = sockets[0];
  socket.emit("open");
  socket.emit("message", { data: promptMessage("same") });
  socket.emit("message", { data: promptMessage("same") });
  await settle();
  await settle();

  assert.equal((await store.list()).length, 1);
  assert.equal(socket.sent.length, 2);
});

test("normal close schedules bounded reconnect", async () => {
  const { sockets, timers, states } = await setup();
  sockets[0].emit("open");
  sockets[0].emit("close", { code: 1006 });
  await settle();

  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 1000);
  assert.equal(states.at(-1).status, "RECONNECTING");

  timers[0].callback();
  assert.equal(sockets.length, 2);
});

test("single companion close is explicit and does not reconnect aggressively", async () => {
  const { sockets, timers, states, transport } = await setup();
  sockets[0].emit("open");
  sockets[0].emit("close", { code: 4409 });
  await settle();

  assert.equal(timers.length, 0);
  assert.equal(states.at(-1).status, "CONFLICT");
  assert.match(states.at(-1).lastError, /autre compagnon/i);

  transport.retry();
  assert.equal(sockets.length, 2);
});
