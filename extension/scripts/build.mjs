import { cp, mkdir, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const extensionRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const dist = path.join(extensionRoot, "dist");

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });

for (const entry of ["manifest.json", "src", "popup"]) {
  await cp(path.join(extensionRoot, entry), path.join(dist, entry), {
    recursive: true,
  });
}

console.log(`Built Firefox extension at ${dist}`);
