(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const PROTOCOL_VERSION = 1;
  const UUID_PATTERN =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  class ProtocolError extends Error {
    constructor(code) {
      super(code);
      this.name = "ProtocolError";
      this.code = code;
    }
  }

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function hasExactKeys(value, expected) {
    const keys = Object.keys(value).sort();
    return (
      keys.length === expected.length &&
      expected.every((key, index) => key === keys[index])
    );
  }

  function parseServerMessage(rawMessage) {
    if (typeof rawMessage !== "string") {
      throw new ProtocolError("invalid_message");
    }

    let message;
    try {
      message = JSON.parse(rawMessage);
    } catch {
      throw new ProtocolError("invalid_json");
    }

    if (!isPlainObject(message)) {
      throw new ProtocolError("invalid_message");
    }
    if (message.version !== PROTOCOL_VERSION) {
      throw new ProtocolError("unsupported_version");
    }

    if (message.type === "prompt") {
      if (!hasExactKeys(message, ["delivery_id", "payload", "type", "version"])) {
        throw new ProtocolError("invalid_prompt");
      }
      if (
        typeof message.delivery_id !== "string" ||
        !UUID_PATTERN.test(message.delivery_id)
      ) {
        throw new ProtocolError("invalid_delivery_id");
      }
      if (
        !isPlainObject(message.payload) ||
        !hasExactKeys(message.payload, ["session", "text"]) ||
        typeof message.payload.session !== "string" ||
        message.payload.session.trim() === "" ||
        typeof message.payload.text !== "string" ||
        message.payload.text.trim() === ""
      ) {
        throw new ProtocolError("invalid_payload");
      }

      return {
        type: "prompt",
        deliveryId: message.delivery_id,
        session: message.payload.session,
        text: message.payload.text,
      };
    }

    if (message.type === "pong") {
      if (!hasExactKeys(message, ["type", "version"])) {
        throw new ProtocolError("invalid_pong");
      }
      return { type: "pong" };
    }

    if (message.type === "error") {
      if (
        !hasExactKeys(message, ["code", "type", "version"]) ||
        typeof message.code !== "string" ||
        message.code.trim() === ""
      ) {
        throw new ProtocolError("invalid_error");
      }
      return { type: "error", code: message.code };
    }

    throw new ProtocolError("unexpected_type");
  }

  function buildAckMessage(deliveryId) {
    if (typeof deliveryId !== "string" || !UUID_PATTERN.test(deliveryId)) {
      throw new ProtocolError("invalid_delivery_id");
    }
    return JSON.stringify({
      version: PROTOCOL_VERSION,
      type: "ack",
      delivery_id: deliveryId,
    });
  }

  namespace.protocol = {
    PROTOCOL_VERSION,
    ProtocolError,
    parseServerMessage,
    buildAckMessage,
  };
})();
