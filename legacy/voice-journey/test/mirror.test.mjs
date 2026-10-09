import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { EXIT, isDropLocked, parseArgs, recordingsRsyncArgs, runMirror, stagingRsyncArgs, validateOptions } from "../src/mirror.mjs";
import { openCorpusFileForRead } from "../src/host-access.mjs";

const NAME_A = "20240101 101010-AAAA1111.m4a";
const NAME_B = "20240102 101010-BBBB2222.m4a";

async function fixture({ rsyncExit = 0, locked = false } = {}) {
  const root = await mkdtemp(path.join(tmpdir(), "vj-mirror-"));
  const corpus = path.join(root, "corpus");
  await mkdir(corpus);
  await writeFile(path.join(corpus, NAME_A), "aaa");
  await writeFile(path.join(corpus, NAME_B), "bbbb");
  await writeFile(path.join(corpus, "CloudRecordings.db"), "db");
  await writeFile(path.join(corpus, "unrelated.txt"), "nope");
  // Fake rsync: log argv (one arg per line) and the NUL list, optionally fail.
  const bin = path.join(root, "rsync-fake");
  await writeFile(
    bin,
    `#!/bin/sh
mkdir -p "${root}/calls"
n=$(ls "${root}/calls" | grep -c args)
for a in "$@"; do printf '%s\\n' "$a"; done > "${root}/calls/$n.args"
for a in "$@"; do case "$a" in --files-from=*) tr '\\0' '\\n' < "\${a#--files-from=}" > "${root}/calls/$n.list";; esac; done
echo "rsync: send_files failed to open \\"${NAME_A}\\"" >&2
${locked ? 'echo "rrsync error: Another instance of rrsync is already accessing this directory" >&2' : ""}
exit ${rsyncExit}
`,
  );
  await chmod(bin, 0o755);
  const argv = (extra = []) => [
    "--corpus-root", corpus, "--state-dir", path.join(root, "state"), "--dest", "me@host:./",
    "--rsync-bin", bin, "--approval", "test approval", ...extra,
  ];
  const logs = [];
  const run = (extra) => runMirror(argv(extra), { log: (line) => logs.push(line) });
  return { root, corpus, argv, logs, run, calls: () => readdir(path.join(root, "calls")).catch(() => []) };
}

test("rsync argument shape is write-only safe: no delete, no pull, relative dest, files-from", () => {
  const options = parseArgs(["--corpus-root", "/c", "--dest", "me@host:./", "--ssh-key", "/k"]);
  const args = recordingsRsyncArgs(options, "/tmp/list");
  assert.deepEqual(args.slice(-2), ["/c/", "me@host:./"]);
  assert.ok(args.includes("--files-from=/tmp/list") && args.includes("--from0"));
  assert.ok(args.includes("--partial-dir=.rsync-partial"));
  assert.ok(!args.some((a) => a.startsWith("--delete") || a === "--remove-source-files" || a === "-a"));
  assert.ok(!args.includes("--delay-updates"));
  assert.match(args[args.indexOf("-e") + 1], /BatchMode=yes.* -i \/k$/u);
  const staging = stagingRsyncArgs(options, "/s", "/tmp/l2");
  assert.deepEqual(staging.slice(-2), ["/s/", "me@host:./"]);
});

test("refuses a local destination and state inside the corpus", () => {
  assert.throws(() => validateOptions(parseArgs(["--dest", "/tmp/somewhere"])), /local destination/u);
  assert.throws(() => validateOptions(parseArgs(["--dest", "./rel"])), /local destination/u);
  assert.throws(
    () => validateOptions(parseArgs(["--corpus-root", "/c", "--state-dir", "/c/state", "--dest", "h:./"])),
    /inside the corpus/u,
  );
  validateOptions(parseArgs(["--dest", "me@host:./"]));
  validateOptions(parseArgs(["--dest", "rsync://host/mod"]));
});

test("the corpus seam only opens allowlisted files for reading", () => {
  assert.throws(() => openCorpusFileForRead("/c", "../etc/passwd"), /allowlist/u);
  assert.throws(() => openCorpusFileForRead("/c", "sub/a.m4a"), /allowlist/u);
  assert.throws(() => openCorpusFileForRead("/c", "notes.txt"), /allowlist/u);
});

test("dry run reads nothing and runs nothing", async () => {
  const f = await fixture();
  assert.equal(await f.run(["--dry-run"]), EXIT.ok);
  assert.deepEqual(await f.calls(), []);
  await assert.rejects(readdir(path.join(f.root, "state")));
  await rm(f.root, { recursive: true });
});

test("real run needs an approval", async () => {
  const f = await fixture();
  const code = await runMirror(["--corpus-root", f.corpus, "--state-dir", path.join(f.root, "state"), "--dest", "h:./"], { log: () => {} });
  assert.equal(code, EXIT.usage);
  await rm(f.root, { recursive: true });
});

test("run writes a verifiable manifest, sends recordings then manifest, never names in logs", async () => {
  const f = await fixture();
  assert.equal(await f.run(), EXIT.ok);
  assert.deepEqual((await f.calls()).filter((n) => n.endsWith(".list")).sort(), ["0.list", "1.list"]);
  const first = (await readFile(path.join(f.root, "calls/0.list"), "utf8")).split("\n").filter(Boolean);
  assert.deepEqual(first.sort(), [NAME_A, NAME_B]); // unrelated.txt and the db are not in pass 1
  const second = (await readFile(path.join(f.root, "calls/1.list"), "utf8")).split("\n").filter(Boolean);
  assert.equal(second[0], "CloudRecordings.db");
  assert.match(second[1], /^_mirror\/manifest-.*\.json$/u);
  const latest = JSON.parse(await readFile(path.join(f.root, "state/staging/_mirror/latest.json"), "utf8"));
  assert.equal(latest.recordingCount, 2);
  const a = latest.files.find((x) => x.path === NAME_A);
  assert.equal(a.sha256, "9834876dcfb05cb167a5c24953eba58c4ac89b1adf57f28f2f9d09af107ee8f0");
  assert.equal(a.sizeBytes, 3);
  assert.ok(latest.files.some((x) => x.path === "CloudRecordings.db" && x.snapshot));
  assert.ok((await readdir(path.join(f.root, "state/manifests"))).length === 1);
  const text = f.logs.join("\n");
  assert.ok(!text.includes("AAAA1111") && !text.includes(".m4a"));
  assert.ok(await readFile(path.join(f.corpus, NAME_A), "utf8")); // source untouched
  await rm(f.root, { recursive: true });
});

test("unchanged files are not rehashed; changed ones are", async () => {
  const f = await fixture();
  await f.run();
  f.logs.length = 0;
  await f.run();
  assert.match(f.logs[0], /0 hashed/u);
  await writeFile(path.join(f.corpus, NAME_A), "changed");
  await utimes(path.join(f.corpus, NAME_A), new Date(), new Date(Date.now() + 60000));
  f.logs.length = 0;
  await f.run();
  assert.match(f.logs[0], /1 hashed/u);
  await rm(f.root, { recursive: true });
});

test("a failed transfer exits 4, withholds the manifest pass, and leaks no names", async () => {
  const f = await fixture({ rsyncExit: 23 });
  assert.equal(await f.run(), EXIT.transfer);
  assert.equal((await f.calls()).filter((n) => n.endsWith(".args")).length, 1); // pass 2 never ran
  const text = f.logs.join("\n");
  assert.match(text, /rsync exited 23/u);
  assert.ok(!text.includes("AAAA1111"));
  await rm(f.root, { recursive: true });
});

test("lock: a live holder skips the run (exit 3); a stale pid is taken over", async () => {
  const f = await fixture();
  await mkdir(path.join(f.root, "state"), { recursive: true });
  const lock = path.join(f.root, "state/mirror.lock");
  await writeFile(lock, JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }));
  assert.equal(await f.run(), EXIT.locked);
  assert.deepEqual(await f.calls(), []);
  await writeFile(lock, JSON.stringify({ pid: 2 ** 22 + 12345, startedAt: new Date().toISOString() }));
  assert.equal(await f.run(), EXIT.ok);
  await assert.rejects(readFile(lock)); // released
  await rm(f.root, { recursive: true });
});

test("a missing corpus fails with exit 1 and does not call rsync", async () => {
  const f = await fixture();
  await rm(f.corpus, { recursive: true });
  assert.equal(await f.run(), EXIT.failure);
  assert.deepEqual(await f.calls(), []);
  await rm(f.root, { recursive: true });
});

test("defaults match the Lubuntu drop: vj-mirror account, Homebrew rsync, locked-down ssh, group-readable modes", () => {
  const saved = { dest: process.env.VOICE_JOURNEY_MIRROR_DEST, rsync: process.env.VOICE_JOURNEY_RSYNC };
  delete process.env.VOICE_JOURNEY_MIRROR_DEST;
  delete process.env.VOICE_JOURNEY_RSYNC;
  try {
    const options = parseArgs(["--ssh-key", "/k"]);
    assert.equal(options.dest, "vj-mirror@cole-lubuntu-laptop:");
    assert.equal(options.rsyncBin, "/opt/homebrew/bin/rsync");
    validateOptions(options);
    const args = recordingsRsyncArgs(options, "/l");
    assert.deepEqual(args.slice(-1), ["vj-mirror@cole-lubuntu-laptop:"]);
    assert.ok(args.includes("--chmod=D2750,F0640"));
    const ssh = args[args.indexOf("-e") + 1];
    assert.match(ssh, /-o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes/u);
    assert.match(ssh, /-i \/k$/u);
    assert.equal(parseArgs(["--rsync-bin", "/x/rsync"]).rsyncBin, "/x/rsync");
  } finally {
    if (saved.dest !== undefined) process.env.VOICE_JOURNEY_MIRROR_DEST = saved.dest;
    if (saved.rsync !== undefined) process.env.VOICE_JOURNEY_RSYNC = saved.rsync;
  }
});

test("rrsync's drop lock exits 3 (try next period), is not a transfer failure, and leaks no names", async () => {
  assert.ok(isDropLocked({ stderr: "rrsync error: Another instance of rrsync is already accessing this directory\n" }));
  assert.ok(!isDropLocked({ stderr: "rsync error: some files could not be transferred" }));
  const f = await fixture({ rsyncExit: 1, locked: true });
  assert.equal(await f.run(), EXIT.locked);
  assert.equal((await f.calls()).filter((n) => n.endsWith(".args")).length, 1); // pass 2 never ran
  const text = f.logs.join("\n");
  assert.match(text, /locked/u);
  assert.ok(!text.includes("AAAA1111"));
  await rm(f.root, { recursive: true });
});
