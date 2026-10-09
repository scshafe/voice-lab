import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const source = fileURLToPath(new URL("../scripts/voice-journey-launcher.c", import.meta.url));
const hasCc = spawnSync("cc", ["--version"]).status === 0;

// Compiles the launcher's mode resolver (main excluded) into a harness and
// checks the allowlist: no argument = browser, "mirror", and nothing else.
test("launcher mode allowlist", { skip: !hasCc && "no C compiler" }, async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "vj-launcher-"));
  const harness = path.join(dir, "harness.c");
  await writeFile(
    harness,
    `#define VJ_LAUNCHER_NO_MAIN 1
#include "${source}"
int main(int argc, char *argv[]) {
  const char *t = resolve_mode(argc, argv);
  puts(t ? t : "REFUSED");
  return 0;
}
`,
  );
  const exe = path.join(dir, "harness");
  execFileSync("cc", ["-Wall", "-Werror", "-o", exe, harness]);
  const resolve = (...args) => execFileSync(exe, args, { encoding: "utf8" }).trim();
  assert.match(resolve(), /voice-journey-browser-launchd$/u);
  assert.match(resolve("browser"), /voice-journey-browser-launchd$/u);
  assert.match(resolve("mirror"), /voice-journey-mirror-launchd$/u);
  for (const bad of [["/bin/sh"], ["-c", "id"], ["Mirror"], [""], ["mirror "], ["mirror", "--approval", "x"], ["browser", "mirror"], ["mirror\n"]]) {
    assert.equal(resolve(...bad), "REFUSED", JSON.stringify(bad));
  }
  await rm(dir, { recursive: true });
});

test("launcher binary refuses an unknown mode with exit 64 before spawning", { skip: !hasCc && "no C compiler" }, async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "vj-launcher-"));
  const exe = path.join(dir, "launcher");
  execFileSync("cc", ["-Wall", "-Werror", "-o", exe, source]);
  const result = spawnSync(exe, ["/bin/true"], { encoding: "utf8" });
  assert.equal(result.status, 64);
  assert.match(result.stderr, /usage/u);
  await rm(dir, { recursive: true });
});
