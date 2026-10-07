(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const { SEND_STATE, RECOVERY_STATUS } = namespace.sendStore;

  const DEFAULT_RETRY_DELAYS_MS = Object.freeze([250, 750, 1500, 2500, 4000]);
  const DEFAULT_RECOVERY_READY_DELAYS_MS = Object.freeze([0, 100, 250, 500, 1000, 2000]);
  const RETRYABLE_READY_ERRORS = new Set([
    "composer_not_found",
    "send_button_not_found",
    "send_button_disabled",
    "content_script_unavailable",
  ]);

  function isArchitectureSession(session) {
    if (typeof session !== "string") return false;
    const parts = session.split(":");
    return parts.length === 3 && parts[1] === "ARCH" && parts[0] !== "" && parts[2] !== "";
  }

  function errorText(error) {
    return error instanceof Error ? error.message : String(error);
  }

  function preSendErrorCode(error) {
    const message = errorText(error);
    if (/receiving end does not exist/i.test(message)) {
      return "content_script_unavailable";
    }
    return message;
  }

  function isLegacySyntheticConversationId(value) {
    if (typeof value !== "string" || value === "") return false;
    let decoded = value;
    try {
      decoded = decodeURIComponent(value);
    } catch {
      // Keep the original value for malformed legacy data.
    }
    return decoded.toLowerCase().startsWith("local-chatgpt:");
  }

  function isLegacySyntheticRouting(routing) {
    return Boolean(
      routing &&
      typeof routing === "object" &&
      isLegacySyntheticConversationId(routing.conversation_id)
    );
  }

  function routingFromActiveConversation(legacyRouting, rawUrl) {
    if (!isLegacySyntheticRouting(legacyRouting)) return null;
    const conversation = canonicalConversation(rawUrl);
    if (
      !conversation ||
      isLegacySyntheticConversationId(conversation.conversation_id)
    ) {
      return null;
    }
    return {
      binding_version: legacyRouting.binding_version,
      conversation_id: conversation.conversation_id,
      canonical_url: conversation.canonical_url,
    };
  }

  function canonicalConversation(rawUrl) {
    if (typeof rawUrl !== "string" || !rawUrl) return null;
    try {
      const parsed = new URL(rawUrl);
      if (!["chatgpt.com", "chat.openai.com"].includes(parsed.hostname.toLowerCase())) {
        return null;
      }
      const parts = parsed.pathname.split("/").filter(Boolean);
      if (
        parts.length < 2 ||
        parts[parts.length - 2] !== "c" ||
        !parts[parts.length - 1]
      ) {
        return null;
      }
      const conversationId = parts[parts.length - 1];
      return {
        conversation_id: conversationId,
        canonical_url: "https://chatgpt.com/c/" + conversationId,
      };
    } catch {
      return null;
    }
  }

  function shouldCloseManagedTabAfterAck({
    event,
    sentContext,
    queuedPrompts = [],
  }) {
    if (
      !event ||
      event.state !== SEND_STATE.SENT_CONFIRMED ||
      !sentContext ||
      sentContext.delivery_id !== event.delivery_id ||
      !Number.isInteger(sentContext.tab_id) ||
      isArchitectureSession(sentContext.session)
    ) {
      return false;
    }
    return !queuedPrompts.some(
      (entry) => entry?.session === sentContext.session,
    );
  }

  function sentContextMatchesTab(sentContext, tab) {
    if (
      !sentContext ||
      !tab ||
      !Number.isInteger(tab.id)
    ) {
      return false;
    }
    if (
      typeof sentContext.conversation_url === "string" &&
      sentContext.conversation_url
    ) {
      const expected = canonicalConversation(sentContext.conversation_url);
      const observed = canonicalConversation(tab.url);
      return Boolean(
        expected &&
        observed &&
        expected.canonical_url === observed.canonical_url
      );
    }
    if (Number.isInteger(sentContext.tab_id)) {
      return sentContext.tab_id === tab.id;
    }
    return true;
  }

  function bindingConflictDetails({
    session,
    expectedRouting = null,
    observedConversation = null,
    observedBindingVersion = null,
  }) {
    return {
      agent_session: session,
      expected_conversation_id: expectedRouting?.conversation_id || null,
      expected_canonical_url: expectedRouting?.canonical_url || null,
      expected_binding_version: expectedRouting?.binding_version || null,
      observed_conversation_id: observedConversation?.conversation_id || null,
      observed_canonical_url: observedConversation?.canonical_url || null,
      observed_binding_version:
        Number.isInteger(observedBindingVersion) ? observedBindingVersion : null,
    };
  }

  function observedConversationFromRoutingError(error) {
    const details = typeof error?.details === "string" ? error.details : errorText(error);
    const match = details.match(/(?:^|,)observed=([^,]+)/);
    if (!match) return null;
    const conversationId = match[1];
    if (
      !conversationId ||
      ["unknown", "non_conversation"].includes(conversationId)
    ) {
      return null;
    }
    return {
      conversation_id: conversationId,
      canonical_url: "https://chatgpt.com/c/" + conversationId,
    };
  }

  class PromptSendCoordinator {
    constructor({
      queueStore,
      sentPromptStore,
      sendStore,
      router,
      sendToTab,
      wakeTab = async () => {},
      emitStatus = () => false,
      sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay)),
      retryDelaysMs = DEFAULT_RETRY_DELAYS_MS,
    }) {
      this.queueStore = queueStore;
      this.sentPromptStore = sentPromptStore;
      this.sendStore = sendStore;
      this.router = router;
      this.sendToTab = sendToTab;
      this.wakeTab = wakeTab;
      this.emitStatus = emitStatus;
      this.sleep = sleep;
      this.retryDelaysMs = retryDelaysMs;
      this.deliveryWorkers = new Map();
      this.sessionChains = new Map();
      this.enqueueSetupChain = Promise.resolve();
    }

    async _emit(result) {
      try {
        this.emitStatus(result.event);
      } catch {
        // Durable outbox is replayed by the transport.
      }
      return result;
    }

    async _queueEntry(deliveryId) {
      const queue = await this.queueStore.list();
      return queue.find((entry) => entry.delivery_id === deliveryId) || null;
    }

    async _hasEarlierUnfinished(entry) {
      const queue = await this.queueStore.list();
      for (const candidate of queue) {
        if (candidate.delivery_id === entry.delivery_id) return false;
        if (candidate.session === entry.session) {
          const state = await this.sendStore.get(candidate.delivery_id);
          if (!state || state.state !== SEND_STATE.SENT_CONFIRMED) return true;
        }
      }
      return false;
    }

    enqueue(deliveryId, { manualTabId = null } = {}) {
      const existingWorker = this.deliveryWorkers.get(deliveryId);
      if (existingWorker) return existingWorker;

      const setup = this.enqueueSetupChain.then(async () => {
        const entry = await this._queueEntry(deliveryId);
        if (!entry) {
          return {
            run: Promise.resolve({
              ok: false,
              error: "delivery_not_found:" + deliveryId,
            }),
          };
        }

        const prior = this.sessionChains.get(entry.session) || Promise.resolve();
        const run = prior.then(
          () => this._run(entry, { manualTabId }),
          () => this._run(entry, { manualTabId }),
        );
        this.sessionChains.set(
          entry.session,
          run.then(() => undefined, () => undefined),
        );
        return { run };
      });
      this.enqueueSetupChain = setup.then(() => undefined, () => undefined);

      const worker = setup
        .then(({ run }) => run)
        .finally(() => {
          this.deliveryWorkers.delete(deliveryId);
        });
      this.deliveryWorkers.set(deliveryId, worker);
      return worker;
    }

    send(deliveryId) {
      return this.enqueue(deliveryId);
    }

    async resumeSession(session) {
      if (typeof session !== "string" || session.trim() === "") {
        return { ok: false, error: "invalid_session" };
      }
      if (isArchitectureSession(session)) {
        return { ok: false, state: SEND_STATE.QUEUED, error: "manual_arch_required" };
      }
      const queue = await this.queueStore.list();
      const entries = queue.filter((entry) => entry.session === session);
      for (const entry of entries) {
        const state = await this.sendStore.get(entry.delivery_id);
        if (!state) {
          return {
            ok: false,
            error: "chatgpt_send_not_found:" + entry.delivery_id,
          };
        }
        if (state.state === SEND_STATE.SENT_CONFIRMED) {
          continue;
        }
        if (
          [
            SEND_STATE.QUEUED,
            SEND_STATE.ROUTING,
            SEND_STATE.WAITING_READY,
            SEND_STATE.RETRYABLE_FAILURE,
          ].includes(state.state)
        ) {
          return this.enqueue(entry.delivery_id);
        }
        return {
          ok: false,
          state: state.state,
          error: "session_head_not_sendable",
        };
      }
      return { ok: true, state: "EMPTY" };
    }

    async _run(entry, { manualTabId = null } = {}) {
      const existing = await this.sendStore.get(entry.delivery_id);
      if (!existing) return { ok: false, error: "chatgpt_send_not_found" };
      if (isArchitectureSession(entry.session) && !Number.isInteger(manualTabId)) {
        return { ok: false, state: SEND_STATE.QUEUED, error: "manual_arch_required" };
      }
      if (
        [SEND_STATE.SEND_ARMED, SEND_STATE.SENT_CONFIRMED, SEND_STATE.AMBIGUOUS].includes(
          existing.state,
        )
      ) {
        return {
          ok: existing.state === SEND_STATE.SENT_CONFIRMED,
          state: existing.state,
        };
      }
      if (existing.state === SEND_STATE.BLOCKED) {
        return { ok: false, state: SEND_STATE.BLOCKED, error: existing.error_code };
      }
      if (await this._hasEarlierUnfinished(entry)) {
        return {
          ok: false,
          state: "WAITING_FIFO",
          error: "session_predecessor_not_complete",
        };
      }

      let attempt = existing.attempt;
      let target = null;
      let prepared = null;
      let tab = null;

      for (let index = 0; index <= this.retryDelaysMs.length; index += 1) {
        attempt += 1;
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.ROUTING,
            attempt,
          }),
        );

        try {
          if (Number.isInteger(manualTabId)) {
            target ||= { tabId: manualTabId };
            tab = await this.router.revalidateManualTarget({
              tabId: manualTabId,
            });
          } else {
            if (!target) {
              target = await this.router.route({
                session: entry.session,
                routing: entry.routing,
              });
            }
            tab = await this.router.revalidateTarget({
              session: entry.session,
              routing: entry.routing,
              tabId: target.tabId,
            });
          }
          prepared = await this.sendToTab(tab.id, {
            type: "devcockpit_prepare_prompt",
            text: entry.text,
          });
        } catch (error) {
          prepared = { ok: false, error: preSendErrorCode(error) };
        }

        if (prepared?.ok === true) break;

        const code = prepared?.error || "chatgpt_prepare_failed";
        const retryable =
          RETRYABLE_READY_ERRORS.has(code) ||
          code.startsWith("composer_not_found:");
        if (!retryable) {
          await this._emit(
            await this.sendStore.transition({
              deliveryId: entry.delivery_id,
              session: entry.session,
              state: SEND_STATE.BLOCKED,
              attempt,
              errorCode: code,
            }),
          );
          return { ok: false, state: SEND_STATE.BLOCKED, error: code };
        }

        const hasRetry = index < this.retryDelaysMs.length;
        if (
          hasRetry &&
          code === "content_script_unavailable" &&
          !Number.isInteger(manualTabId) &&
          Number.isInteger(tab?.id)
        ) {
          try {
            await this.wakeTab(tab.id);
          } catch {
            // Activation is best-effort. The bounded readiness retry remains authoritative.
          }
        }
        const nextRetryAt = hasRetry
          ? new Date(Date.now() + this.retryDelaysMs[index]).toISOString()
          : null;
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: hasRetry ? SEND_STATE.WAITING_READY : SEND_STATE.BLOCKED,
            attempt,
            errorCode: code,
            nextRetryAt,
          }),
        );
        if (!hasRetry) {
          return { ok: false, state: SEND_STATE.BLOCKED, error: code };
        }
        await this.sleep(this.retryDelaysMs[index]);
      }

      try {
        tab = Number.isInteger(manualTabId)
          ? await this.router.revalidateManualTarget({ tabId: manualTabId })
          : await this.router.revalidateTarget({
              session: entry.session,
              routing: entry.routing,
              tabId: target.tabId,
            });
      } catch (error) {
        const code = errorText(error);
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.BLOCKED,
            attempt,
            errorCode: code,
          }),
        );
        return { ok: false, state: SEND_STATE.BLOCKED, error: code };
      }

      await this._emit(
        await this.sendStore.transition({
          deliveryId: entry.delivery_id,
          session: entry.session,
          state: SEND_STATE.SEND_ARMED,
          attempt,
        }),
      );

      let result;
      try {
        result = await this.sendToTab(tab.id, {
          type: "devcockpit_commit_prepared_prompt",
          text: entry.text,
          baseline: prepared.baseline,
        });
      } catch (error) {
        result = { ok: false, error: errorText(error), ambiguous: true };
      }

      if (!result?.ok) {
        const code = result?.error || "send_confirmation_unknown";
        const ambiguousConversation = result?.ambiguous
          ? canonicalConversation(result.conversationUrl)
          : null;
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.AMBIGUOUS,
            attempt,
            conversation: ambiguousConversation,
            errorCode: code,
          }),
        );
        return { ok: false, state: SEND_STATE.AMBIGUOUS, error: code };
      }

      const observedConversation = canonicalConversation(result.conversationUrl);
      if (
        entry.routing &&
        observedConversation &&
        observedConversation.canonical_url !== entry.routing.canonical_url
      ) {
        const ambiguous = await this.sendStore.transition({
          deliveryId: entry.delivery_id,
          session: entry.session,
          state: SEND_STATE.AMBIGUOUS,
          attempt,
          errorCode: "CHATGPT_SEND_BINDING_CONFLICT",
        });
        await this._emit(ambiguous);
        await this.sendStore.setRecoveryStatus(entry.delivery_id, {
          status: RECOVERY_STATUS.INTERVENTION_REQUIRED,
          errorCode: "CHATGPT_SEND_BINDING_CONFLICT",
          bindingConflict: bindingConflictDetails({
            session: entry.session,
            expectedRouting: entry.routing,
            observedConversation,
          }),
        });
        return {
          ok: false,
          state: SEND_STATE.AMBIGUOUS,
          error: "CHATGPT_SEND_BINDING_CONFLICT",
        };
      }

      const conversation =
        observedConversation ||
        (entry.routing
          ? {
              conversation_id: entry.routing.conversation_id,
              canonical_url: entry.routing.canonical_url,
            }
          : null);
      if (!conversation) {
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.AMBIGUOUS,
            attempt,
            errorCode: "confirmed_send_missing_conversation_identity",
          }),
        );
        return {
          ok: false,
          state: SEND_STATE.AMBIGUOUS,
          error: "confirmed_send_missing_conversation_identity",
        };
      }

      const confirmed = await this.sendStore.transition({
        deliveryId: entry.delivery_id,
        session: entry.session,
        state: SEND_STATE.SENT_CONFIRMED,
        attempt,
        conversation,
      });

      try {
        await this.sentPromptStore.recordSent({
          deliveryId: entry.delivery_id,
          session: entry.session,
          tabId: tab.id,
          conversationUrl: conversation.canonical_url,
        });
        await this.queueStore.remove(entry.delivery_id);
      } catch {
        // SENT_CONFIRMED is already durable; never replay the irreversible send.
      }
      await this._emit(confirmed);
      void this.resumeSession(entry.session);
      return { ok: true, state: SEND_STATE.SENT_CONFIRMED, conversation };
    }
  }

  class AmbiguousSendRecovery {
    constructor({
      queueStore,
      sentPromptStore,
      sendStore,
      routingStore,
      router,
      inspectTab,
      reloadTab,
      emitStatus = () => false,
      resumeSession = async () => ({ ok: true }),
      sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay)),
      readyDelaysMs = DEFAULT_RECOVERY_READY_DELAYS_MS,
    }) {
      this.queueStore = queueStore;
      this.sentPromptStore = sentPromptStore;
      this.sendStore = sendStore;
      this.routingStore = routingStore;
      this.router = router;
      this.inspectTab = inspectTab;
      this.reloadTab = reloadTab;
      this.emitStatus = emitStatus;
      this.resumeSession = resumeSession;
      this.sleep = sleep;
      this.readyDelaysMs = readyDelaysMs;
      this.workers = new Map();
    }

    async _queueEntry(deliveryId) {
      const queue = await this.queueStore.list();
      return queue.find((entry) => entry.delivery_id === deliveryId) || null;
    }

    async _emit(result) {
      try {
        this.emitStatus(result.event);
      } catch {
        // Durable status outbox remains authoritative.
      }
      return result;
    }

    async _markIntervention(
      entry,
      errorCode,
      { observedConversation = null, bindingConflict = false } = {},
    ) {
      const cached = await this.routingStore.get(entry.session);
      const conflict = bindingConflict
        ? bindingConflictDetails({
            session: entry.session,
            expectedRouting: entry.routing,
            observedConversation:
              observedConversation ||
              (
                cached?.conversation_id
                  ? {
                      conversation_id: cached.conversation_id,
                      canonical_url: cached.canonical_url,
                    }
                  : null
              ),
            observedBindingVersion:
              Number.isInteger(cached?.binding_version)
                ? cached.binding_version
                : null,
          })
        : null;
      await this.sendStore.setRecoveryStatus(entry.delivery_id, {
        status: RECOVERY_STATUS.INTERVENTION_REQUIRED,
        errorCode,
        bindingConflict: conflict,
      });
      return {
        ok: false,
        state: SEND_STATE.AMBIGUOUS,
        error: errorCode,
        bindingConflict: conflict,
      };
    }

    async _targetFailure(entry, error) {
      const code = error?.code || preSendErrorCode(error);
      const bindingConflict = [
        "bound_target_changed",
        "binding_snapshot_conflict",
        "stale_binding_snapshot",
      ].includes(code);
      return this._markIntervention(
        entry,
        bindingConflict ? "CHATGPT_SEND_BINDING_CONFLICT" : code,
        {
          observedConversation: observedConversationFromRoutingError(error),
          bindingConflict,
        },
      );
    }

    async _inspect(tabId, text) {
      try {
        return await this.inspectTab(tabId, text);
      } catch (error) {
        return {
          ok: false,
          error: preSendErrorCode(error),
        };
      }
    }

    async _validateExpectedConversation(entry, tab, expectedConversation) {
      if (!expectedConversation) return null;
      const observedConversation = canonicalConversation(tab?.url);
      if (
        !observedConversation ||
        observedConversation.canonical_url === expectedConversation.canonical_url
      ) {
        return null;
      }
      const durableConflict = Boolean(entry.routing);
      return this._markIntervention(
        entry,
        durableConflict
          ? "CHATGPT_SEND_BINDING_CONFLICT"
          : "ambiguous_recovery_wrong_conversation",
        {
          observedConversation,
          bindingConflict: durableConflict,
        },
      );
    }

    async _resolveInspection(
      entry,
      tab,
      inspection,
      expectedConversation = null,
    ) {
      if (!inspection?.ok) {
        return {
          resolved: false,
          error: inspection?.error || "delivery_evidence_ambiguous",
        };
      }

      if (inspection.state === "NOT_SENT") {
        await this.sendStore.setRecoveryStatus(entry.delivery_id, {
          status: RECOVERY_STATUS.VERIFIED_NOT_SENT,
        });
        const resolved = await this.sendStore.resolveAmbiguous({
          deliveryId: entry.delivery_id,
          state: SEND_STATE.BLOCKED,
          errorCode: "verified_not_sent",
        });
        await this._emit(resolved);
        return {
          resolved: true,
          result: {
            ok: true,
            state: SEND_STATE.BLOCKED,
            evidence: "NOT_SENT",
          },
        };
      }

      if (inspection.state !== "SENT") {
        return { resolved: false, error: "delivery_evidence_ambiguous" };
      }

      const observedConversation =
        canonicalConversation(inspection.conversationUrl) ||
        canonicalConversation(tab?.url);
      if (!observedConversation) {
        return {
          resolved: true,
          result: await this._markIntervention(
            entry,
            "verified_send_missing_conversation_identity",
          ),
        };
      }
      if (
        expectedConversation &&
        observedConversation.canonical_url !== expectedConversation.canonical_url
      ) {
        const durableConflict = Boolean(entry.routing);
        return {
          resolved: true,
          result: await this._markIntervention(
            entry,
            durableConflict
              ? "CHATGPT_SEND_BINDING_CONFLICT"
              : "ambiguous_recovery_wrong_conversation",
            { observedConversation, bindingConflict: durableConflict },
          ),
        };
      }

      await this.sendStore.setRecoveryStatus(entry.delivery_id, {
        status: RECOVERY_STATUS.CONFIRMED,
      });
      const resolved = await this.sendStore.resolveAmbiguous({
        deliveryId: entry.delivery_id,
        state: SEND_STATE.SENT_CONFIRMED,
        conversation: observedConversation,
      });
      try {
        await this.sentPromptStore.recordSent({
          deliveryId: entry.delivery_id,
          session: entry.session,
          tabId: tab.id,
          conversationUrl: observedConversation.canonical_url,
        });
        await this.queueStore.remove(entry.delivery_id);
      } catch {
        // SENT_CONFIRMED is already durable. Never replay the send.
      }
      await this._emit(resolved);
      void Promise.resolve(this.resumeSession(entry.session));
      return {
        resolved: true,
        result: {
          ok: true,
          state: SEND_STATE.SENT_CONFIRMED,
          evidence: "SENT",
          conversation: observedConversation,
        },
      };
    }

    recover(deliveryId) {
      const existing = this.workers.get(deliveryId);
      if (existing) return existing;
      const worker = this._recover(deliveryId).finally(() => {
        this.workers.delete(deliveryId);
      });
      this.workers.set(deliveryId, worker);
      return worker;
    }

    async _recover(deliveryId) {
      const current = await this.sendStore.get(deliveryId);
      if (!current) {
        return { ok: false, state: null, error: "chatgpt_send_not_found" };
      }
      if (current.state === SEND_STATE.SENT_CONFIRMED) {
        return { ok: true, state: SEND_STATE.SENT_CONFIRMED };
      }
      if (current.state !== SEND_STATE.AMBIGUOUS) {
        return {
          ok: false,
          state: current.state,
          error: "chatgpt_send_not_ambiguous",
        };
      }

      const entry = await this._queueEntry(deliveryId);
      if (!entry) return { ok: false, error: "delivery_not_found:" + deliveryId };
      if (isArchitectureSession(entry.session)) {
        return {
          ok: false,
          state: SEND_STATE.AMBIGUOUS,
          error: "manual_arch_recovery_required",
        };
      }

      const expectedConversation = entry.routing
        ? {
            conversation_id: entry.routing.conversation_id,
            canonical_url: entry.routing.canonical_url,
          }
        : current.conversation;

      await this.sendStore.setRecoveryStatus(deliveryId, {
        status: RECOVERY_STATUS.INSPECTING,
      });

      let tab;
      try {
        tab = await this.router.recoveryTarget({
          session: entry.session,
          routing: entry.routing,
        });
      } catch (error) {
        return this._targetFailure(entry, error);
      }

      const targetConflict = await this._validateExpectedConversation(
        entry,
        tab,
        expectedConversation,
      );
      if (targetConflict) return targetConflict;

      let inspection = await this._inspect(tab.id, entry.text);
      let resolution = await this._resolveInspection(
        entry,
        tab,
        inspection,
        expectedConversation,
      );
      if (resolution.resolved) return resolution.result;

      const refreshedState = await this.sendStore.get(deliveryId);
      if (refreshedState?.recovery?.reload_attempted) {
        return this._markIntervention(
          entry,
          inspection?.error || "delivery_evidence_ambiguous",
        );
      }

      const reloadBarrier = await this.sendStore.markRecoveryReloadAttempted(deliveryId);
      if (!reloadBarrier.started) {
        return this._markIntervention(
          entry,
          inspection?.error || "delivery_evidence_ambiguous",
        );
      }

      try {
        await this.reloadTab(tab.id);
      } catch (error) {
        return this._markIntervention(
          entry,
          "recovery_reload_failed:" + preSendErrorCode(error),
        );
      }

      await this.sendStore.setRecoveryStatus(deliveryId, {
        status: RECOVERY_STATUS.WAITING_CONTENT,
      });

      let lastError = inspection?.error || "delivery_evidence_ambiguous";
      for (const delay of this.readyDelaysMs) {
        await this.sleep(delay);
        try {
          tab = await this.router.revalidateRecoveryTarget({
            session: entry.session,
            routing: entry.routing,
            tabId: tab.id,
          });
        } catch (error) {
          return this._targetFailure(entry, error);
        }

        const revalidatedConflict = await this._validateExpectedConversation(
          entry,
          tab,
          expectedConversation,
        );
        if (revalidatedConflict) return revalidatedConflict;

        inspection = await this._inspect(tab.id, entry.text);
        resolution = await this._resolveInspection(
          entry,
          tab,
          inspection,
          expectedConversation,
        );
        if (resolution.resolved) return resolution.result;
        lastError = inspection?.error || lastError;
      }

      return this._markIntervention(entry, lastError);
    }

    async verify(deliveryId, { tabId }) {
      const entry = await this._queueEntry(deliveryId);
      if (!entry) return { ok: false, error: "delivery_not_found:" + deliveryId };
      const current = await this.sendStore.get(deliveryId);
      if (!current || current.state !== SEND_STATE.AMBIGUOUS) {
        return { ok: false, error: "chatgpt_send_not_ambiguous" };
      }

      const expectedConversation = entry.routing
        ? {
            conversation_id: entry.routing.conversation_id,
            canonical_url: entry.routing.canonical_url,
          }
        : current.conversation;

      await this.sendStore.setRecoveryStatus(deliveryId, {
        status: RECOVERY_STATUS.INSPECTING,
      });

      let tab;
      try {
        tab = await this.router.revalidateRecoveryTarget({
          session: entry.session,
          routing: entry.routing,
          tabId,
        });
      } catch (error) {
        return this._targetFailure(entry, error);
      }

      const targetConflict = await this._validateExpectedConversation(
        entry,
        tab,
        expectedConversation,
      );
      if (targetConflict) return targetConflict;

      const inspection = await this._inspect(tab.id, entry.text);
      const resolution = await this._resolveInspection(
        entry,
        tab,
        inspection,
        expectedConversation,
      );
      if (resolution.resolved) return resolution.result;
      return this._markIntervention(
        entry,
        inspection?.error || "delivery_evidence_ambiguous",
      );
    }
  }

  namespace.send = {
    DEFAULT_RETRY_DELAYS_MS,
    DEFAULT_RECOVERY_READY_DELAYS_MS,
    preSendErrorCode,
    isArchitectureSession,
    isLegacySyntheticConversationId,
    isLegacySyntheticRouting,
    routingFromActiveConversation,
    canonicalConversation,
    shouldCloseManagedTabAfterAck,
    sentContextMatchesTab,
    bindingConflictDetails,
    PromptSendCoordinator,
    AmbiguousSendRecovery,
  };
})();
