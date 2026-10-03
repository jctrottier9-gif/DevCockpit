import assert from "node:assert/strict";
import test from "node:test";
import { loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const RESPONSE_ID = "10cd3422-3dbd-481f-a5b0-5915a1f7f5be";
const ROUTING = {
  binding_version: 4,
  conversation_id: "conv-a",
  canonical_url: "https://chatgpt.com/c/conv-a",
};

test("protocol parses strict v2 prompts with bound and null routing", async () => {
  const context = await loadClassicScripts(["src/protocol.js"]);
  const { parseServerMessage } = context.DevCockpitCompanion.protocol;
  const bound = parseServerMessage(JSON.stringify({
    version: 2,
    type: "prompt",
    delivery_id: DELIVERY_ID,
    payload: { session: "DevCockpit:DEV:DC-063A", text: "Implement DC-063A" },
    routing: ROUTING,
  }));
  assert.equal(bound.type, "prompt");
  assert.equal(bound.deliveryId, DELIVERY_ID);
  assert.equal(bound.session, "DevCockpit:DEV:DC-063A");
  assert.deepEqual(bound.routing, ROUTING);

  const provisional = parseServerMessage(JSON.stringify({
    version: 2,
    type: "prompt",
    delivery_id: DELIVERY_ID,
    payload: { session: "DevCockpit:DEV:NEW", text: "new" },
    routing: null,
  }));
  assert.equal(provisional.routing, null);
});

test("protocol v1 is explicitly incompatible with v2 companion", async () => {
  const context = await loadClassicScripts(["src/protocol.js"]);
  assert.throws(
    () => context.DevCockpitCompanion.protocol.parseServerMessage(JSON.stringify({
      version: 1,
      type: "prompt",
      delivery_id: DELIVERY_ID,
      payload: { session: "DevCockpit:DEV:DC-063A", text: "legacy" },
    })),
    (error) => error?.code === "unsupported_version",
  );
});

test("protocol builds v2 chatgpt_response and parses its ACK", async () => {
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
  assert.equal(wire.version, 2);
  assert.equal(wire.type, "chatgpt_response");

  const ack = parseServerMessage(JSON.stringify({
    version: 2,
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
      version: 2,
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
  ["unexpected type", { version: 2, type: "unknown" }, "unexpected_type"],
  [
    "invalid delivery id",
    {
      version: 2,
      type: "prompt",
      delivery_id: "not-a-uuid",
      payload: { session: "s", text: "t" },
      routing: null,
    },
    "invalid_delivery_id",
  ],
  [
    "invalid routing",
    {
      version: 2,
      type: "prompt",
      delivery_id: DELIVERY_ID,
      payload: { session: "s", text: "t" },
      routing: { binding_version: 1, conversation_id: "x" },
    },
    "invalid_routing",
  ],
  [
    "invalid response ack uuid",
    { version: 2, type: "chatgpt_response_ack", response_id: "bad" },
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
