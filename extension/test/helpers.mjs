import { readFile } from "node:fs/promises";
import { runInContext, createContext } from "node:vm";
import { fileURLToPath } from "node:url";
import path from "node:path";

const extensionRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

export async function loadClassicScripts(relativePaths, extraGlobals = {}) {
  const context = createContext({
    console,
    URL,
    Date,
    JSON,
    Object,
    Array,
    Set,
    String,
    Number,
    Error,
    Promise,
    ...extraGlobals,
  });
  context.globalThis = context;

  for (const relativePath of relativePaths) {
    const source = await readFile(path.join(extensionRoot, relativePath), "utf8");
    runInContext(source, context, { filename: relativePath });
  }

  return context;
}

export function createMemoryStorage(initial = {}) {
  const state = structuredClone(initial);
  return {
    state,
    failNextSet: false,
    async get(key) {
      return key in state ? { [key]: structuredClone(state[key]) } : {};
    },
    async set(values) {
      if (this.failNextSet) {
        this.failNextSet = false;
        throw new Error("storage_failed");
      }
      Object.assign(state, structuredClone(values));
    },
  };
}

export async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}
