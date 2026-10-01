import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts, settle } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const RESPONSE_ID = "10cd3422-3dbd-481f-a5b0-5915a1f7f5be";

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
  send(payload) { this.sent.push(payload); }
  close() { this.emit("close", { code: 1000 }); }
}

function promptMessage(text = "prompt") {
  return JSON.stringify({
    version: 1,
    type: "prompt",
    delivery_id: DELIVERY_ID,
    payload: { session: "DevCockpit:DEV:DC-030", text },
  });
}

function pendingResponse() {
  return {
    response_id: RESPONSE_ID,
    delivery_id: DELIVERY_ID,
    session: "DevCockpit:DEV:DC-030",
    text: "returned response",
    created_at: "2026-10-01T16:00:00.000Z",
    last_error: null,
  };
}

async function setup({ failStorage = false, pending = [] } = {}) {
  const storage = createMemoryStorage();
  storage.failNextSet = failStorage;
  const timers = [];
  const states = [];
  const sockets = [];
  const responseAcks = [];
  const responseErrors = [];

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
    getPendingResponses: async () => pending,
    onResponseAck: async (responseId) => responseAcks.push(responseId),
    onResponseError: async (responseId, code) =>
      responseErrors.push({ responseId, code }),
    onState: (state) => states.push({ ...state }),
    setTimeoutFn: (callback, delay) => {
      timers.push({ callback, delay });
      return timers.length;
    },
    clearTimeoutFn: () => {},
  });
  transport.connect();
  return { store, timers, states, sockets, transport, responseAcks, responseErrors };
}

test("prompt ACK is sent only after local persistence", async () => {
  const { store, sockets } = await setup();
  const socket = sockets[0];
  socket.emit("open");
  socket.emit("message", { data: promptMessage() });
  await settle();
  assert.equal((await store.list()).length, 1);
  assert.equal(JSON.parse(socket.sent[0]).type, "ack");

  const failed = await setup({ failStorage: true });
  failed.sockets[0].emit("open");
  failed.sockets[0].emit("message", { data: promptMessage() });
  await settle();
  assert.equal(failed.sockets[0].sent.length, 0);
});

test("identical prompt replay does not duplicate queue", async () => {
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

test("pending response replays with same response_id after reconnect", async () => {
  const { sockets, timers } = await setup({ pending: [pendingResponse()] });
  const first = sockets[0];
  first.emit("open");
  await settle();
  const firstWire = JSON.parse(first.sent[0]);
  assert.equal(firstWire.type, "chatgpt_response");
  assert.equal(firstWire.response_id, RESPONSE_ID);

  first.emit("close", { code: 1006 });
  timers[0].callback();
  const second = sockets[1];
  second.emit("open");
  await settle();
  assert.equal(second.sent.length, 1);
  assert.equal(JSON.parse(second.sent[0]).response_id, RESPONSE_ID);
  assert.equal(JSON.parse(second.sent[0]).delivery_id, DELIVERY_ID);
});

test("response ACK and correlated errors are routed explicitly", async () => {
  const { sockets, responseAcks, responseErrors, states } =
    await setup({ pending: [pendingResponse()] });
  const socket = sockets[0];
  socket.emit("open");
  await settle();

  socket.emit("message", { data: JSON.stringify({
    version: 1,
    type: "chatgpt_response_ack",
    response_id: RESPONSE_ID,
  }) });
  await settle();
  assert.equal(responseAcks[0], RESPONSE_ID);

  socket.emit("message", { data: JSON.stringify({
    version: 1,
    type: "error",
    code: "session_mismatch",
    response_id: RESPONSE_ID,
  }) });
  await settle();
  assert.equal(responseErrors[0].responseId, RESPONSE_ID);
  assert.equal(responseErrors[0].code, "session_mismatch");
  assert.match(states.at(-1).lastError, /session_mismatch/);
});

test("normal close reconnects and companion conflict fails closed", async () => {
  let value = await setup();
  value.sockets[0].emit("open");
  value.sockets[0].emit("close", { code: 1006 });
  await settle();
  assert.equal(value.timers[0].delay, 1000);
  value.timers[0].callback();
  assert.equal(value.sockets.length, 2);

  value = await setup();
  value.sockets[0].emit("open");
  value.sockets[0].emit("close", { code: 4409 });
  await settle();
  assert.equal(value.timers.length, 0);
  assert.equal(value.states.at(-1).status, "CONFLICT");
  value.transport.retry();
  assert.equal(value.sockets.length, 2);
});
