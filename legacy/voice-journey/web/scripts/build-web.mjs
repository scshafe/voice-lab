import { buildWebApp } from "@scshafe/ui/build";
import path from "node:path";
import { fileURLToPath } from "node:url";

// S4: the esbuild wrapper + fail-loud @scshafe/ui preflight now live in @scshafe/ui/build —
// this script keeps only the app's paths. Output stays gitignored (web/dist);
// the corpus browser serves it request-time, so a rebuild goes live on reload.
const webRoot = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..");

await buildWebApp({
  entry: path.join(webRoot, "src", "main.jsx"),
  outfile: path.join(webRoot, "dist", "app.js"),
  resolveFrom: webRoot,
  banner: "/* Voice Journey web bundle. Source entry: web/src/main.jsx. */"
});
