import { readFile, access } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const extensionRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

const manifestPath = path.join(extensionRoot, "manifest.json");
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));

if (manifest.manifest_version !== 2) {
  throw new Error("Firefox companion must use the selected Manifest V2 background model");
}
if (manifest.permissions.includes("<all_urls>")) {
  throw new Error("Extension must not request <all_urls>");
}
if (manifest.background?.persistent !== true) {
  throw new Error("WebSocket background context must remain persistent for DC-012");
}
if (
  !manifest.content_scripts?.some((item) =>
    item.matches?.includes("https://chatgpt.com/*"),
  )
) {
  throw new Error("ChatGPT content-script permission is missing");
}

const referencedFiles = new Set([
  ...(manifest.background?.scripts || []),
  manifest.browser_action?.default_popup,
  ...manifest.content_scripts.flatMap((item) => item.js || []),
]);

for (const relativePath of referencedFiles) {
  if (relativePath) {
    await access(path.join(extensionRoot, relativePath));
  }
}

const jsFiles = [
  "src/protocol.js",
  "src/queue-store.js",
  "src/response-store.js",
  "src/send-store.js",
  "src/routing-store.js",
  "src/router.js",
  "src/transport.js",
  "src/send-coordinator.js",
  "src/background.js",
  "src/chatgpt-page-adapter.js",
  "src/content.js",
  "popup/popup.js",
];

for (const relativePath of jsFiles) {
  const result = spawnSync(process.execPath, ["--check", relativePath], {
    cwd: extensionRoot,
    stdio: "inherit",
  });
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

console.log("Extension manifest and JavaScript checks passed.");
