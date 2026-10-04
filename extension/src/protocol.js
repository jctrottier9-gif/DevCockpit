(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const PROTOCOL_VERSION = 2;
  const MAX_MESSAGE_BYTES = 512 * 1024;
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
    const sortedExpected = [...expected].sort();
    return (
      keys.length === sortedExpected.length &&
      sortedExpected.every((key, index) => key === keys[index])
    );
  }

  function isUuid(value) {
    return typeof value === "string" && UUID_PATTERN.test(value);
  }

  function utf8ByteLength(value) {
    let bytes = 0;
    for (const character of value) {
      const point = character.codePointAt(0);
      bytes += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
    }
    return bytes;
  }

  function parseRoutingSnapshot(value) {
    if (value === null) {
      return null;
    }
    if (
      !isPlainObject(value) ||
      !hasExactKeys(value, ["binding_version", "canonical_url", "conversation_id"]) ||
      !Number.isInteger(value.binding_version) ||
      value.binding_version < 1 ||
      typeof value.conversation_id !== "string" ||
      value.conversation_id.trim() === "" ||
      typeof value.canonical_url !== "string" ||
      value.canonical_url.trim() === ""
    ) {
      throw new ProtocolError("invalid_routing");
    }
    return {
      binding_version: value.binding_version,
      conversation_id: value.conversation_id,
      canonical_url: value.canonical_url,
    };
  }

  function parseServerMessage(rawMessage) {
    if (typeof rawMessage !== "string") {
      throw new ProtocolError("invalid_message");
    }
    if (utf8ByteLength(rawMessage) > MAX_MESSAGE_BYTES) {
      throw new ProtocolError("message_too_large");
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
      if (
        !hasExactKeys(message, [
          "delivery_id",
          "payload",
          "routing",
          "type",
          "version",
        ])
      ) {
        throw new ProtocolError("invalid_prompt");
      }
      if (!isUuid(message.delivery_id)) {
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
        routing: parseRoutingSnapshot(message.routing),
      };
    }

    if (message.type === "chatgpt_response_ack") {
      if (!hasExactKeys(message, ["response_id", "type", "version"])) {
        throw new ProtocolError("invalid_response_ack");
      }
      if (!isUuid(message.response_id)) {
        throw new ProtocolError("invalid_response_id");
      }
      return { type: "chatgpt_response_ack", responseId: message.response_id };
    }

    if (message.type === "pong") {
      if (!hasExactKeys(message, ["type", "version"])) {
        throw new ProtocolError("invalid_pong");
      }
      return { type: "pong" };
    }

    if (message.type === "error") {
      const validShape =
        hasExactKeys(message, ["code", "type", "version"]) ||
        hasExactKeys(message, ["code", "response_id", "type", "version"]);
      if (
        !validShape ||
        typeof message.code !== "string" ||
        message.code.trim() === "" ||
        ("response_id" in message && !isUuid(message.response_id))
      ) {
        throw new ProtocolError("invalid_error");
      }
      return {
        type: "error",
        code: message.code,
        responseId: message.response_id || null,
      };
    }

    throw new ProtocolError("unexpected_type");
  }

  function buildAckMessage(deliveryId) {
    if (!isUuid(deliveryId)) {
      throw new ProtocolError("invalid_delivery_id");
    }
    return JSON.stringify({
      version: PROTOCOL_VERSION,
      type: "ack",
      delivery_id: deliveryId,
    });
  }

  function buildChatGptResponseMessage({ responseId, deliveryId, session, text }) {
    if (!isUuid(responseId)) {
      throw new ProtocolError("invalid_response_id");
    }
    if (!isUuid(deliveryId)) {
      throw new ProtocolError("invalid_delivery_id");
    }
    if (typeof session !== "string" || session.trim() === "") {
      throw new ProtocolError("invalid_response_session");
    }
    if (typeof text !== "string" || text.trim() === "") {
      throw new ProtocolError("invalid_response_text");
    }

    const rawMessage = JSON.stringify({
      version: PROTOCOL_VERSION,
      type: "chatgpt_response",
      response_id: responseId,
      delivery_id: deliveryId,
      payload: { session, text },
    });
    if (utf8ByteLength(rawMessage) > MAX_MESSAGE_BYTES) {
      throw new ProtocolError("message_too_large");
    }
    return rawMessage;
  }

  namespace.protocol = {
    PROTOCOL_VERSION,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    parseServerMessage,
    buildAckMessage,
    buildChatGptResponseMessage,
  };
})();
