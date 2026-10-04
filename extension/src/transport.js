(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const {
    parseServerMessage,
    buildAckMessage,
    buildChatGptSendStatusMessage,
    buildChatGptResponseMessage,
  } = namespace.protocol;

  const CONNECTION_STATUS = Object.freeze({
    CONNECTED: "CONNECTED",
    RECONNECTING: "RECONNECTING",
    DISCONNECTED: "DISCONNECTED",
    CONFLICT: "CONFLICT",
  });

  const SINGLE_COMPANION_CLOSE_CODE = 4409;
  const DEFAULT_RECONNECT_DELAYS_MS = Object.freeze([1000, 2000, 4000, 8000, 15000]);

  function errorText(error) {
    return error instanceof Error ? error.message : String(error);
  }

  function responseWirePayload(response) {
    return {
      responseId: response.response_id,
      deliveryId: response.delivery_id,
      session: response.session,
      text: response.text,
    };
  }

  class CompanionTransport {
    constructor({
      url,
      webSocketFactory,
      onPrompt,
      onState,
      getPendingResponses = async () => [],
      getPendingSendStatuses = async () => [],
      onPromptAccepted = async () => {},
      onResponseAck = async () => {},
      onResponseError = async () => {},
      onSendStatusAck = async () => {},
      setTimeoutFn = globalThis.setTimeout.bind(globalThis),
      clearTimeoutFn = globalThis.clearTimeout.bind(globalThis),
      reconnectDelaysMs = DEFAULT_RECONNECT_DELAYS_MS,
    }) {
      this.url = url;
      this.webSocketFactory = webSocketFactory;
      this.onPrompt = onPrompt;
      this.onState = onState;
      this.getPendingResponses = getPendingResponses;
      this.getPendingSendStatuses = getPendingSendStatuses;
      this.onPromptAccepted = onPromptAccepted;
      this.onResponseAck = onResponseAck;
      this.onResponseError = onResponseError;
      this.onSendStatusAck = onSendStatusAck;
      this.setTimeoutFn = setTimeoutFn;
      this.clearTimeoutFn = clearTimeoutFn;
      this.reconnectDelaysMs = reconnectDelaysMs;
      this.socket = null;
      this.reconnectTimer = null;
      this.reconnectAttempt = 0;
      this.blockedByCompanionConflict = false;
      this.stopped = false;
      this.currentStatus = CONNECTION_STATUS.DISCONNECTED;
      this.messageChain = Promise.resolve();
    }

    _emit(status, lastError = null) {
      this.currentStatus = status;
      this.onState?.({ status, lastError });
    }

    connect({ manual = false } = {}) {
      if (this.stopped) {
        return;
      }
      if (this.blockedByCompanionConflict && !manual) {
        return;
      }
      if (manual) {
        this.blockedByCompanionConflict = false;
        this.reconnectAttempt = 0;
      }
      if (this.reconnectTimer !== null) {
        this.clearTimeoutFn(this.reconnectTimer);
        this.reconnectTimer = null;
      }

      this._emit(CONNECTION_STATUS.RECONNECTING);
      const socket = this.webSocketFactory(this.url);
      this.socket = socket;

      socket.addEventListener("open", () => {
        if (this.socket !== socket) {
          return;
        }
        this.reconnectAttempt = 0;
        this._emit(CONNECTION_STATUS.CONNECTED);
        this.messageChain = this.messageChain
          .then(() => this._replayPendingOutbound(socket))
          .catch((error) => this._emit(this.currentStatus, errorText(error)));
      });

      socket.addEventListener("message", (event) => {
        if (this.socket !== socket) {
          return;
        }
        this.messageChain = this.messageChain
          .then(() => this._handleMessage(socket, event.data))
          .catch((error) => this._emit(this.currentStatus, errorText(error)));
      });

      socket.addEventListener("close", (event) => {
        if (this.socket !== socket) {
          return;
        }
        this.socket = null;
        if (event.code === SINGLE_COMPANION_CLOSE_CODE) {
          this.blockedByCompanionConflict = true;
          this._emit(
            CONNECTION_STATUS.CONFLICT,
            "Un autre compagnon est déjà connecté",
          );
          return;
        }
        this._scheduleReconnect();
      });

      socket.addEventListener("error", () => {
        if (this.socket === socket && this.currentStatus === CONNECTION_STATUS.CONNECTED) {
          this._emit(CONNECTION_STATUS.DISCONNECTED, "Erreur WebSocket");
        }
      });
    }

    async _replayPendingOutbound(socket) {
      const pendingResponses = await this.getPendingResponses();
      for (const response of pendingResponses) {
        if (this.socket !== socket || this.currentStatus !== CONNECTION_STATUS.CONNECTED) {
          return;
        }
        socket.send(buildChatGptResponseMessage(responseWirePayload(response)));
      }
      const pendingStatuses = await this.getPendingSendStatuses();
      for (const event of pendingStatuses) {
        if (this.socket !== socket || this.currentStatus !== CONNECTION_STATUS.CONNECTED) {
          return;
        }
        socket.send(buildChatGptSendStatusMessage(event));
      }
    }

    async _handleMessage(socket, rawMessage) {
      const message = parseServerMessage(rawMessage);

      if (message.type === "prompt") {
        await this.onPrompt({
          deliveryId: message.deliveryId,
          session: message.session,
          text: message.text,
          routing: message.routing,
        });
        socket.send(buildAckMessage(message.deliveryId));
        Promise.resolve(
          this.onPromptAccepted({
            deliveryId: message.deliveryId,
            session: message.session,
            text: message.text,
            routing: message.routing,
          }),
        ).catch((error) => this._emit(this.currentStatus, errorText(error)));
        return;
      }

      if (message.type === "chatgpt_send_status_ack") {
        await this.onSendStatusAck(message.eventId);
        return;
      }

      if (message.type === "chatgpt_response_ack") {
        await this.onResponseAck(message.responseId);
        return;
      }

      if (message.type === "error") {
        if (message.responseId) {
          await this.onResponseError(message.responseId, message.code);
        }
        this._emit(this.currentStatus, "Serveur: " + message.code);
      }
    }

    sendPendingSendStatus(event) {
      if (
        this.currentStatus !== CONNECTION_STATUS.CONNECTED ||
        !this.socket
      ) {
        return false;
      }
      try {
        this.socket.send(buildChatGptSendStatusMessage(event));
        return true;
      } catch (error) {
        this._emit(this.currentStatus, errorText(error));
        return false;
      }
    }

    sendPendingResponse(response) {
      if (
        this.currentStatus !== CONNECTION_STATUS.CONNECTED ||
        !this.socket
      ) {
        return false;
      }
      try {
        this.socket.send(buildChatGptResponseMessage(responseWirePayload(response)));
        return true;
      } catch (error) {
        this._emit(this.currentStatus, errorText(error));
        return false;
      }
    }

    _scheduleReconnect() {
      if (this.stopped || this.blockedByCompanionConflict) {
        this._emit(CONNECTION_STATUS.DISCONNECTED);
        return;
      }
      const index = Math.min(
        this.reconnectAttempt,
        this.reconnectDelaysMs.length - 1,
      );
      const delay = this.reconnectDelaysMs[index];
      this.reconnectAttempt += 1;
      this._emit(CONNECTION_STATUS.RECONNECTING);
      this.reconnectTimer = this.setTimeoutFn(() => {
        this.reconnectTimer = null;
        this.connect();
      }, delay);
    }

    retry() {
      this.connect({ manual: true });
    }

    stop() {
      this.stopped = true;
      if (this.reconnectTimer !== null) {
        this.clearTimeoutFn(this.reconnectTimer);
        this.reconnectTimer = null;
      }
      if (this.socket?.close) {
        this.socket.close();
      }
      this.socket = null;
      this._emit(CONNECTION_STATUS.DISCONNECTED);
    }
  }

  namespace.transport = {
    CONNECTION_STATUS,
    SINGLE_COMPANION_CLOSE_CODE,
    DEFAULT_RECONNECT_DELAYS_MS,
    CompanionTransport,
  };
})();
