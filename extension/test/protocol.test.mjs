import assert from "node:assert/strict";
import test from "node:test";
import { loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";

test("protocol parses a strict v1 prompt", async () => {
  const context = await loadClassicScripts(["src/protocol.js"]);
  const { parseServerMessage } = context.DevCockpitCompanion.protocol;

  const parsed = parseServerMessage(
    JSON.stringify({
      version: 1,
      type: "prompt",
      delivery_id: DELIVERY_ID,
      payload: {
        session: "DevCockpit:DEV:DC-012",
        text: "Implement DC-012",
      },
    }),
  );

  assert.equal(parsed.type, "prompt");
  assert.equal(parsed.deliveryId, DELIVERY_ID);
  assert.equal(parsed.session, "DevCockpit:DEV:DC-012");
  assert.equal(parsed.text, "Implement DC-012");
});

for (const [name, message, expectedCode] of [
  [
    "invalid version",
    { version: 2, type: "pong" },
    "unsupported_version",
  ],
  [
    "unexpected type",
    { version: 1, type: "chatgpt_response" },
    "unexpected_type",
  ],
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
    "invalid payload",
    {
      version: 1,
      type: "prompt",
      delivery_id: DELIVERY_ID,
      payload: { session: "s", text: "t", role: "DEV" },
    },
    "invalid_payload",
  ],
]) {
  test(`protocol rejects ${name}`, async () => {
    const context = await loadClassicScripts(["src/protocol.js"]);
    const { parseServerMessage } = context.DevCockpitCompanion.protocol;
    assert.throws(
      () => parseServerMessage(JSON.stringify(message)),
      (error) => error?.code === expectedCode,
    );
  });
}
