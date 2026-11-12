# voice-lab: project review and enrichment ideas, November 2026

Status: **proposed; do not merge.** A review and a list of ideas for the owner to pick from.
Nothing here is a decision, and no code, recording, training run or production data was
touched to write it. Written 2026-11-09 against `main` at `b8d36c5`, by the project's agent
running on Claude Fable 5.2 at the owner's request.

## 1. The short version

voice-lab is a carefully engineered lab whose parts are each well built, and whose central
loop is not yet closed. The owner can record in the studio, takes reach the PC, and the PC
answers speech jobs for parley. But the PC cannot fine-tune, and no route brings a run
trained on the Mac to the PC. So, as far as the repository can tell, **the speech API speaks
with F5-TTS's public base voice, not with the owner's voice**
(`DECISIONS.md:2125-2132`, `docs/design/VOICE-PLATFORM.md:1162`). Everything else in this
review is secondary to that.

The five findings that matter most:

1. **The loop "record, train, speak in my own voice" stops at the PC's door.** Phase 5's
   core (a fine-tune job on the PC that trains on the PC's corpus) is unbuilt and, under
   D-051, waits for the Recording v1 sign-off. Most of the corpus is also still to record:
   about 1,850 prompts at the last documented count
   (`docs/research/2026-10-30-recording-ux-review.md:1`).
2. **Nothing can say whether a trained voice is good.** A finished run becomes the speaking
   voice on integrity checks alone (`src/cole_voice_lab/speech/release.py:366-450`;
   `docs/SPEECH_API.md:128` says so plainly). Blind listening, the project's stated primary
   signal, has had no input path since the bake-off was retired (`DECISIONS.md:2120-2121`).
   No automatic metric exists.
3. **The owner's side is weeks behind the agent's side.** Every step set since October
   landed, verified and deployed. None of the studio work since #121 has been checked on the
   real Mac, the parity sign-off is not recorded, and three draft PRs have waited since
   2026-10-10 for one real UMIK-1 session. The gates are scattered over five documents.
4. **The backup is weaker than its design.** One vault destination on the same site, no
   restore drill of real data, no alert when the nightly copy fails (it has already stopped
   once), and a recovery sheet that the logs still list as lying beside the key it recovers
   (`docs/design/PHASE-3-LOG.md:252`).
5. **The entry-point documents describe a system that no longer runs.** `CURRENT.md`,
   `README.md`, `ARCHITECTURE.md`, `SECURITY.md` and AGENTS.md's mission line still describe
   the Mac mini, a vault that "is not deployed", and the September desktop app. The decision
   log and the phase logs are accurate; the pages a reader opens first are not.

The five ideas ranked highest (section 4 has all of them, with effort and risks):

1. **Close the loop on the PC**: a fine-tune job in the GPU slot, reading the PC's corpus,
   feeding the automatic voice release. Until then, a small supported way to bring one Mac
   run to the PC.
2. **An evaluation bench for fine-tuned runs**: a fixed held-out set, automatic diagnostics
   (two speech recognizers, speaker similarity against real held-out takes), a blind A/B of
   run N against run N-1, and a screen before a run goes live.
3. **A conversational pack for parley**: spoken-text normalization and an abbreviation-safe
   splitter published as a contract, a phrase cache built on the renders' determinism, an
   interactive lane ahead of batch work, and compressed audio.
4. **Make the corpus fit its use**: an "assistant register" prompt pack, the owner's
   technical vocabulary from lexinet, and a transcript check of every take with the speech
   recognizer the repository already has.
5. **A second model family, spiked before it is built**: VoxCPM2 for fidelity and LoRA
   fine-tuning, Qwen3-TTS for low latency, both under the family ports D-048 already
   provides.

### Grades

| Area | Grade | One line |
| --- | --- | --- |
| Speech API | B+ | Careful queue, ledger, webhooks and auth; batch-grade latency and no text handling |
| Voice release | B- | Sound checksum-bound chain; promotion on integrity alone |
| Fine-tuning pipeline | B- | Reproducible and recorded; Mac-only, one 3,500-line module, no validation signal |
| Model-family breadth | D | Clean ports, one family registered |
| Evaluation | D | The primary signal has no input; no automatic metric |
| Studio recording loop | B+ | Everything planned is built and tested offscreen; nothing confirmed on the Mac |
| Desktop app overall | B- | Coherent after D-059; setup is multi-step, Speak and fine-tune live only in Swift |
| Capture robustness | A- | Continuity evidence, fail-closed saves, durable outbox |
| Legacy cleanup | D+ | Five superseded capture paths still tracked and verified on every run |
| House-panel capture | B | Server side complete and inert; no client has sent a real take |
| Corpus store | B+ | Rigorous integrity, enforced ports; stale migration manifest |
| Intake and sync | A- | Defense in depth; the API server's missing timeouts are the gap |
| Vault and durability | C | Excellent design; one same-site copy, no real drill, no alerting |
| Deploy lane | B+ | Verify, deploy, health check, rollback; 40 green deploys in a row |
| Security posture | B+ | Layered auth, least-privilege units, pinned images; stale SECURITY.md |
| Journey integration | B- | Faithful port, live for reads; cutover half-done for a month |
| Tests and verify | B+ | Broad and strict; the desktop suite can hang for days with nothing to stop it (3.9) |
| Plan execution | B | Agent-side plans land; real-hardware acceptance and phase 5 trail |
| Documentation accuracy | C | Logs and decisions true; entry points stale |
| Documentation navigability | D+ | No single current-state page, no single gate list |

## 2. How this was done, and how far to trust it

Six read-only reviewers (subagents of this session) each read one slice in full: plans and
documents against the Git history; speech, release, fine-tuning and evaluation; the capture
clients; storage, vault, deploy and security; the other projects' public repositories
(parley, embedded-house-panel, lexinet, voice-journey); and the open-weight model landscape
on the web. I read the top-level documents and the decisions the ideas rest on, collected the
size and CI numbers, ran `scripts/verify` on `main`, and checked the claims this document leads with
against the files myself (the citations in section 1, the release checks, the sentence
splitter, the digest cache, the missing exception hook, the vault units, the API server
class).

Limits:

- **The repository cannot show what is on the PC's disks.** Whether a voice release exists,
  how many takes are on the PC, whether the nightly vault copy is healthy today, and whether
  the recovery sheet was since stored offline are not determinable from here. Where a
  finding depends on that, it says "as far as the repository can tell".
- **Nothing was run except verify.** Risks found by reading (for example the studio staying
  busy after an unexpected exception) were not reproduced.
- **The model landscape is web research of 2026-11-09**, much of it newer than my training
  data. Vendor numbers were measured on hardware unlike the PC's, and no model was run. It
  is a list of candidates to spike, not of recommendations. Sources are in section 6.
- Line numbers are those of `b8d36c5`.

## 3. Review

### 3.1 Where the project stands against its plans

| Plan | Status | What is left |
| --- | --- | --- |
| Platform phases 0 and 1 | Done | |
| Phase 2: PC backend, intake, laptop vault | Done | Owner: one real session from the Air with its restore drill; store the recovery sheet (`docs/design/PHASE-2-LOG.md:27`, `:356`) |
| Phase 3: voice-journey ported and served | Code done, runbooks A to E run | Owner: a real UMIK-1 session, cutover F, the Air (H) (`docs/design/PHASE-3-LOG.md:242-252`) |
| Phase 4: desktop app, measurement, web view | Built | Checklist sign-off, measurement acceptance on macOS and Linux, the phone check of the view; the unversioned paths are still served (`src/cole_voice_lab/api/journey_routes.py:148`) |
| Recording v1, steps 1 to 9 | Done (S-1 on macOS only) | Linux S-1 (`DECISIONS.md:1145`) |
| Recording v1, step 10: parity sign-off, Swift retired | Gated on the owner | See 3.4 |
| Recording loop (D-058) | 12 of 12 built | "What the Mac must check" has no results (`docs/design/RECORDING-LOOP.md:59-157`) |
| Info tips (D-059) | 11 of 11 built | The Mac must build the Swift change of #147; six scshafe-qt asks go through the owner (`docs/design/INFO-TIPS.md:231-237`) |
| House panel (D-056), this repository's side | Done | The panel's own app and enrolment, outside this repository |
| Phase 5: models on the PC | Half-built | Built: speech API, voice release, family ports, benchmark, ROCm lock. Not built: `/api/v1/jobs`, a PC fine-tune job, corpus selection on the PC, the Models, Blind review and Voice release screens, spikes S-3 to S-5 |
| Phase 6: publish, rename, retire | Out of order | The repository is already public; the rename, the Swift removal and the Mini files' removal are not done, and no decision records the publication |
| Drafts #31, #37, #40 | Gated on the owner | #31 waits for a real UMIK-1 session accepted on the PC, #37 for #31, #40 for the checklist sign-off and cutover F |

Two things stand out. First, phase 5 is where the project's purpose lives, and it is the
least built. Second, the phases were written as a sequence, but the work has been ordered by
what an agent can finish without the owner: twelve recording-loop PRs and thirteen info-tip
PRs landed while the one sitting that unlocks three drafts and the sign-off has not happened.
That is not a criticism of either side. It does mean the next unit of value is mostly the
owner's hour at the Mac and the PC, not another agent increment.

### 3.2 What blocks value today

In the order that unlocks the most:

1. **Training on the PC does not exist.** The PC's corpus selection reads a recording
   library that only the Mac has, the PC has no fine-tune job, and it has no interactive
   Speak (`DECISIONS.md:2125-2132`). A run finished on the Mac has no documented way to the
   PC's `derived/finetune/`: `finetune restore-run` restores a run that was archived and
   removed locally (`docs/FINETUNE.md:321`), which is another job. The automatic release
   (D-055) is therefore waiting for a run that cannot arrive.
2. **The corpus is small.** About 1,850 prompts and two and a quarter hours of reading were
   still to record at the last count. A fine-tune of the full corpus needs the owner at the
   microphone, in a studio whose recent work he has not yet seen on the Mac.
3. **The Mac build and checks of #121 to #154, then the parity sign-off.** Under D-051 the
   sign-off is also what releases phase 5.
4. **The Mac's existing takes are not recorded as imported to the PC**
   (`corpus-import plan|approve|send`, `docs/design/RECORDING-V1.md:225-270`).
5. **Speak and fine-tune exist only in the Swift app.** The Qt app has no Speak, Models or
   Voice release screen (its screens are corpus, journey, practice, prompts, referee, status,
   studio and uploads). "Retire the Swift recorder at parity" cannot mean uninstalling it
   until those exist somewhere else. Recording v1's parity list does not cover this.

### 3.3 Speech, release, fine-tuning and evaluation

**Speech API (B+).** What the documents promise is in the code: submit, status, cancel,
joined and per-segment audio with ranges (`src/cole_voice_lab/speech/routes.py:224-362`),
sentence events, signed webhooks pinned to tailnet addresses
(`src/cole_voice_lab/speech/webhooks.py:121-183`), retention sweeps, a write-once render
ledger holding the text's digest only (`src/cole_voice_lab/speech/runner.py:670-715`), the
GPU slot and crash recovery. Caller authentication is solid (RS256 only, issuer, subject and
audience against a registry). The weaknesses are about use, not correctness:

- **Latency.** At the default 32 flow steps a 15-word sentence takes about 6 s
  (`docs/SPEECH_API.md:650-656`), plus a 0.25 s runner poll, a 0.2 s event poll and about
  20 s of cold start after 15 idle minutes. That is listening speed, not conversation speed.
- **No text handling.** Raw text goes to the model: numbers, abbreviations, URLs, Markdown
  and emoji are the model's problem. The splitter breaks after any terminal followed by white
  space once a fragment is 12 characters long (`src/cole_voice_lab/speech/segments.py:26-31`),
  so "…talk to Dr. Smith" splits after "Dr." No test covers an abbreviation. The documented
  CJK handling does not split unspaced text (`docs/SPEECH_API.md:451-452` against the same
  lines).
- **One queue.** First in, first out, no priority and no fairness: a 2,000-character job
  holds a short reply for minutes. A job caught by a restart ends failed and is not retried,
  and the same idempotency key then returns that failure for 30 days
  (`src/cole_voice_lab/speech/store.py:457-475`).
- **Work per pass grows with history.** Each 0.25 s pass scans every job record more than
  once (`src/cole_voice_lab/speech/runner.py:396-419`), and records live 30 days. Small
  today; it grows with use.
- **The HTTP server has no request timeout and no thread cap**
  (`src/cole_voice_lab/api/server.py:89-92`), unlike the vault server, which reads against
  deadlines. Behind the door on a tailnet this is an availability risk, not an exposure.
- **`ogg-opus` is in the schema and always refused**
  (`src/cole_voice_lab/speech/routes.py:283`). Parley ships about 1.4 MB of WAV per 30 s to a
  phone.
- **Drift.** "Re-hashed before every render" is, after the first job, a check of the file's
  identity (device, inode, size, times) against a cached digest
  (`src/cole_voice_lab/finetune.py:1201-1218`). That is a reasonable design, but it is not
  what `docs/SPEECH_API.md:330` says. The queue markers the document says the runner looks
  for are written and removed but never listed.

**Voice release (B-).** The append-only, checksum-bound chain and the owner's pin are right.
Three concerns:

- **Promotion is automatic on integrity alone.** Finished, not archived, the pinned subject,
  files matching their records. No audio is examined, not even that the listening samples
  exist. The owner chose this knowingly (D-055: "a bad run is live until a newer one replaces
  it"). It deserves a second look once runs are frequent, because the first to hear a bad
  voice would be a parley conversation.
- **Ordering by clocks.** A run is released only when it finished after the newest release
  was written (`src/cole_voice_lab/speech/release.py:422-426`, `:502-506`), and the scan
  stops at the first older run. A run that finishes on another machine and arrives later (the
  only way a run can arrive today) can be skipped for good.
- **The subject pin is a label in `run.json`.** Whoever can write `derived/finetune/` chooses
  the voice. On one owner's PC that is acceptable; it is worth saying in SECURITY.md.

**Fine-tuning (B-).** The pipeline is reproducible and thoroughly recorded.
`src/cole_voice_lab/finetune.py` is 3,509 lines holding contracts, dataset building, runtime
provisioning, training, Speak, and archive together. A run records no loss and no validation
metric. It runs only on the Mac, on a runtime kept alive for exactly that reason.

**Model families (D).** The ports are real and the comparison record is thoughtful.
One family is registered in both registries (`src/cole_voice_lab/speech/families.py:100-103`).
The candidate table in `docs/MODEL_FAMILIES.md:194-211` is marked unverified and is now a
month stale in a fast field (section 4.4).

**Evaluation (D).** What exists: three fixed listening sentences per run, compared by ear and
not blind; `families record` and `families compare` for training cost and inference numbers
(`src/cole_voice_lab/model_comparison.py:351-472`); and a complete blind-review builder that
only accepts audition runs, which nothing writes any more. So the invariant "blind listening
is the primary decision signal" is, for the runs that will speak, currently unreachable. No
speech-recognition check, speaker similarity or quality predictor exists anywhere. This is
the largest gap after the loop itself, and it is the cheaper of the two to close.

**Dead weight left by retired features.** `audition.py` (778 lines) validates records nothing
writes; `references.py` and `datasets.py` (about 1,500 lines) have no live consumer beyond
the CLI; `docs/REFERENCES_AND_DATASETS.md` still presents zero-shot reference publishing and
hosted-transfer approvals as current, against D-057 and D-028.

### 3.4 The recording clients

**Nine capture paths are in the tree.** The live one is the Qt prompt studio. Beside it: the
Qt app's older session flow, the measurement ritual (live, for singing), the Swift recorder
(the reference, and the only home of Speak and fine-tune), the legacy Qt Widgets desktop
(`desktop_window.py`, 2,410 lines, still described as current by `docs/DESKTOP.md`), the
terminal `capture run`, the browser capture console, voice-journey's Node capture
(`legacy/`), and the house panel's contract. Three of them (the Widgets desktop, the browser
console, the Node capture) are removable in principle today: about 5,000 lines of source and
a share of 8,800 lines of tests that every verify and every deploy still runs. The Swift
stack is about 24,000 lines of Swift and up to 50,000 lines of Python service, scripts and
tests; it cannot go at the sign-off, for the reason in 3.2 item 5.

**The studio (B+).** All twelve recording-loop steps and all nine items of the October
review are in the code, with a rapid-fire acceptance test. What stands between the owner and
the sign-off is on the Mac:

- the eleven check groups of `docs/design/RECORDING-LOOP.md` and D-059's seven, none recorded
  as run;
- quality-flag thresholds that have never been calibrated on the UMIK-1;
- the first team-signed Swift build (D-054 says it has not been done), needed because #147
  changed the reference app that "is not changed";
- VoiceOver and Full Keyboard Access, which are unverified (several screens set no
  accessibility properties of their own; the scshafe-qt components may supply some);
- stop-to-review timing with the 300 ms tail, measured at p95 294 ms against a 300 ms limit
  before the tail was added.

**Rough edges, verified by reading:**

- **An unexpected exception can strand the studio.** The app installs no `sys.excepthook`,
  and about twenty completion callbacks re-raise errors of an unexpected type. In `_save` the
  phase is already "saving", so such an error leaves the studio busy with no message until
  relaunch, and a frozen app shows no traceback. Not reproduced.
- **Save & Next reads the take at least four times and stores it twice**
  (`apps/desktop/src/voice_lab_desktop/corpus/spool.py:555`, then `build_take_package` in
  `src/cole_voice_lab/corpus_takes.py`). The roughly one second for a 13 s take is from an
  earlier session's note.
- **The take spool on the Mac is never pruned**, and it is the double-stored copy.
- **Every Redo uploads the rejected attempt** to the PC
  (`apps/desktop/src/voice_lab_desktop/screens/studio.py:1385-1392`). Whether it should is an
  open question in RECORDING-LOOP.md.
- **Stale or internal wording**: "Uploads off until S-1 passes (corpus_sync)"
  (`apps/desktop/src/voice_lab_desktop/studio/status.py:41`) after S-1 passed; "session" in
  one status line.
- **Keep-selected writes on the GUI thread**; undo is lost on relaunch; one status slot can
  overwrite a background failure with the next message.
- **Setup is long**: a config key, an approval that needs Tailscale and a tag, and three
  "libraries" on one Mac, which already caused the silently queued uploads of 2026-10-14.

**Architecture.** The known PySide collection hang is unfixed and recurred on 2026-10-31;
closures over models are still handed to the task pool. The app imports about 27 server
modules directly, so a stale Mac bundle against a newer PC is format skew, handled only by a
notice. There is no update channel: every fix needs a manual rebuild on the Mac, which is
why the Mac checks lag. The desktop does not consume `contracts/corpus-client.v1`; the
bundle is checked against the server only.

**House panel (B).** This repository's side is complete: the v2 take contract with ALSA
evidence, per-profile views, the scoped audience, the contract bundle and the fixture
server. No panel take has exercised it on real hardware; that waits on the panel project.

### 3.5 Storage, intake, vault and durability

**Corpus store (B+) and intake (A-).** The ports are enforced by a real import-boundary test
(`tests/test_storage_import_boundary.py`). Intake refuses links and odd files, copies to
fresh inodes, publishes by atomic rename, and the publisher unit has no network. I found
nothing weaker than documented. Leftovers: `migrations/active/corpus-store.md:12` has said
`migrating` since September, and `docs/PROMPT_CATALOG_V2.md:3` is still "draft, not
adversarially reviewed".

**Vault and durability (C).** The design is excellent and the top-level documents undersell
it: they say no vault is deployed, while `docs/design/PHASE-2-LOG.md:195-207` records the
PC's library bound to the laptop destination since 2026-10-06 with a nightly timer. The
grade is for what protects the recordings in practice:

- **Key recovery.** The library key is a systemd credential on a PC whose root disk is not
  encrypted, and the recovery sheet was left on that PC for the owner to store offline and
  shred. `docs/design/PHASE-3-LOG.md:252` still lists it open. If that is still so, losing
  the PC makes the laptop's ciphertext unreadable. This is ten minutes of the owner's time
  and the most valuable ten minutes in this document.
- **No alert on failure.** The vault units have no `OnFailure=`, and nothing runs
  `vault staleness --check`. A push fails closed on any unclassified path, and that has
  already stopped the nightly copy once (`docs/design/PHASE-3-LOG.md:226-231`). A silent gap
  of weeks is plausible.
- **No real restore drill.** One drill is recorded, on synthetic data.
- **No offsite copy** until the Air vault is installed (accepted in D-023, still open).
- **Not covered at all:** the journey verdicts and goals, which are human judgements
  (`docs/design/PHASE-3-LOG.md:254-256`).
- **Hand-installed units.** The vault and speech units live in `deploy/host-manual/`, so a
  change to them does not deploy.

**Size against use.** The storage and library modules total about 14,000 lines
(`library_generations.py` alone is 3,309) and serve one destination holding a small library.
The safety logic is correct as far as I can read it, and it sits in modules too long for one
session to hold. That is a maintenance risk, not a defect.

### 3.6 Deploy and security

**Deploy (B+).** Verify on the PC runner, deploy through the host entrypoint, health check,
rollback of the API service. The last 40 deploys are green; over 123 runs, 107 succeeded, 9
failed and 7 were cancelled. Each merge runs the suite on the deploy runner and again on
hosted CI, and a pull request runs it twice more (`push` on every branch plus `pull_request`,
`.github/workflows/ci.yml:7-10`); a documentation-only merge still deploys for ten minutes.
Both workflow files are generated by scshafe-dev, so changing that is the owner's call.

**Security (B+).** Layered auth (the door, then the API's own RS256 verification with an
allow-list), scoped panel tokens that fail closed, least-privilege units, images pinned by
digest, actions by SHA. Points worth the owner's attention:

- **`/view/` trusts `X-Forwarded-User`** when no bearer token is present
  (`src/cole_voice_lab/view/routes.py:124-136`). It is safe only while the API is reachable
  solely through the proxy; the proxy's configuration is security-critical.
- **Three requirement sets are not hash-locked**: `requirements/runtimes/journey-analysis.txt`,
  the Mac's `f5-tts-finetune-1.1.22-mps.txt`, and the dev extras, which the deploy's verify
  job installs on the production host.
- **SECURITY.md's status lines are stale** ("built, not yet deployed" for the vault and for
  speech). In a security document, a wrong statement of what is exposed is itself a finding.
- **Dependabot.** #135 (brotli) and #129 (opentelemetry-api) are green and safe to merge.
  #138 (pyasn1) and #139 (filelock, a major version) bump only the constraints file, not the
  hash-locked lock, which is why their CI fails; they need a re-lock, and #139 should not
  merge as it is.

### 3.7 The journey code

About 10,000 lines of `src/cole_voice_lab/journey/` are a bit-exact Python port of
voice-journey's Node pipeline, proven on the real corpus (7,018 files, no differences). The
read side is live. The pipeline's job runner waits on the cutover, so the PC serves a moved
snapshot while new memos are not processed there, and the original repository is still
receiving commits. It is a stable asset carrying a parity tax (`jscompat.py`, 668 lines,
exists only to match a Node tree that is being archived). It is also the least used asset in
the repository: a speech recognizer, pitch and voice-quality features, and a singing filter
that nothing on the speech side calls. Section 4 uses them.

### 3.8 Documentation

The decision log and the per-plan logs are careful and mostly true. The problems are at the
entrance and in the structure:

- **`CURRENT.md` is the least accurate file in the repository.** Its top three paragraphs are
  recent; lines 49 to 219 describe the September desktop app, a 36-prompt pilot, a
  verification of 1,149 tests, and Mac mini gates that D-021 and D-024 retired. It says
  nothing of the PC backend, the speech API in production, or the real open gates.
- **"No vault is deployed"** appears in `CURRENT.md:34`, `README.md:149`,
  `ARCHITECTURE.md:113`, `SECURITY.md:77-82` and `deploy/README.md:9`.
- **The Mac mini** is still the host in AGENTS.md's mission, `README.md:6`,
  `PROGRESS_CONTRACT.md:15` and `ARCHITECTURE.md:118`. `docs/RUNBOOK.md` is still titled for
  a handoff D-044 retired.
- **The repository is public and no document says so.** `docs/design/PHASE-4-LOG.md:234`
  still calls it private.
- **Owner steps live in five places** (`PHASE-2-LOG.md:349`, `PHASE-3-LOG.md:240`,
  `PHASE-4-LOG.md:468`, `RECORDING-V1.md:180`, `RECORDING-LOOP.md:59`), overlapping and
  unreconciled, and nothing records an owner act when it happens.
- **Superseded documents carry no marker**: `docs/DESKTOP.md`, `CAPTURE_AND_SYNC.md`,
  `CAPTURE_KIT.md`, `RECORDING_EDITOR_UX.md`, `CAPTURE_CONSOLE_PLAN.md` (its header says "not
  started"; it was built and is now legacy), `LIBRARY_VAULT.md`'s head.
- **Older decisions reversed without a note on the entry**: D-003 (by D-015 and D-039), D-006
  (by D-057), D-042 (by D-051 and D-052), D-041 (by D-043 and D-054), among others.

### 3.9 Tests and verify

**Grade: B+.** `scripts/verify` on the PC, `main` at `b8d36c5`, 2026-11-09: passed in
7 min 38 s. Backend: 6,996 tests passed (6,963 in the parallel pass, 33 serial), 151 skipped
(the macOS-only tests), 94.42% branch-aware coverage. Desktop: 655 passed, 91.88%. Ruff,
strict mypy on 224 source files, both wheel builds, the schema and contract-bundle checks and
the private-asset guard all passed.

The numbers behind the grade: 98,000 lines of backend Python against 122,000 lines of tests,
24,000 lines of desktop Python and 7,000 of QML against 20,000 lines of desktop tests; strict
mypy on three packages; coverage floors of 90% (backend) and 85% (desktop) with branches; any
Qt or QML warning fails the desktop suite; four TODO-class comments in the whole tree. Hosted
CI over the last 200 runs: 174 green, 26 red, of which 9 are the Dependabot lock mismatch and
4 were on `main`.

**Verify hung while this review was being written.** After the document was added, verify
was run again on this branch (a documentation-only change). The first run was cut off after
the backend suites passed. The second hung in the desktop pytest step
(`apps/desktop/tests`, `pytest -n auto`) for about 2.7 days until the owner's oversight
session killed it. It was not rerun, so this branch has no completed verify; the passing run
above is of `main` the same day. The cause was not investigated. The known PySide collection
hang (a pool thread waiting for the GIL while the collector frees Qt objects) fits, and is
unconfirmed. Two things are certain and worth fixing whatever the cause:

- **Nothing bounds a verify run.** `faulthandler_timeout = 300`
  (`apps/desktop/pyproject.toml:73`) dumps tracebacks and lets the test continue; there is no
  per-test or whole-suite timeout, so a hang costs the one-at-a-time verify slot until a
  person notices. A hard limit in `scripts/verify` (for example `timeout` around each pytest
  step, failing with the faulthandler dump) would turn days into minutes.
- **The same hang on the deploy runner would hold the deploy queue** up to its 60-minute
  job limit, and on a developer's machine it holds indefinitely.

What holds the grade below A, beyond that:

- **A second flake with an unfixed cause**: CI-only races on slow runners.
- **The suite pays for retired code.** Legacy capture, audition, references and datasets keep
  thousands of test lines running in every verify and every deploy.
- **Nothing in verify touches real audio or a real model**, by design. That makes the Mac
  checks and the opt-in smoke tests the only evidence of real behaviour, and they are the
  part that lags.
- **macOS-only tests skip on the PC and on hosted CI**, so the Swift half is verified only
  when someone runs verify on the Mac.

## 4. Enrichment ideas

Effort: S is a PR or two, M is a step set of three to six PRs, L is a phase. "Owner decision"
marks an idea that changes an invariant or a recorded decision, or spends the owner's time at
the microphone. Nothing here makes voice-lab record on its own.

### 4.1 Quick wins, ranked

| # | Idea | Value | Effort | Risks and dependencies |
| --- | --- | --- | --- | --- |
| Q1 | **One current-state page and one gate list.** Rewrite `CURRENT.md` to about 60 lines (what runs where, what is in flight) with a single owner-gates table (gate, what it unlocks, dated evidence when closed). Mark superseded documents with a status line. Fix the Mac mini and "no vault deployed" lines. Record the publication as a decision. | The owner sees in one place what his next hour buys. Every later session starts from the truth. | S | None. Documentation only. |
| Q2 | **Vault safety net.** `OnFailure=` and a daily `vault staleness --check` on the vault units; a vault class for the journey verdicts; and, for the owner, the recovery sheet offline and one restore drill of real data. | Turns a backup that probably works into one that is known to. | S (agent) plus an hour (owner) | The units are hand-installed, so the owner installs the change. The notification outbox has no sender yet (`PHASE-3-LOG.md:254`). |
| Q3 | **Spoken-text normalization and an abbreviation-safe splitter.** Numbers, dates, units, abbreviations, URLs, Markdown and emoji become speakable text before segmentation; the rule and its test vectors ship in a contract so parley deletes its copy. | Fewer misread replies in every parley turn, at no GPU cost. | S to M | Changes the audio for a given text, so the rule needs a version in the render fingerprint. Small owner decision: where the lexicon of exceptions lives. |
| Q4 | **A transcript check of every take.** Run the speech recognizer the journey code already has (`src/cole_voice_lab/journey/stt.py`) over accepted takes on the PC and flag takes whose words differ from the prompt. | Catches misreadings before they are trained on. The cheapest quality gain per take. | M | CPU only. Needs the journey analysis runtime hash-locked. A flag for review, never an automatic rejection. |
| Q5 | **Studio hardening.** An exception hook that shows the error and resets the phase; prune uploaded takes from the spool; read the take once in Save & Next; fix the two stale strings. | Removes the one way the studio can freeze mid-recording, and the slowest step of the loop. | S each | Pruning removes local copies, so it must wait for the PC's acceptance and stay opt-in (working agreement: non-destructive by default). |
| Q5b | **Bound every verify step.** A hard timeout around each pytest step in `scripts/verify`, failing with the faulthandler dump; then find the desktop hang's cause (3.9). | A hung run costs minutes, not days, and leaves evidence. | S, then M for the cause | None for the timeout. The cause may sit in PySide itself. |
| Q6 | **API server limits.** Request timeouts and a thread cap, as the vault server has; an index of unfinished jobs so a pass does not scan 30 days of history. | Availability and steady latency as use grows. | S | None of note. |
| Q7 | **Serve `ogg-opus`.** Encode finished segments beside the WAV, which stays the ledgered artifact. | A phone download of a reply drops by about ten times. | M | An encoder inside the runner's sandbox. Parley changes too. |
| Q8 | **A `speech-caller.v1` contract bundle and fixture server**, as `contracts/corpus-client.v1` does for the panel. | Parley drops a 477-line hand-kept fake and three guessed error shapes; the next caller starts from a contract. | M | A second bundle to keep frozen. |
| Q9 | **Retry semantics.** A job ended by a restart is requeued once instead of failed, and a failed job does not hold its idempotency key for 30 days. | A deploy no longer costs parley a turn. | S | The record's contract changes slightly. |
| Q10 | **Remove the three dead capture paths** (Widgets desktop, browser console, Node capture) and the bake-off's leftovers (`audition.py`, the reference publisher). | About 8,000 lines of source and their tests leave every verify and deploy. | M | Owner decision: D-025 already names the replacement, but "until parity" has been the rule. `finalize_capture_session` in `capture.py` is shared and stays. |
| Q11 | **Dependabot.** Merge #135 and #129; re-lock for #138; close or re-lock #139. | Clears four open PRs. | S | Each merge deploys. |
| Q12 | **A voice timeline.** For each release, render the same few fixed sentences once and keep them in the lab, so the owner can hear the voice change run by run. | Makes progress audible; costs seconds per release. | S | Kept audio in the lab under the existing retention rules; no caller involved. |

### 4.2 Bigger bets, ranked

**B1. Close the loop on the PC.** *Value: the project's purpose.* A fine-tune job in the GPU
slot (kind `finetune`, the speech API already answers `503 gpu_busy` for long jobs), corpus
selection from the PC's corpus store (the latest kept take per prompt, D-016), the F5-TTS
adapter on the ROCm lock, and the automatic release that already exists. Start it from the
CLI on the PC; the screen comes later (B6). *Effort: L.* *Dependencies:* the Mac's takes
imported to the PC; enough recorded corpus to be worth a run; spike that F5-TTS training (not
only inference) is sound on the PC's card. *Risks:* a run holds the one GPU for hours while
parley gets `gpu_busy`, so it wants a schedule or a pause between epochs; ROCm's newer lines
no longer list the consumer card, so the pinned stack is the stack. *Owner decision:* D-051
puts this after the v1 sign-off; doing it first reorders that.

An interim that is small: **a supported transfer of one finished Mac run to the PC** (a
checksum-bound bundle, sent over the tailnet between his own machines, imported with the
release checks). *Effort: M.* *Owner decision:* it moves derived weights and a reference clip
between hosts, which needs the transfer approval the invariants ask for; and the release
ordering in 3.3 must accept a run that finished before the newest release. It would put his
voice on the API within days of a decision rather than after phase 5.

**B2. An evaluation bench for fine-tuned runs.** *Value: every later choice (more corpus,
another family, other settings) becomes a measurement instead of an impression.* Four parts,
each useful alone:

1. A fixed held-out text set and a few held-out real takes that no run trains on.
2. Automatic diagnostics per run, written into the comparison record: word error rate from
   two different recognizers, speaker similarity against the held-out real takes (the real
   takes scored against each other give the ceiling), duration and clipping sanity. Several
   seeds per sentence, and paired differences between runs with intervals.
3. A blind A/B of run N against run N-1 (and against the real takes), by pointing the
   existing blind-review builder at fine-tuned runs. This restores the invariant.
4. A screen before the automatic release: a run whose render of the fixed set fails the
   recognizer badly is skipped, with the reason in the journal.

*Effort: M for 1 and 2, M for 3, S for 4.* *Risks:* a new isolated runtime for the metric
models; quality predictors are known to miss prosody errors, so they stay diagnostics, as
AGENTS.md already rules. *Owner decision:* part 4 amends D-055, which chose integrity checks
only.

**B3. A conversational pack for parley.** *Value: the one caller that exists gets faster and
more natural speech.* Beyond Q3, Q7, Q8 and Q9:

- **A phrase cache.** Renders are deterministic per text digest, release and settings, so a
  repeated sentence can be answered from retained audio without the GPU. Greetings,
  confirmations and "let me check" cost nothing the second time. *Owner decision (small):*
  what a cache hit writes to the ledger, and how long cached audio is kept.
- **An interactive lane.** Short jobs from an interactive caller go ahead of batch jobs;
  the first sentence of a reply renders at fewer flow steps if the owner's ear accepts it
  (the 16-against-32 listening check of `DECISIONS.md:1404` was never recorded as done).
- **Keep-warm that follows use**, so the 20 s cold start does not land on the first turn of
  a conversation.
- **Per-sentence pauses and speed**, and a longer text limit or multi-part job so parley's
  split and re-join goes away.
- **Word timings in the segment record**, replacing parley's loudness-based estimate (mean
  error around 100 ms by its own notes). This one is L and needs an aligner in a runtime.

*Effort: M to L in slices.* *Risk:* each slice touches the render fingerprint; keep the
rerun-identical property by versioning the settings.

**B4. Make the corpus fit its use.** *Value: the voice is trained on read sentences and used
for assistant replies; the gap is audible in every clone.*

- **An "assistant register" prompt pack**: questions, short confirmations, lists, numbers,
  dates, hedges, code and product names, spoken the way he talks. Published through the
  catalog like any other version.
- **His vocabulary from lexinet**: names and acronyms as prompts, and a spoken-form lexicon
  (how each acronym is said) shared with Q3. *Owner decision:* lexicon content would enter a
  public repository's catalog unless it is kept under the asset root.
- **Coverage-driven suggestions**: which phones, diphones and sentence shapes the kept takes
  under-cover, and which unrecorded prompts would add most, shown in the studio's library so
  the next sitting's two hours go where they help.
- **The nested corpus-size experiment** already proposed in `docs/CORPUS_RESEARCH.md` (30, 60,
  90 minutes), which B1 and B2 make runnable. It answers "do I need to record more?".

*Effort: M.* *Dependencies:* B2 to measure the effect. The owner records; nothing here
records for him.

**B5. A second model family, spiked first.** *Value: the comparison the owner asked for in
D-048 and D-049, and possibly a clearly better or faster voice.* From the web research
(unverified until spiked; section 6):

- **VoxCPM2** (April 2026): Apache-2.0 code and weights, 48 kHz, official full and LoRA
  fine-tuning, and higher reported speaker similarity than F5-TTS. The natural fidelity
  challenger. LoRA would also mean a run of megabytes instead of gigabytes, which changes
  what the vault and the release carry.
- **Qwen3-TTS** (January 2026): Apache-2.0, official single-speaker fine-tuning with a fixed
  reference clip, and streaming. The candidate for conversation speed.
- **Not to depend on**: Chatterbox (retired August 2026), Seed-VC (archived), VibeVoice (its
  TTS code was removed). The Chatterbox row in `docs/MODEL_FAMILIES.md` is stale.

Each needs a ROCm spike on the PC's card before anything else, in the manner of S-2: does it
load, train and render on the pinned stack without FlashAttention. *Effort: S per spike, L
per family built.* *Dependencies:* B1 and B2, or there is nothing to compare with.
*Owner decision:* which family to spend the spike on.

**B6. A Voice screen in the Qt app.** *Value: the Swift app can finally be retired, and the
owner manages his voice where he records it.* Listen to the current release, compare it with
the previous one, pin or roll back, start and watch a fine-tune, and score a blind review
without leaving the app. *Effort: L.* *Dependencies:* B1 (a jobs API on the PC) and B2.
*Owner decision:* whether Speak in the app is a speech caller like parley or its own lane.

**B7. Finish the cutover and shed the parity tax.** Merge the voice-journey redirect, archive
that repository, land #31, #37 and #40, then simplify `jscompat.py`, split `finetune.py` and
`cli.py` (4,397 lines), and hash-lock the journey runtime. *Value: one live copy of the
pipeline; a smaller suite.* *Effort: M.* *Dependencies:* the owner's real UMIK-1 session and
sign-offs, which gate everything here.

**B8. An announcer: his voice elsewhere in the house.** A batch, low-priority job class and a
second registered caller that pre-renders fixed phrases (agenda lines, vocabulary cards from
lexinet, greetings), with the house panel playing finished files. *Value: the voice becomes
part of the house, not only of one app.* *Effort: L, across three projects.* *Owner
decisions:* a panel token is refused on speech today and the panel should not hold a
machine secret, so either a relay renders for it or a user-subject token becomes a speech
caller; the panel plays 16 kHz stereo while the API emits 24 kHz mono; the registry allows
four callers.

**B9. Shared transcription on the PC.** A queued transcription job beside the speech jobs,
for voice-lab's own take checks (Q4), the panel's planned local transcription and the owner's
dictation project. *Value: one recognizer for the fleet.* *Effort: L.* *Owner decision:* a
new service class that carries private audio between his own devices.

**B10. Provenance marking.** An inaudible watermark on audio the API serves, with the release
id as its message, so a clip of the cloned voice can later be identified as synthetic.
*Value: modest for a private lab, larger the day a clip leaves the house.* *Effort: M.*
*Risks:* published detectors are imperfect; it changes the served bytes. *Owner decision.*

**B11. An edge voice.** A small CPU model (a Piper voice trained on the corpus, or a
zero-shot clone with a small model) so the panel or a Mac without a GPU can speak short
phrases offline. *Value: speech without the PC.* *Effort: L.* *Owner decision:* it is a
second voice beside "the newest release speaks" (D-055), and a zero-shot clone from a
reference is what the invariants refuse today. I would not do this before B8 shows the need.

**B12. Voice Memos as training data.** The journey pipeline already separates speech from
singing and transcribes it. *Value: hours of natural speech.* *Risks:* phone microphones,
transcript errors, other speakers in the room. *Owner decision* on screening. Ranked last on
purpose: studio takes are the better hour spent.

### 4.3 One ranking

By value over effort, with the owner's own steps listed where they outrank anything an agent
can build:

| Rank | Item | Kind |
| --- | --- | --- |
| 1 | Recovery sheet offline; one real restore drill | Owner, one hour |
| 2 | Mac build of #121 to #154, the Mac checks, the parity verdict; import the Mac's takes | Owner, one sitting |
| 3 | Q1 current-state page and gate list | Quick win |
| 4 | B1 close the loop (or its interim transfer first) | Big bet, owner decision on order |
| 5 | B2 evaluation bench, parts 1 to 3 | Big bet |
| 6 | Q3 spoken-text normalization and splitter | Quick win |
| 7 | Q2 vault alerting and the journey class | Quick win |
| 8 | Q4 transcript check of takes | Quick win |
| 9 | Q5 studio hardening; Q5b a bound on every verify step | Quick wins |
| 10 | B4 corpus fit (register pack, lexinet vocabulary, coverage) | Big bet |
| 11 | B3 phrase cache and interactive lane | Big bet, in slices |
| 12 | Q6, Q9 server limits and retry semantics | Quick wins |
| 13 | Q7, Q8 opus and the caller contract | Quick wins |
| 14 | B5 second-family spikes | Big bet, after B1 and B2 |
| 15 | Q10 remove dead paths; B7 cutover | Cleanup, owner gates |
| 16 | B6 Voice screen | Big bet, after B1 |
| 17 | Q12 voice timeline; Q11 Dependabot | Small |
| 18 | B8 announcer; B9 shared transcription | Cross-project, owner decisions |
| 19 | B10 watermark; B11 edge voice; B12 memos | Later, owner decisions |

### 4.4 Decisions that would be the owner's

- **Order:** phase 5's fine-tune job before the Recording v1 sign-off (B1), or the interim
  transfer of a Mac run.
- **D-055:** whether a run must pass an automatic screen before it speaks (B2 part 4).
- **Removal:** the three dead capture paths and the bake-off leftovers now, or at parity
  (Q10); and what "retire the Swift recorder" means while Speak and fine-tune live only
  there.
- **Whether retired attempts upload** (open in RECORDING-LOOP.md).
- **The phrase cache's ledger and retention rule** (B3).
- **Vocabulary from lexinet in the catalog**, public or under the asset root (B4).
- **Which family to spike** (B5).
- **A second speech caller and who may be one** (B8); **a transcription service** (B9);
  **watermarking** (B10); **a second, edge voice** (B11); **memos as training data** (B12).
- **CI shape:** two CI runs per pull request and a deploy on documentation-only merges come
  from scshafe-dev's generated workflows, which only an owner session changes.

## 5. A suggested first month

Not a plan, an order that follows from the ranking:

1. The owner's hour: recovery sheet, restore drill, then the Mac sitting (build, checks,
   parity verdict, import of the Mac's takes), and the one real UMIK-1 session that releases
   #31, #37 and #40.
2. Meanwhile, agent work that needs no decision: Q1, Q2 (as unit changes for the owner to
   install), Q3, Q5, Q6, Q9, Q11.
3. On the owner's word about order: B1's interim transfer or B1 itself, with B2 parts 1 to 3
   beside it so the first PC run is measured the day it finishes.
4. Then B4 with the next recording sitting, and the first family spike of B5.

## 6. Sources for the model landscape

Web research of 2026-11-09 by one of this session's reviewers. Dates are the content's.
Claims marked unverified in section 4 rest on these and on vendor numbers; none was
reproduced.

- F5-TTS releases (1.1.22, 23 July 2026): <https://github.com/SWivid/F5-TTS/releases>
- VoxCPM (April 2026): <https://github.com/OpenBMB/VoxCPM>
- Qwen3-TTS (January 2026): <https://github.com/QwenLM/Qwen3-TTS>,
  <https://github.com/QwenLM/Qwen3-TTS/blob/main/finetuning/README.md>
- CosyVoice (December 2025): <https://github.com/FunAudioLLM/CosyVoice>
- Fish Speech (March 2026): <https://github.com/fishaudio/fish-speech>
- Chatterbox (retired August 2026): <https://github.com/resemble-ai/chatterbox>
- Seed-VC (archived November 2025): <https://github.com/Plachtaa/seed-vc>
- VibeVoice: <https://github.com/microsoft/VibeVoice>
- Pocket TTS (October 2026): <https://github.com/kyutai-labs/pocket-tts>
- Piper: <https://github.com/OHF-Voice/piper1-gpl>
- ROCm compatibility matrix and 10.0 release notes (August 2026):
  <https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html>,
  <https://rocm.docs.amd.com/en/docs-10.0.0/about/release-notes.html>
- seed-tts-eval: <https://github.com/BytedanceSpeech/seed-tts-eval>
- UTMOSv2: <https://github.com/sarulab-speech/UTMOSv2>
- On what quality predictors miss (June 2026): <https://arxiv.org/abs/2606.19951>
- TTSDS2 (2025): <https://arxiv.org/pdf/2506.19441>
- LoRA against full fine-tuning of F5-TTS (Interspeech 2025):
  <https://www.isca-archive.org/interspeech_2025/kwon25_interspeech.pdf>
- AudioSeal: <https://github.com/facebookresearch/audioseal>

The other projects were read through their public repositories on GitHub: `scshafe/parley`
(its relay's `voicelab.py`, `sentences.py`, `timings.py` and `fake_voicelab.py`),
`scshafe/embedded-house-panel` (its voice-lab contract and speaker documents),
`scshafe/lexinet` (README and contract) and `scshafe/voice-journey` (README and recent
history). Nothing was written to any of them.
