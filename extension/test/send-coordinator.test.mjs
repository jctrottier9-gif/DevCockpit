import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts, settle } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const SECOND_DELIVERY_ID = "9fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const SESSION = "DevCockpit:DEV:DC-063B";

async function setup({
  commitResponse = {
    ok: true,
    conversationUrl: "https://chatgpt.com/c/new-conversation",
  },
  prepareResponse = {
    ok: true,
    baseline: { user_message_count: 0, expected_text: "send me" },
  },
  routeError = null,
  prepareErrors = [],
  retryDelaysMs = [],
  session = SESSION,
  routing = null,
  targetUrl = "https://chatgpt.com/",
} = {}) {
  const storage = createMemoryStorage();
  let counter = 0;
  const context = await loadClassicScripts(
    [
      "src/queue-store.js",
      "src/response-store.js",
      "src/send-store.js",
      "src/send-coordinator.js",
    ],
    {
      crypto: {
        randomUUID: () =>
          `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
      },
    },
  );
  const { QueueStore } = context.DevCockpitCompanion.queue;
  const { SentPromptStore } = context.DevCockpitCompanion.responses;
  const { ChatGptSendStore } = context.DevCockpitCompanion.sendStore;
  const { PromptSendCoordinator } = context.DevCockpitCompanion.send;

  const queueStore = new QueueStore(storage);
  const sentPromptStore = new SentPromptStore(storage);
  const sendStore = new ChatGptSendStore(storage, {
    uuid: () =>
      `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
  });
  await queueStore.acceptPrompt({
    deliveryId: DELIVERY_ID,
    session,
    text: "send me",
    routing,
  });
  await sendStore.ensureQueued({ deliveryId: DELIVERY_ID, session });

  const calls = [];
  const transitions = [];
  const wakeCalls = [];
  let routeCalls = 0;
  let revalidateCalls = 0;
  const remainingPrepareErrors = [...prepareErrors];
  const originalTransition = sendStore.transition.bind(sendStore);
  sendStore.transition = async (command) => {
    const result = await originalTransition(command);
    transitions.push(result.entry.state);
    return result;
  };

  const coordinator = new PromptSendCoordinator({
    queueStore,
    sentPromptStore,
    sendStore,
    router: {
      async route() {
        routeCalls += 1;
        if (routeError) throw new Error(routeError);
        return {
          kind: routing ? "BOUND" : "PROVISIONAL",
          tabId: 7,
          url: targetUrl,
        };
      },
      async revalidateTarget() {
        revalidateCalls += 1;
        return { id: 7, url: targetUrl };
      },
      async revalidateManualTarget({ tabId }) {
        revalidateCalls += 1;
        return { id: tabId, url: "https://chatgpt.com/" };
      },
    },
    sendToTab: async (_tabId, message) => {
      calls.push(message.type);
      if (message.type === "devcockpit_prepare_prompt") {
        if (remainingPrepareErrors.length > 0) {
          throw new Error(remainingPrepareErrors.shift());
        }
        return prepareResponse;
      }
      return commitResponse;
    },
    wakeTab: async (tabId) => {
      wakeCalls.push(tabId);
    },
    sleep: async () => {},
    retryDelaysMs,
  });

  return {
    coordinator,
    sendStore,
    queueStore,
    sentPromptStore,
    calls,
    transitions,
    wakeCalls,
    routingCounts: () => ({ routeCalls, revalidateCalls }),
  };
}

test("automatic send persists SEND_ARMED before exactly one DOM commit", async () => {
  const { coordinator, sendStore, queueStore, calls, transitions } = await setup();

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.ok, true);
  assert.deepEqual(calls, [
    "devcockpit_prepare_prompt",
    "devcockpit_commit_prepared_prompt",
  ]);
  assert.ok(
    transitions.indexOf("SEND_ARMED") <
      transitions.indexOf("SENT_CONFIRMED"),
  );
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "SENT_CONFIRMED");
  assert.equal((await queueStore.list()).length, 0);
});

test("post-barrier uncertainty becomes AMBIGUOUS and never auto-resends", async () => {
  const { coordinator, sendStore, calls } = await setup({
    commitResponse: {
      ok: false,
      error: "send_confirmation_timeout",
      ambiguous: true,
    },
  });

  const first = await coordinator.enqueue(DELIVERY_ID);
  const second = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(first.state, "AMBIGUOUS");
  assert.equal(second.state, "AMBIGUOUS");
  assert.equal(
    calls.filter((item) => item === "devcockpit_commit_prepared_prompt").length,
    1,
  );
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "AMBIGUOUS");
});

test("certain pre-barrier readiness failure is BLOCKED without DOM click", async () => {
  const { coordinator, sendStore, calls } = await setup({
    prepareResponse: { ok: false, error: "send_button_disabled" },
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.deepEqual(calls, ["devcockpit_prepare_prompt"]);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});

test("created-tab navigation timeout remains BLOCKED before SEND_ARMED", async () => {
  const { coordinator, sendStore, calls, transitions } = await setup({
    routeError: "created_tab_navigation_timeout",
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.equal(result.error, "created_tab_navigation_timeout");
  assert.deepEqual(calls, []);
  assert.equal(transitions.includes("SEND_ARMED"), false);
  assert.deepEqual(wakeCalls, [7]);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});

test("transient missing content script retries before SEND_ARMED and then succeeds", async () => {
  const {
    coordinator,
    sendStore,
    calls,
    transitions,
    routingCounts,
    wakeCalls,
  } = await setup({
    prepareErrors: [
      "Could not establish connection. Receiving end does not exist.",
    ],
    retryDelaysMs: [0],
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "SENT_CONFIRMED");
  assert.deepEqual(calls, [
    "devcockpit_prepare_prompt",
    "devcockpit_prepare_prompt",
    "devcockpit_commit_prepared_prompt",
  ]);
  assert.deepEqual(routingCounts(), {
    routeCalls: 1,
    revalidateCalls: 3,
  });
  assert.deepEqual(wakeCalls, [7]);
  assert.ok(transitions.includes("WAITING_READY"));
  assert.ok(
    transitions.indexOf("WAITING_READY") <
      transitions.indexOf("SEND_ARMED"),
  );
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "SENT_CONFIRMED");
});

test("missing content script exhausts bounded retries while still pre-SEND_ARMED", async () => {
  const { coordinator, sendStore, calls, transitions, wakeCalls } = await setup({
    prepareErrors: [
      "Could not establish connection. Receiving end does not exist.",
      "Could not establish connection. Receiving end does not exist.",
    ],
    retryDelaysMs: [0],
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.equal(result.error, "content_script_unavailable");
  assert.deepEqual(calls, [
    "devcockpit_prepare_prompt",
    "devcockpit_prepare_prompt",
  ]);
  assert.equal(transitions.includes("SEND_ARMED"), false);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});

test("unrelated tab messaging failure remains non-retryable", async () => {
  const { coordinator, sendStore, calls, transitions } = await setup({
    prepareErrors: ["Unexpected extension messaging failure"],
    retryDelaysMs: [0, 0],
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.equal(result.error, "Unexpected extension messaging failure");
  assert.deepEqual(calls, ["devcockpit_prepare_prompt"]);
  assert.equal(transitions.includes("WAITING_READY"), false);
  assert.equal(transitions.includes("SEND_ARMED"), false);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});


test("canonical conversation accepts nested ChatGPT project and workspace routes", async () => {
  const context = await loadClassicScripts(
    [
      "src/queue-store.js",
      "src/response-store.js",
      "src/send-store.js",
      "src/send-coordinator.js",
    ],
    {},
  );
  const { canonicalConversation } = context.DevCockpitCompanion.send;

  assert.equal(
    canonicalConversation("https://chatgpt.com/g/g-p-project/c/conv-123?model=auto#x").conversation_id,
    "conv-123",
  );
  assert.equal(
    canonicalConversation("https://chatgpt.com/g/g-p-project/c/conv-123?model=auto#x").canonical_url,
    "https://chatgpt.com/c/conv-123",
  );
  assert.equal(
    canonicalConversation("https://chatgpt.com/w/team/c/conv-456").canonical_url,
    "https://chatgpt.com/c/conv-456",
  );
  assert.equal(
    canonicalConversation("https://chatgpt.com/g/g-p-project/c/conv-123/extra"),
    null,
  );
});


test("managed tab cleanup waits for confirmed backend ack and an empty session queue", async () => {
  const context = await loadClassicScripts(
    ["src/send-store.js", "src/send-coordinator.js"],
    {},
  );
  const { shouldCloseManagedTabAfterAck } = context.DevCockpitCompanion.send;
  const sentContext = {
    delivery_id: DELIVERY_ID,
    session: SESSION,
    tab_id: 7,
    conversation_url: "https://chatgpt.com/c/conv-a",
  };
  const confirmedEvent = {
    event_id: "event-confirmed",
    delivery_id: DELIVERY_ID,
    session: SESSION,
    state: "SENT_CONFIRMED",
  };

  assert.equal(
    shouldCloseManagedTabAfterAck({
      event: confirmedEvent,
      sentContext,
      queuedPrompts: [],
    }),
    true,
  );
  assert.equal(
    shouldCloseManagedTabAfterAck({
      event: confirmedEvent,
      sentContext,
      queuedPrompts: [{ delivery_id: SECOND_DELIVERY_ID, session: SESSION }],
    }),
    false,
  );
  assert.equal(
    shouldCloseManagedTabAfterAck({
      event: { ...confirmedEvent, state: "SEND_ARMED" },
      sentContext,
      queuedPrompts: [],
    }),
    false,
  );
  assert.equal(
    shouldCloseManagedTabAfterAck({
      event: confirmedEvent,
      sentContext: { ...sentContext, session: "DevCockpit:ARCH:ASTRA-101" },
      queuedPrompts: [],
    }),
    false,
  );
});

test("response selection accepts a reopened exact conversation and rejects another one", async () => {
  const context = await loadClassicScripts(
    ["src/send-store.js", "src/send-coordinator.js"],
    {},
  );
  const { sentContextMatchesTab } = context.DevCockpitCompanion.send;
  const sentContext = {
    delivery_id: DELIVERY_ID,
    session: SESSION,
    tab_id: 7,
    conversation_url: "https://chatgpt.com/c/conv-a",
  };

  assert.equal(
    sentContextMatchesTab(sentContext, {
      id: 99,
      url: "https://chatgpt.com/g/g-p-project/c/conv-a?model=auto",
    }),
    true,
  );
  assert.equal(
    sentContextMatchesTab(sentContext, {
      id: 7,
      url: "https://chatgpt.com/c/conv-b",
    }),
    false,
  );
});

test("resumeSession wakes the next queued delivery after predecessor removal", async () => {
  const { coordinator, sendStore, queueStore, sentPromptStore } = await setup();

  await queueStore.acceptPrompt({
    deliveryId: SECOND_DELIVERY_ID,
    session: SESSION,
    text: "second prompt",
    routing: null,
  });
  await sendStore.ensureQueued({
    deliveryId: SECOND_DELIVERY_ID,
    session: SESSION,
  });

  await sendStore.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: "ROUTING",
    attempt: 1,
  });
  await sendStore.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: "SEND_ARMED",
    attempt: 1,
  });
  await sendStore.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: "SENT_CONFIRMED",
    attempt: 1,
    conversation: {
      conversation_id: "conv-a",
      canonical_url: "https://chatgpt.com/c/conv-a",
    },
  });
  await queueStore.remove(DELIVERY_ID);

  const result = await coordinator.resumeSession(SESSION);

  assert.equal(result.state, "SENT_CONFIRMED");
  assert.equal((await sendStore.get(SECOND_DELIVERY_ID)).state, "SENT_CONFIRMED");
  assert.equal((await sentPromptStore.get(SECOND_DELIVERY_ID)).delivery_id, SECOND_DELIVERY_ID);
  assert.equal((await queueStore.list()).length, 0);
});

test("resumeSession never skips a blocked session head", async () => {
  const { coordinator, sendStore, queueStore } = await setup();

  await queueStore.acceptPrompt({
    deliveryId: SECOND_DELIVERY_ID,
    session: SESSION,
    text: "second prompt",
    routing: null,
  });
  await sendStore.ensureQueued({
    deliveryId: SECOND_DELIVERY_ID,
    session: SESSION,
  });
  await sendStore.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: "BLOCKED",
    attempt: 1,
    errorCode: "manual_fix_required",
  });

  const result = await coordinator.resumeSession(SESSION);

  assert.equal(result.ok, false);
  assert.equal(result.state, "BLOCKED");
  assert.equal(result.error, "session_head_not_sendable");
  assert.equal((await sendStore.get(SECOND_DELIVERY_ID)).state, "QUEUED");
  assert.equal((await queueStore.list()).length, 2);
});

test("normal confirmed send automatically schedules the next delivery in the session", async () => {
  const { coordinator, sendStore, queueStore } = await setup();

  await queueStore.acceptPrompt({
    deliveryId: SECOND_DELIVERY_ID,
    session: SESSION,
    text: "second prompt",
    routing: null,
  });
  await sendStore.ensureQueued({
    deliveryId: SECOND_DELIVERY_ID,
    session: SESSION,
  });

  const first = await coordinator.enqueue(DELIVERY_ID);
  assert.equal(first.state, "SENT_CONFIRMED");

  for (let index = 0; index < 5; index += 1) {
    await settle();
    if ((await sendStore.get(SECOND_DELIVERY_ID)).state === "SENT_CONFIRMED") {
      break;
    }
  }

  assert.equal((await sendStore.get(SECOND_DELIVERY_ID)).state, "SENT_CONFIRMED");
  assert.equal((await queueStore.list()).length, 0);
});

test("multiple readiness retries keep exactly one routed target", async () => {
  const {
    coordinator,
    sendStore,
    routingCounts,
  } = await setup({
    prepareErrors: [
      "Could not establish connection. Receiving end does not exist.",
      "Could not establish connection. Receiving end does not exist.",
    ],
    retryDelaysMs: [0, 0],
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "SENT_CONFIRMED");
  assert.deepEqual(routingCounts(), {
    routeCalls: 1,
    revalidateCalls: 4,
  });
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "SENT_CONFIRMED");
});


test("explicit legacy repair adopts only a real active ChatGPT conversation", async () => {
  const context = await loadClassicScripts(
    [
      "src/queue-store.js",
      "src/response-store.js",
      "src/send-store.js",
      "src/send-coordinator.js",
    ],
    {},
  );
  const { routingFromActiveConversation } = context.DevCockpitCompanion.send;
  const legacy = {
    binding_version: 3,
    conversation_id: "local-chatgpt%3Alegacy-id",
    canonical_url: "https://chatgpt.com/c/local-chatgpt%3Alegacy-id",
  };

  const repaired = routingFromActiveConversation(
    legacy,
    "https://chatgpt.com/g/g-p-project/c/real-conversation?model=auto",
  );
  assert.equal(repaired.binding_version, 3);
  assert.equal(repaired.conversation_id, "real-conversation");
  assert.equal(
    repaired.canonical_url,
    "https://chatgpt.com/c/real-conversation",
  );

  assert.equal(
    routingFromActiveConversation(
      legacy,
      "https://chatgpt.com/g/g-p-project/project",
    ),
    null,
  );
  assert.equal(
    routingFromActiveConversation(
      {
        binding_version: 3,
        conversation_id: "already-real",
        canonical_url: "https://chatgpt.com/c/already-real",
      },
      "https://chatgpt.com/c/other",
    ),
    null,
  );
});


test("ARCH delivery never auto-sends and requires the manual companion action", async () => {
  const { coordinator, sendStore, calls, routingCounts } = await setup({
    session: "DevCockpit:ARCH:ASTRA-101",
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.ok, false);
  assert.equal(result.state, "QUEUED");
  assert.equal(result.error, "manual_arch_required");
  assert.deepEqual(calls, []);
  assert.deepEqual(routingCounts(), { routeCalls: 0, revalidateCalls: 0 });
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "QUEUED");
});

test("manual ARCH launch sends only to the explicitly selected active tab", async () => {
  const { coordinator, sendStore, queueStore, calls, routingCounts } = await setup({
    session: "DevCockpit:ARCH:ASTRA-101",
    commitResponse: {
      ok: true,
      conversationUrl: "https://chatgpt.com/c/astra-work-conversation",
    },
  });

  const result = await coordinator.enqueue(DELIVERY_ID, { manualTabId: 42 });

  assert.equal(result.ok, true);
  assert.equal(result.state, "SENT_CONFIRMED");
  assert.deepEqual(calls, [
    "devcockpit_prepare_prompt",
    "devcockpit_commit_prepared_prompt",
  ]);
  assert.deepEqual(routingCounts(), { routeCalls: 0, revalidateCalls: 2 });
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "SENT_CONFIRMED");
  assert.equal((await queueStore.list()).length, 0);
});

test("resumeSession never bypasses manual ARCH policy", async () => {
  const { coordinator, sendStore, calls } = await setup({
    session: "DevCockpit:ARCH:ASTRA-101",
  });

  const result = await coordinator.resumeSession("DevCockpit:ARCH:ASTRA-101");

  assert.equal(result.ok, false);
  assert.equal(result.state, "QUEUED");
  assert.equal(result.error, "manual_arch_required");
  assert.deepEqual(calls, []);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "QUEUED");
});

test("architecture session detection is exact and does not affect DEV or PO", async () => {
  const context = await loadClassicScripts(
    ["src/send-store.js", "src/send-coordinator.js"],
    {},
  );
  const { isArchitectureSession } = context.DevCockpitCompanion.send;

  assert.equal(isArchitectureSession("DevCockpit:ARCH:ASTRA-101"), true);
  assert.equal(isArchitectureSession("DevCockpit:DEV:DC-070D"), false);
  assert.equal(isArchitectureSession("DevCockpit:PO:DC-070D"), false);
  assert.equal(isArchitectureSession("DevCockpit:ARCHIVE:ASTRA-101"), false);
});


test("confirmed DOM evidence on a different durable conversation fails closed", async () => {
  const routing = {
    binding_version: 4,
    conversation_id: "conv-a",
    canonical_url: "https://chatgpt.com/c/conv-a",
  };
  const { coordinator, sendStore, queueStore, calls } = await setup({
    routing,
    targetUrl: routing.canonical_url,
    commitResponse: {
      ok: true,
      conversationUrl: "https://chatgpt.com/c/conv-b",
    },
  });

  const result = await coordinator.enqueue(DELIVERY_ID);
  const state = await sendStore.get(DELIVERY_ID);

  assert.equal(result.state, "AMBIGUOUS");
  assert.equal(result.error, "CHATGPT_SEND_BINDING_CONFLICT");
  assert.equal(state.state, "AMBIGUOUS");
  assert.equal(state.recovery.status, "INTERVENTION_REQUIRED");
  assert.equal(
    state.recovery.binding_conflict.expected_conversation_id,
    "conv-a",
  );
  assert.equal(
    state.recovery.binding_conflict.observed_conversation_id,
    "conv-b",
  );
  assert.equal((await queueStore.list()).length, 1);
  assert.equal(
    calls.filter((item) => item === "devcockpit_commit_prepared_prompt").length,
    1,
  );
});

async function setupAmbiguousRecovery({
  session = SESSION,
  routing = null,
  conversationUrl = "https://chatgpt.com/c/recovery-conversation",
  inspections = [
    { ok: false, error: "delivery_evidence_ambiguous:matching=0" },
    {
      ok: true,
      state: "SENT",
      conversationUrl: "https://chatgpt.com/c/recovery-conversation",
    },
  ],
  readyDelaysMs = [0],
  initialReloadError = null,
} = {}) {
  const storage = createMemoryStorage();
  let counter = 100;
  const context = await loadClassicScripts(
    [
      "src/queue-store.js",
      "src/response-store.js",
      "src/send-store.js",
      "src/send-coordinator.js",
    ],
    {
      crypto: {
        randomUUID: () =>
          `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
      },
    },
  );
  const { QueueStore } = context.DevCockpitCompanion.queue;
  const { SentPromptStore } = context.DevCockpitCompanion.responses;
  const { ChatGptSendStore, SEND_STATE } =
    context.DevCockpitCompanion.sendStore;
  const {
    AmbiguousSendRecovery,
    canonicalConversation,
  } = context.DevCockpitCompanion.send;

  const queueStore = new QueueStore(storage);
  const sentPromptStore = new SentPromptStore(storage);
  const sendStore = new ChatGptSendStore(storage, {
    uuid: () =>
      `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
  });
  await queueStore.acceptPrompt({
    deliveryId: DELIVERY_ID,
    session,
    text: "send me",
    routing,
  });
  await sendStore.ensureQueued({ deliveryId: DELIVERY_ID, session });
  await sendStore.transition({
    deliveryId: DELIVERY_ID,
    session,
    state: SEND_STATE.ROUTING,
    attempt: 1,
  });
  await sendStore.transition({
    deliveryId: DELIVERY_ID,
    session,
    state: SEND_STATE.SEND_ARMED,
    attempt: 1,
  });
  await sendStore.transition({
    deliveryId: DELIVERY_ID,
    session,
    state: SEND_STATE.AMBIGUOUS,
    attempt: 1,
    conversation: canonicalConversation(conversationUrl),
    errorCode: "send_confirmation_timeout",
  });

  let currentUrl = conversationUrl;
  let inspectionSequence = [...inspections];
  let inspectionIndex = 0;
  let reloadError = initialReloadError;
  let reloadCount = 0;
  let inspectCount = 0;
  let resumeCount = 0;

  const routingStore = {
    async get() {
      if (routing) {
        return {
          kind: "BOUND",
          tab_id: 7,
          binding_version: routing.binding_version,
          conversation_id: routing.conversation_id,
          canonical_url: routing.canonical_url,
        };
      }
      return {
        kind: "PROVISIONAL",
        tab_id: 7,
        binding_version: null,
        conversation_id: null,
        canonical_url: null,
      };
    },
  };
  const router = {
    async recoveryTarget() {
      return { id: 7, url: currentUrl };
    },
    async revalidateRecoveryTarget() {
      return { id: 7, url: currentUrl };
    },
  };
  const inspectTab = async () => {
    inspectCount += 1;
    const index = Math.min(
      inspectionIndex,
      Math.max(0, inspectionSequence.length - 1),
    );
    inspectionIndex += 1;
    const value = inspectionSequence[index];
    if (value instanceof Error) throw value;
    return value;
  };
  const reloadTab = async () => {
    reloadCount += 1;
    if (reloadError) throw reloadError;
  };
  const buildRecovery = () =>
    new AmbiguousSendRecovery({
      queueStore,
      sentPromptStore,
      sendStore,
      routingStore,
      router,
      inspectTab,
      reloadTab,
      resumeSession: async () => {
        resumeCount += 1;
        return { ok: true };
      },
      sleep: async () => {},
      readyDelaysMs,
    });

  return {
    queueStore,
    sentPromptStore,
    sendStore,
    buildRecovery,
    recovery: buildRecovery(),
    setInspections(next) {
      inspectionSequence = [...next];
      inspectionIndex = 0;
    },
    setReloadError(error) {
      reloadError = error;
    },
    setCurrentUrl(url) {
      currentUrl = url;
    },
    counts() {
      return { reloadCount, inspectCount, resumeCount };
    },
  };
}

test("ambiguous recovery confirms already-visible evidence without reload", async () => {
  const value = await setupAmbiguousRecovery({
    inspections: [
      {
        ok: true,
        state: "SENT",
        conversationUrl: "https://chatgpt.com/c/recovery-conversation",
      },
    ],
  });

  const result = await value.recovery.recover(DELIVERY_ID);

  assert.equal(result.state, "SENT_CONFIRMED");
  assert.equal(value.counts().reloadCount, 0);
  assert.equal((await value.sendStore.get(DELIVERY_ID)).state, "SENT_CONFIRMED");
  assert.equal((await value.queueStore.list()).length, 0);
});

test("ambiguous recovery reloads at most once then confirms exact prompt", async () => {
  const value = await setupAmbiguousRecovery();

  const first = await value.recovery.recover(DELIVERY_ID);
  const second = await value.recovery.recover(DELIVERY_ID);

  assert.equal(first.state, "SENT_CONFIRMED");
  assert.equal(second.state, "SENT_CONFIRMED");
  assert.equal(value.counts().reloadCount, 1);
  assert.equal(value.counts().inspectCount, 2);
  assert.equal(
    (await value.sendStore.get(DELIVERY_ID)).recovery.status,
    "CONFIRMED",
  );
});

test("ambiguous recovery survives restart after reload barrier without a second reload", async () => {
  const value = await setupAmbiguousRecovery({
    inspections: [
      { ok: false, error: "delivery_evidence_ambiguous:matching=0" },
    ],
    initialReloadError: new Error("background_restarted"),
  });

  const interrupted = await value.recovery.recover(DELIVERY_ID);
  assert.equal(interrupted.state, "AMBIGUOUS");
  assert.equal(value.counts().reloadCount, 1);
  assert.equal(
    (await value.sendStore.get(DELIVERY_ID)).recovery.reload_attempted,
    true,
  );

  value.setReloadError(null);
  value.setInspections([
    {
      ok: true,
      state: "SENT",
      conversationUrl: "https://chatgpt.com/c/recovery-conversation",
    },
  ]);
  const restarted = value.buildRecovery();
  const recovered = await restarted.recover(DELIVERY_ID);

  assert.equal(recovered.state, "SENT_CONFIRMED");
  assert.equal(value.counts().reloadCount, 1);
});

test("content script not ready stays ambiguous after bounded single reload", async () => {
  const missing = new Error(
    "Could not establish connection. Receiving end does not exist.",
  );
  const value = await setupAmbiguousRecovery({
    inspections: [missing],
    readyDelaysMs: [0, 0],
  });

  const result = await value.recovery.recover(DELIVERY_ID);
  const state = await value.sendStore.get(DELIVERY_ID);

  assert.equal(result.state, "AMBIGUOUS");
  assert.equal(value.counts().reloadCount, 1);
  assert.equal(value.counts().inspectCount, 3);
  assert.equal(state.recovery.status, "INTERVENTION_REQUIRED");
  assert.equal(state.recovery.last_error, "content_script_unavailable");
});

test("multiple matching turns remain ambiguous after the single recovery reload", async () => {
  const value = await setupAmbiguousRecovery({
    inspections: [
      { ok: false, error: "delivery_evidence_ambiguous:matching=2" },
    ],
    readyDelaysMs: [0],
  });

  const result = await value.recovery.recover(DELIVERY_ID);
  const state = await value.sendStore.get(DELIVERY_ID);

  assert.equal(result.state, "AMBIGUOUS");
  assert.equal(value.counts().reloadCount, 1);
  assert.equal(state.recovery.status, "INTERVENTION_REQUIRED");
  assert.match(state.recovery.last_error, /matching=2/);
});

test("NOT_SENT proof is distinct and never triggers recovery reload", async () => {
  const value = await setupAmbiguousRecovery({
    inspections: [{ ok: true, state: "NOT_SENT" }],
  });

  const result = await value.recovery.recover(DELIVERY_ID);
  const state = await value.sendStore.get(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.equal(result.evidence, "NOT_SENT");
  assert.equal(value.counts().reloadCount, 0);
  assert.equal(state.recovery.status, "VERIFIED_NOT_SENT");
  assert.equal(state.error_code, "verified_not_sent");
});

test("different conversation evidence never confirms a durable binding", async () => {
  const routing = {
    binding_version: 4,
    conversation_id: "conv-a",
    canonical_url: "https://chatgpt.com/c/conv-a",
  };
  const value = await setupAmbiguousRecovery({
    routing,
    conversationUrl: routing.canonical_url,
    inspections: [
      {
        ok: true,
        state: "SENT",
        conversationUrl: "https://chatgpt.com/c/conv-b",
      },
    ],
  });

  const result = await value.recovery.recover(DELIVERY_ID);
  const state = await value.sendStore.get(DELIVERY_ID);

  assert.equal(result.state, "AMBIGUOUS");
  assert.equal(result.error, "CHATGPT_SEND_BINDING_CONFLICT");
  assert.equal(value.counts().reloadCount, 0);
  assert.equal(state.recovery.status, "INTERVENTION_REQUIRED");
  assert.equal(
    state.recovery.binding_conflict.expected_conversation_id,
    "conv-a",
  );
  assert.equal(
    state.recovery.binding_conflict.observed_conversation_id,
    "conv-b",
  );
  assert.equal(state.recovery.binding_conflict.expected_binding_version, 4);
  assert.equal(state.recovery.binding_conflict.observed_binding_version, 4);
});

test("automatic ambiguous recovery never runs for ARCH sessions", async () => {
  const value = await setupAmbiguousRecovery({
    session: "DevCockpit:ARCH:ASTRA-072B",
  });

  const result = await value.recovery.recover(DELIVERY_ID);

  assert.equal(result.state, "AMBIGUOUS");
  assert.equal(result.error, "manual_arch_recovery_required");
  assert.deepEqual(value.counts(), {
    reloadCount: 0,
    inspectCount: 0,
    resumeCount: 0,
  });
});
