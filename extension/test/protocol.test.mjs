import assert from "node:assert/strict";
import test from "node:test";
import { loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const RESPONSE_ID = "10cd3422-3dbd-481f-a5b0-5915a1f7f5be";

test("protocol parses a strict v1 prompt", async () => {
  const context = await loadClassicScripts(["src/protocol.js"]);
  const { parseServerMessage } = context.DevCockpitCompanion.protocol;
  const parsed = parseServerMessage(JSON.stringify({
    version: 1,
    type: "prompt",
    delivery_id: DELIVERY_ID,
    payload: { session: "DevCockpit:DEV:DC-012", text: "Implement DC-012" },
  }));
  assert.equal(parsed.type, "prompt");
  assert.equal(parsed.deliveryId, DELIVERY_ID);
  assert.equal(parsed.session, "DevCockpit:DEV:DC-012");
});

test("protocol builds chatgpt_response and parses its ACK", async () => {
  const context = await loadClassicScripts(["src/protocol.js"]);
  const { buildChatGptResponseMessage, parseServerMessage } =
    context.DevCockpitCompanion.protocol;

  const raw = buildChatGptResponseMessage({
    responseId: RESPONSE_ID,
    deliveryId: DELIVERY_ID,
    session: "DevCockpit:DEV:DC-030",
    text: "Returned response\nwith formatting",
  });
  const wire = JSON.parse(raw);
  assert.equal(wire.version, 1);
  assert.equal(wire.type, "chatgpt_response");
  assert.equal(wire.response_id, RESPONSE_ID);
  assert.equal(wire.delivery_id, DELIVERY_ID);
  assert.equal(wire.payload.session, "DevCockpit:DEV:DC-030");
  assert.equal(wire.payload.text, "Returned response\nwith formatting");

  const ack = parseServerMessage(JSON.stringify({
    version: 1,
    type: "chatgpt_response_ack",
    response_id: RESPONSE_ID,
  }));
  assert.equal(ack.type, "chatgpt_response_ack");
  assert.equal(ack.responseId, RESPONSE_ID);
});

test("protocol preserves response_id on correlated errors", async () => {
  const context = await loadClassicScripts(["src/protocol.js"]);
  const parsed = context.DevCockpitCompanion.protocol.parseServerMessage(
    JSON.stringify({
      version: 1,
      type: "error",
      code: "session_mismatch",
      response_id: RESPONSE_ID,
    }),
  );
  assert.equal(parsed.code, "session_mismatch");
  assert.equal(parsed.responseId, RESPONSE_ID);
});

test("oversize response fails explicitly instead of truncating", async () => {
  const context = await loadClassicScripts(["src/protocol.js"]);
  const { buildChatGptResponseMessage } = context.DevCockpitCompanion.protocol;
  assert.throws(
    () => buildChatGptResponseMessage({
      responseId: RESPONSE_ID,
      deliveryId: DELIVERY_ID,
      session: "DevCockpit:DEV:DC-030",
      text: "x".repeat(600 * 1024),
    }),
    (error) => error?.code === "message_too_large",
  );
});

for (const [name, message, expectedCode] of [
  ["invalid version", { version: 2, type: "pong" }, "unsupported_version"],
  ["unexpected type", { version: 1, type: "unknown" }, "unexpected_type"],
  [
    "invalid delivery id",
    {
      version: 1,
      type: "prompt",
      delivery_id: "not-a-uuid",
      payload: { session: "s", text: "t" },
    },
    "invalid_delivery_id",
  ],
  [
    "invalid response ack uuid",
    { version: 1, type: "chatgpt_response_ack", response_id: "bad" },
    "invalid_response_id",
  ],
]) {
  test("protocol rejects " + name, async () => {
    const context = await loadClassicScripts(["src/protocol.js"]);
    assert.throws(
      () => context.DevCockpitCompanion.protocol.parseServerMessage(JSON.stringify(message)),
      (error) => error?.code === expectedCode,
    );
  });
}
