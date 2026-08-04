import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const generator = path.resolve(
  projectRoot,
  "../stream-deck-profile/assets/generate_icons.py"
);
const pluginRoot = path.join(
  projectRoot,
  "com.yanndouchin.founderos-actions.sdPlugin"
);

const completed = spawnSync(
  process.env.PYTHON || "python3",
  [generator, "--plugin-only", "--plugin-root", pluginRoot],
  { cwd: projectRoot, encoding: "utf8", stdio: "inherit" }
);

if (completed.error) {
  throw completed.error;
}
if (completed.status !== 0) {
  process.exit(completed.status ?? 1);
}

console.log("FounderOS icons regenerated from the shared visual system.");
