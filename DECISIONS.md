# Decision log

## D-001 — Sibling lab, not ChloeSpeech core

Capture, biometric storage, dataset construction, training, and comparative
evaluation remain in `voice-lab` (named `cole-voice-lab` until 2026-10). ChloeSpeech receives only a validated,
hash-pinned promotion manifest. This preserves ChloeSpeech's provider-neutral
runtime scope.

## D-002 — Local-first, explicit asset root

The repository has no default writable media directory. Commands that handle
private data require an asset root argument or `COLE_VOICE_LAB_ASSET_ROOT`.

## D-003 — Native PCM and immutable originals

Production capture uses CoreAudio through FFmpeg and writes mono 48 kHz PCM WAV,
preferably 24-bit. Calibration information is retained as metadata and is never
baked into raw recordings. Accepted originals become write-protected.

## D-004 — Worker isolation

Each incompatible model stack executes out-of-process using a versioned JSON
contract. Shared caches may deduplicate weights, but Python dependency graphs do
not leak into the core CLI.

## D-005 — Two experiment tracks

Qwen3-TTS, Chatterbox, and F5-TTS participate in the direct-TTS track. Seed-VC
participates in a performed-source voice-conversion track and is not silently
ranked as if it generated prosody from text.

## D-006 — Pilot gate before corpus

The first artifact is a roughly five-minute pilot. The full 90-minute training
corpus is conditional on recording quality and the zero-shot result.

## D-007 — Evaluation engine identity is not production-provider identity

MLX Chatterbox outputs are explicitly unwatermarked and are not mislabeled as
outputs from ChloeSpeech's official upstream Chatterbox adapter. If an MLX
candidate wins, promotion requires either a dedicated, reviewed ChloeSpeech MLX
provider or a separately evidenced official-runtime reproduction. The lab never
fabricates Perth-watermark evidence.

Amended 2026-10-06 (D-026): on the PC, Chatterbox candidates run through the official
upstream PyTorch runtime, which applies its Perth watermark, so a Chatterbox winner from a
PC bake-off needs no separate official-runtime reproduction. MLX results remain historical
evidence under the rule above.

## D-008 — Sequential candidate residency

The 16 GiB mini runs one candidate in one fresh subprocess at a time. Model
process exit is the Metal-memory release boundary. Weight caches may also need
sequential explicit lifecycle management because the 10 GiB free-space floor
prevents all quality profiles from coexisting on the current disk.

Amended 2026-10-06 (D-026): when the model workers move to the PC, the rule stays and
its reason changes: one candidate at a time within the RX 6900 XT's 16 GB of VRAM and a
single GPU slot shared with fine-tuning and Speak. Process exit remains the release
boundary.

## D-009 — Loopback capture control and fail-closed resume

A foreground, loopback-only capture console may drive the native FFmpeg
recorder. The browser controls recording but never captures audio. An
unfinalized session may be resumed only by re-inspecting every completed
canonical take; partial recordings are retained as rejected forensic evidence
outside the session directory and are never promoted to canonical takes.

## D-010 — Native desktop recording library

Cole clarified on 2026-09-07 that the primary capture interface must be a
standalone Python desktop window with the entire prompt catalog, completion,
recording dates, and preserved history. This supersedes D-009's choice of a
browser interface as the primary operator flow; its native recording and safe
resume rules remain in force. The older console is still an explicit command.

Use optional Qt for Python Widgets for the native window. Tkinter was considered
but its native extension is absent from this host's Homebrew Python; adding it
would alter the existing interpreter setup. Qt wheels can be pinned separately
in the development environment without changing capture-kit/model runtime locks.
The desktop does not serve HTTP or use a webview, and FFmpeg remains the only
microphone recording implementation.

The library catalog and the current recording batch are separate. A batch is
an exact selected prompt suite with room tone; retaking older material creates
a new session. Private provenance sidecars bind per-take UTC start times and
audio checksums outside transferable packages. Legacy recordings expose their
known session date. Timestamps are never guessed from filesystem modification
times. Historical evidence is revalidated and deduplicated across temporary and
accepted-raw copies. Existing raw assets and prompt suites are never migrated.

This supports later larger catalogs and repeated voice snapshots without
rewriting capture or storage. Full-corpus capture still requires pilot approval;
the initial desktop catalog is the existing 36-prompt pilot.

## D-011 — One script inspector and state-safe transport

Cole's 2026-09-07 interface feedback favors a horizontal library/inspector over
the original library, detail, and bottom-capture arrangement. Keep one large
read-only script, with its instructions, transport, and history in the inspector.
Use system typography and native background palettes, with semantic action
colors. Dates, age, and counts remain optional sortable library columns because
comparing old recordings is an original requirement, not redundant metadata.

The inspector follows the canonical queued prompt whenever a session is open.
Keep already advances the backend queue: selecting the next global Missing row
instead would omit intended retakes or required room tone. Advance the visible
context to the backend's next prompt, but never automatically open the microphone.
Filtering or selecting unrelated library rows must not redirect active capture.

R controls recording; Space controls playback; Return/Enter keeps a reviewed
take. Shortcuts are window-scoped and protect text entry, dialogs, popups, and
held keys. Read-only script focus does not disable transport. Playback termination
is asynchronous, so a stop request alone does not enable conflicting actions.

Use explicit state text and elapsed time instead of introducing another audio
capture path for a waveform. Timers must not rewrite the script or reset its
scroll position. Capture, evidence, consent, corpus scope, and immutable history
remain unchanged. Do not restart the operator's application during development.

## D-012 — Selected-prompt recording and reversible audio editing

Cole's subsequent feedback supersedes D-011's queue-only selection and elapsed-only
playback. The selected library row is the recording target while idle or reviewing;
row changes are disabled during capture. Desktop execution may be out of order,
but canonical package order, mandatory room tones, and explicit Keep remain intact.
Old v1 desktop evidence continues to resume without migration. Crossing batch scope
preserves the previous session before preparing the selected prompt. No microphone
starts merely from selection, Keep, or Retake.

The original PCM waveform is drawn in Qt, with frame-addressed trim handles,
precise In/Out fields, and a seek slider. A lazy standard-library AVAudioPlayer
bridge supplies native pause/seek and the actual media clock. Preview buffers
contain exactly the selected PCM frames; clock observations are not claims of
successful playback completion. Audio control failures latch the UI safely until
reopen instead of enabling capture over uncertain output.

Trim edits are private checksum-bound recipes and separate PCM derivatives.
Original and accepted raw audio never change; raw-only reference/dataset contracts
do not silently acquire edited identity. Deleting a take is reversible exclusion,
with Restore, enforced at shared accepted-source eligibility as well as the UI.
Purging originals, exporting edited training data, and widening the corpus remain
separate decisions. Current-take review and Keep-with-trim bind the exact session,
prompt, and canonical path, preventing older/rejected audio from being mistaken
for the active take. Native semantic icons retain labels/tooltips and palette-aware
contrast. The icon's exterior checkerboard is locally masked to transparency,
preserving every retained interior pixel.

## D-013 — Silent tasks are instructions, not spoken scripts

Cole's room-tone screenshot demonstrates that placing every prompt's text under
Script invites reading instructions aloud. In the desktop, category `room-tone`
therefore selects a distinct instruction-only card headed Do not speak, with a
muted-speaker icon, requested duration, and explicit silent-recording transport.
Only speech appears under Read aloud, with delivery guidance separately labeled
as not spoken. Library previews describe silent tasks without copying instructions
into a transcript-looking cell. Native palette changes and accessibility must
preserve readable instruction content.

This is presentation only: existing strict prompt schemas, exact source text,
catalog digests, take identities, and saved recordings remain unchanged. Category,
not prompt ID or text matching, determines silence. Keep and resume follow the
actual selected/canonical task; selection remains locked during recording. Duration
is guidance, not an automatic stop, and earlier mistaken takes remain available
for explicit operator review/retake rather than being silently edited or removed.

## D-014 — Capture duration is encoded audio, not subprocess lifetime

The recording counter previously counted subprocess startup and continued to
extrapolate while Stop waited for shutdown/QC, whereas saved duration came from
WAV frames. These are different clocks. Controlled capture now exposes confirmed
encoded PCM frames through the existing CaptureProcess contract; CaptureState's
elapsed_seconds is None before audio, then frames / 48,000. Native playback keeps
its own existing media clock. No phase/schema or recording-evidence migration is
needed; recording-request timestamps and the wall-time safety watchdog retain
their original meanings.

Use FFmpeg's metadata-only per-packet `{size}` output for the canonical mono
24-bit stream, then divide bytes by three. This remains accurate when startup
varies, input timestamps jump, or WAV output is buffered. Wall-time offsets and
timestamp-derived progress do not provide that same sample-count contract.
[FFmpeg documents packet statistics and their output-specific option scope](https://ffmpeg.org/ffmpeg.html#Advanced-options).
The stdout pipe carries counts, never human audio. Lines are bounded and drained;
invalid/missing clock evidence remains an explicit error. Final encoded-frame
counts must agree with the safely inspected WAV before publication. Foreground
terminal capture remains unchanged.

The desktop distinguishes Starting microphone, Recording, and Stopping/checking.
It never extrapolates capture duration between observations or past a Stop request.
Final review uses the actual WAV duration. This is timing/feedback correction, not
silence insertion, warmup trimming, noise processing, or a new recording backend.

## D-015 — Native buffered desktop capture with continuity evidence

The sample-count clock in D-014 correctly measures received audio but cannot
establish whether earlier input buffers were lost. Inspection of original WAVs
found repeated digital-buffer-aligned joins; an explicitly approved local live
probe confirmed missing buffers in the FFmpeg AVFoundation input path. Its single
unread-buffer slot and polling behavior are unsuitable for this recording workflow.
This is not solved by gain changes, inserting silence, or changing the displayed
duration. Existing recordings remain untouched.

New desktop sessions use macOS Audio Queue capture through a standard-library
ctypes worker in a separate Python process. This isolates the audio callback from
desktop Python work. Eight native buffers and a bounded off-callback writer queue
preserve continuous mono 48 kHz, 24-bit PCM; callbacks copy and enqueue without file
or pipe IO. The worker verifies the selected hardware UID, canonical format, every
sample timestamp, writer frame counts, and shutdown status. Failure retains a
private partial and cannot publish completed audio. The process contract and its
sample-count clock remain the same; no audio crosses a subprocess metadata pipe.
[Apple documents the multi-buffer Audio Queue recording lifecycle](https://developer.apple.com/library/archive/documentation/MusicAudio/Conceptual/AudioQueueProgrammingGuide/AQRecord/RecordingAudio.html).

Alternatives considered were maintaining a custom FFmpeg AVFoundation FIFO build
or using AVAudioRecorder. The former requires distributing a patched native tool;
the latter does not expose the needed explicit input UID/per-buffer continuity
contract in the inspected macOS interface. Audio Queue exposes both directly,
without an additional Python dependency or a change to the Mac's default input.

Successful continuity evidence is private, versioned, and bound to the exact WAV
SHA-256, size, frame count, partial filename, selected UID, and timestamp span.
The parent validates that evidence before canonical publication. Reports remain
outside transferable raw packages. Missing, malformed, unsuccessful or mismatched
evidence fails closed. Cancellation during shutdown or evidence generation cannot
be mistaken for successful capture.

The CaptureDevice contract now permits `coreaudio`; its historical default remains
`avfoundation`. Old sessions and audio remain readable, but the desktop requires a
new native session for further recording rather than rewriting an older session's
recorder identity. Legacy terminal/console capture remains explicitly available
and is not recommended for further training capture while affected.

Portable PCM QC adds conservative repeated 512-frame-boundary discontinuity and
short internal digital-silence findings. Midpoint controls, neighboring derivative
checks and quantization guards reduce false positives. These are review flags,
not proof of the loss stage, missing duration, or perceptual quality. Live timestamp
validation is the primary continuity gate for new native takes; headphone review
remains the operator's final acceptance.

## D-016 — The fine-tuning corpus is the library's latest kept take per prompt

Cole stated on 2026-09-15 that the corpus is the set of catalog prompts, that the live
corpus is the most recent recording of each prompt, that Cole will revise a few prompts
at a time on an ongoing basis, and that Cole wants to re-tune after each revision.
Recording and training happen on the same Mac.

`voice-lab finetune` therefore reads the corpus straight from the recording library:
the latest retained take of every spoken prompt, native and old-app alike, with the
same retained predicate as Library coverage. Lab packages stay the transfer and
acceptance unit; they are not the training unit, and a package's one-group rule does
not limit the corpus. Training copies are level-normalized with a linear gain because
raw UMIK-1 takes sit near -50 dBFS RMS and F5-TTS treats them as silence; originals are
untouched. Training and synthesis run out of process in a pinned F5-TTS runtime, and
every run records its corpus, dataset, hyperparameters, runtime and checkpoint digests
under `derived/finetune/<run-id>/`.

Fine-tuned checkpoints and listening samples are working artifacts, not accepted raw,
references or dataset manifests; consent for rendering, transfer approval and promotion
are unchanged. See docs/FINETUNE.md.

Amended 2026-09-16: the app's Speak page (D-019) also synthesizes with a finished run or the
base weights. It writes under `derived/speech/<operation-id>/`, never into a run, and its
clips have the standing of listening samples. Neither `voice-lab finetune` nor the app reads
a consent record; D-019 records that scope and leaves it open for Cole to confirm.

Amended 2026-10-06 (D-022): from design phase 5, fine-tuning runs in the PC's job runner,
not on the Mac that records, and requires an approved, checksum-bound consent record
whose purposes include `private-fine-tuning` for the `home-host` execution boundary. Until
then the Mac-local behaviour above is unchanged.

## D-017 — The prompt catalog is data in the corpus store

Cole asked on 2026-09-15 that prompts be editable without rebuilding the app. Cole
confirmed that an edit makes earlier recordings of the prompt stop counting by default,
and that an edit made with `--keep-recordings` (typo fixes, punctuation, casing) keeps
them counting, with the corrected text as their training transcript. Cole had also decided
that the two room tones leave the catalog and that the Harvard (720) and CMU ARCTIC
(1,132) sentence sets are added; both are carried out through this store. Before this, the
catalog was a pinned resource frozen into the build's settings, and every take's group
embedded the whole catalog, which would have added about 400 KB to each of a take's
evidence files for the 1,886-prompt catalog.

The catalog is now data. Prompts, their append-only revisions and published catalog
versions live in the library's corpus store, `provenance/corpus/corpus.sqlite3`. Each
version is pinned by the canonical digest of the `PromptSuite` it materializes and backed
by an immutable, content-addressed snapshot under `provenance/corpus/snapshots/`, whose
whole-file digest the version's reference also carries. New recording groups
(`recording-group/v2`) reference a version instead of embedding the catalog. The app's
settings carry no catalog: after its handshake the app asks the recording service, which
pins the subject's active version for the life of the process. Adding, editing or
switching prompts is therefore `voice-lab catalog` publish and activate plus a relaunch,
never a rebuild, and `--expect-catalog-sha256` no longer exists. The store is authoritative
and is never rebuilt, unlike the recording index.

Several things do not change. Take metadata stays in files; the database holds prompts
and catalogs only. The `Prompt` and `PromptSuite` contracts and their digests are
unchanged, so every existing prompt digest and `catalog_sha256` keeps its meaning.
Groups that embed their catalog (`recording-group/v1`) are immutable evidence and are
read by that contract forever; this is a permanent reader rule, like the sidecar-less rule
in docs/PROMPT_CATALOG_V2.md §3.4, not a migration step. The built-in resources stay
immutable, hash-pinned resources; they now also serve as seeds (`voice-lab catalog init
--from-builtin`), which copy their prompts into the store with identical digests. D-010's
"Existing raw assets and prompt suites are never migrated" still holds: built-ins are
seeded, not migrated, and no stored suite, group, take or raw file is rewritten.

A keep-recordings revision accepts earlier prompt digests only when the prompt keeps its
identity: the same prompt id, the same held-out flag, and the same behaviour (spoken, or
the same silent category). The store refuses a keep-recordings edit that changes any of
these, and every reader applies the same rule. The reason is that held-out evaluation
audio must never reach training and silence must never count as speech. For the same
reason the fine-tune corpus refuses a held-out catalog (`FINETUNE.CATALOG_HELD_OUT`) and
never trains on held-out or silent recordings. The store additionally limits a
keep-recordings edit to typo-class text changes unless the operator allows a rewording
(`--allow-rewording`), so kept audio says the words it trains with. For store catalogs
this is the one exception to the exact-digest coverage of docs/PROMPT_CATALOG_V2.md §3.1;
built-in catalogs accept no aliases.

The store, not the command line, owns every rule about what it may hold: the keep-recordings
rules above; every live prompt fitting the purpose of every catalog (a version publishes
every live prompt); and no two live prompts reading the same once case, punctuation and
spacing are ignored (one recording would count twice). Its write methods apply them in the
same transaction as the write, so every editor inherits them. The Harvard and CMU ARCTIC
lists are pinned by the SHA-256 of the published files: neither a plain-http fetch nor a
saved copy is otherwise authenticated, so another copy is imported only when the operator
accepts it explicitly, and each of its prompts records that.

The command line is the editor; an in-app editor is a separate change and writes through
the same store methods. The rollout is
tracked in `migrations/active/corpus-store.md` until the real library is seeded and the
new build is installed and confirmed. See docs/CORPUS_STORE.md.

## D-018 — The recording app is corpus-first and fine-tunes in place

Cole decided on 2026-09-15 that groups, sessions and packages should leave the recording
app's interface. The corpus is the latest recording of every prompt (D-016). Fine-tuning is
an explicit button, never automatic, because each run is a GPU job of about two hours. That
rules out starting one after every Save. Recording and training happen on the same Mac.

The native recording app therefore has one action beyond recording: **Fine-tune model**. It
runs the same `finetune.run_finetune_pipeline` as `voice-lab finetune run`, over the latest
kept take of every spoken prompt that has one. It always sends `allow_missing`, because the
active catalog lists standard sentences nobody has recorded yet. The recording service runs
the job in a separate worker process group, one job at a time. A second request is refused
`FINETUNE.JOB_ACTIVE`, and progress is shown in the status line. Cancel stops the whole
process group. A job interrupted by a service stop is failed `FINETUNE.INTERRUPTED` and is
never resumed or retried. Recording, Save and navigation stay available while a job runs,
because the worker takes no library lock and only reads takes. Prepare for lab, its sheet,
the service's package queue and the `package-preparation` native suite are removed. The app
shows no group, session, setup, package or room-tone-binding wording. The Library counts
prompts with a kept take against the active catalog.

For the native recording app, these D-010 and D-012 sentences about operator-visible
batches, sessions and package preparation no longer hold:

- D-010: "A batch is an exact selected prompt suite with room tone; retaking older material
  creates a new session."
- D-012: "Desktop execution may be out of order, but canonical package order, mandatory room
  tones, and explicit Keep remain intact." Explicit Keep (Save) still holds; package order
  and mandatory room tones do not.
- D-012: "Crossing batch scope preserves the previous session before preparing the selected
  prompt."

The native app never had operator-visible sessions (`docs/NATIVE_RECORDING_UX.md` §6). The
native-app statements this decision retires are in that document: groups "visible as
secondary history metadata", "a manual New setup action", "Catalog coverage and export
context are distinct", and the Prepare for lab paragraphs of §9. Each carries a dated
amendment there. The quoted sentences still describe the Qt desktop (`voice-lab desktop`),
whose batches, sessions and finalization this decision does not change.

Several things do not change. Package maps, package aliases and existing v1 packages stay
readable, and removal and trim identity still follow a take through them. The lab's transfer
path (`sync plan`, `intake`, `dataset`) and its consent and transfer rules are unchanged;
nothing leaves the Mac. Recording groups remain as internal plumbing: every reservation names
one, and the service still creates them automatically. `PreparePackageCommand` and
`RecordingPackageStore` stay in the code, but no app, service or `voice-lab` command
prepares a package from native takes any more; adding one is a separate change. Room tones
leaving the catalog is D-017's. The training rules are D-016's. Fine-tuned checkpoints and
samples remain working artifacts, not accepted raw, references or dataset manifests. See
docs/FINETUNE.md ("From the app") and docs/NATIVE_RECORDING_UX.md §16.

Known gap, open for Cole (2026-09-16): with Prepare for lab gone, nothing turns native takes
into a lab package, so the lab's transfer path (`sync plan`, `intake`, `dataset`) cannot
receive recordings made in the native app. Fine-tuning on the recording Mac does not need
it. Whether to add a `voice-lab` command for that is Cole's call.

Amended 2026-09-16 after the proofread: a job trains on the catalog version the service
pinned at launch, the one the app shows, not on whatever is active when the job starts.
`voice-lab finetune run` and `train` refuse `FINETUNE.JOB_ACTIVE` while the app's job is
queued or running. The runtime's Vocos vocoder is pinned (`charactr/vocos-mel-24khz` at
`0feb3fdd…`, under `pretrained/vocos-mel-24khz/`) and checked with the weights before any
work, so a missing vocoder fails a job in seconds instead of after training. A job stopped
while writing samples leaves a usable model without samples. Quitting while a job is queued
or running asks first, and an interrupted job is reported at the next launch.

Amended 2026-09-16: the app now has a second page, Speak (D-019). Fine-tune model and Speak
share one GPU slot in the recording service. A fine-tune is refused `FINETUNE.JOB_ACTIVE`
while a Speak request is queued or running, and a Speak request while a fine-tune is queued
or running. The Record page and its one action beyond recording are unchanged.

Amended 2026-09-19 (D-020): the encrypted library vault is the separately approved
storage exception to the transfer wording above. Capture sync, hosted model execution,
local fine-tuning and Speak retain their own boundaries; the vault never synthesizes.

Amended 2026-10-06 (D-022, D-025): the app's Fine-tune model action moves, with phase 5,
to the PC's job runner behind `/api/v1/jobs` and a consent record; the app itself is
replaced by the `scshafe-qt` desktop app after phase 4's acceptance criteria.

## D-019 — In-app speech is a local listening tool

Cole asked on 2026-09-15 for a separate view in the recording app to type text and perform
text-to-speech with the tuned model. Recording and training already happen on the same Mac
(D-016, D-018).

The native app therefore has a second page, **Speak**. It synthesizes locally, out of
process in the pinned F5-TTS runtime, with a finished fine-tuned run (the newest by
default, or an earlier one) or with the pinned base weights. The reference clip is a
reference-candidate prompt's level-normalized training copy in a finished run's dataset,
checked against that run's recorded digest. Base uses the newest finished run's clips, so
nothing speaks before a run has finished (`FINETUNE.NO_FINISHED_RUN`). Each request writes
one clip and its record under `derived/speech/<operation-id>/` in the library, private and
checked from its WAV header. Finished runs are never modified.

The recording service runs one request at a time, as a second, in-memory job kind of the
fine-tune supervisor. Speak and fine-tuning share one GPU slot: each is refused
`FINETUNE.JOB_ACTIVE` while the other is queued or running, so synthesis never runs
alongside the app's training. A request has 120 seconds (`FINETUNE.SYNTHESIS_DEADLINE`),
and Cancel kills its worker's process group. The page plays clips through its own player,
which yields to recording like the review player, and can save a byte copy to a place the
operator chooses outside the library. Nothing leaves the Mac, and synthesis keeps F5-TTS's
temporary copy of the reference clip inside the library, removing it when it ends.
`voice-lab finetune run`, `train` and `synthesize` refuse while the app trains or speaks,
but the app does not see work started in a terminal (docs/FINETUNE.md).

Speak clips are working audio for listening, with the standing of fine-tune listening
samples (D-016). They are not accepted raw, references, dataset manifests, promotion
evidence or ChloeSpeech artifacts, and nothing in the lab reads them as any of these. A
Save As copy is a file outside the lab's records. The consent and promotion rules for
recognizable-human synthesis in AGENTS.md, and the transfer approval rule, still govern
every use beyond local listening: publishing a reference, building a dataset, promotion,
ChloeSpeech, or moving audio off the Mac.

Speak does not meet AGENTS.md's invariant "Recognizable-human synthesis requires a
checksum-bound reference and an approved, checksum-bound consent record with a matching
subject and purpose" as the lab's rendering paths do. It reads no consent record. Its
reference is bound to a fine-tune run's dataset digest, not a published reference
(docs/REFERENCES_AND_DATASETS.md). No consent purpose names it: the purposes are
`private-zero-shot-evaluation`, `private-fine-tuning` and `private-chloe-speech-rendering`.
The same has held since D-016 for `voice-lab finetune`, its listening samples and the app's
Fine-tune model, none of which reads a consent record. This decision makes that an explicit
scope limit: in-app synthesis is local listening by Cole, the subject, to Cole's own voice,
on the Mac that holds Cole's library. The page does not check who is at the keyboard, so the
limit rests on the app and the library being Cole's.

Open for Cole (2026-09-16):

- Confirm this consent scope for local listening, or require an approved consent record
  (for example a new purpose for local listening) before Speak, and fine-tuning, may run.
- Speak clips accumulate under `derived/speech/`, and nothing removes them. (The service
  removes only what a request cut short by a service stop or crash left without
  `speech.json`, when it next starts.) Whether to add retention or a delete action is
  Cole's call.

See docs/FINETUNE.md ("Speak page") and docs/NATIVE_RECORDING_UX.md §17.

Amended 2026-09-19 (D-020): "Nothing leaves the Mac" above describes Speak synthesis
and its generated clips. The separately approved library vault stores encrypted library
copies on Cole's own host; `derived/speech/**` remains Mac-only. The vault never runs a
model and does not extend local listening to another execution boundary. This amendment
does not answer the first open question above: consent for local fine-tuning and Speak
still awaits Cole's decision.

Resolved 2026-10-06 (D-022): Cole answered the first open question. When Speak and
fine-tuning move to the PC, both require an approved, checksum-bound consent record with a
matching subject, whose purposes name the act (`private-fine-tuning`, or the new
`private-home-host-synthesis` for Speak) and whose execution boundaries include the new
`home-host`. Synthesis then runs on a different machine from the listener, which is why
"local listening" no longer suffices as a scope. Until phase 5, the Mac-local Speak above is
unchanged. The second open question, retention of Speak clips, remains open.

## D-020 — The library vault is an encrypted durable copy

Cole decided on 2026-09-17 to keep an encrypted copy of the recording library on
`cole-ubuntu-pc`, reached through a dedicated tailnet node. The Mac keeps the working
prompt database and local files. Local `flock`, SQLite WAL, atomic no-replace publication,
and file identity checks remain local; the vault supplies content-addressed blobs and
compare-and-set references through storage ports, never a remote filesystem or catalog.
It is the intended durable copy, with no second vault destination or automatic backup.

The Mac seals every file chunk and manifest with AES-256-GCM before uploading. A fresh
96-bit nonce and associated data binding the library id prevent cross-library reuse;
blob references identify ciphertext by SHA-256 and size. The 256-bit library key lives in
the login Keychain, with an offline recovery sheet kept by Cole. Neither the server nor
its configuration receives that key. Losing both Keychain key and sheet loses access to
the sealed library. Read/write tokens are separate credentials: the Mac keeps raw tokens
in Keychain and the server configuration holds only their SHA-256 hashes. Rotating those
tokens does not rotate the encryption key.

For this encrypted storage destination, these earlier rules require a scoped amendment:

- AGENTS.md: "No human audio is sent to a hosted service or GPU host without a separate,
  explicit transfer approval naming the destination and payload digest."
- AGENTS.md: "Downloads, remote deployment, hosted transfer, model eviction, and raw-asset
  removal require explicit flags."
- D-018: "The lab's transfer path (`sync plan`, `intake`, `dataset`) and its consent and
  transfer rules are unchanged; nothing leaves the Mac."
- D-019: "Nothing leaves the Mac" and its application of the transfer rule to "moving
  audio off the Mac."

A standing `VaultDestinationApproval` replaces a separate per-upload approval only for
encrypted library storage on Cole's approved host. It binds the library id, endpoint
FQDN, namespace, stable Tailscale node ID, `tag:voice-lab-vault`, MagicDNS suffix and file
classes, plus approver, statement, effective timestamp and mandatory expiry. Cole chose
12 months; the contract permits at most 366 days. Preview writes nothing. Confirmation
reconstructs the same record, including `approved_at`, and requires the preview's digest
before publishing an immutable approval. The binding pins that approval's file digest.
Approval and binding are the explicit acts enabling later background copies; they are
not authorization to deploy a host or remove local data.

Before each client session, the Mac checks the answering node's StableID, tag and DNS
identity, then connects to that checked address with TLS verified for the approved
endpoint name. Certificates are not pinned. Before the first new blob of a generation,
the Mac writes a chained ledger entry naming the destination, classes and plaintext
manifest-plan payload digest. This plan has no sealed chunk references yet; the completion
entry records the resulting manifest after the head swap. The manifest chain and local
state reject rollback and forks. Presence is proven by downloading, decrypting and
rehashing bytes, not by trusting the host's claim. A new Mac needs a separately retained
generation and manifest digest for rollback protection; accepting the latest head is an
explicitly weaker choice when that anchor is unavailable. On a new Mac the pinned pair
can select only the current head unless the required local manifest chain is separately
available; an old first-push pair alone cannot retrieve historical generations after
the head advances. This version has no automatic historical-manifest recovery helper.

The vault needs no approved consent record. It is Cole's own storage host, receives only
opaque ciphertext, and never executes a model or synthesizes speech. It is not a hosted
GPU transfer or a new synthesis boundary; `ConsentRecord.hosted_transfer_allowed` stays
`Literal[False]`. Hosted/GPU transfers still need their separate destination- and
payload-specific approval. D-019's local listening scope is reconsidered only to make
this storage distinction: synthesis remains on the Mac that holds the working library,
and the open question about requiring consent for Speak and fine-tuning remains open.

`training` (datasets and finished fine-tune runs) is a separate opt-in class in both the
binding and approval; Cole decides whether to include it at deployment. Speak clips
under `derived/speech/**` remain Mac-only. So do local vault records, locks, leases,
unfinished runs, machine-bound caches and staging files. The prompt database is copied
through SQLite backup, not by shipping its live WAL files. Recording continues locally
while the vault is unreachable; the copier takes no lab writer lock.

A finished run leaves local checkpoint storage only through an explicit per-run
`finetune archive --run <id> --remove-local-copy`. Archiving first copies and verifies
the whole run by download, then records the archive. Removal verifies the whole run
again, checks that no app training or Speak request is active, records the intended
removal, rehashes the local files and unlinks only `checkpoints/**`. Dataset clips and
run records stay local, and `restore-run` restores the removed checkpoints. Merely
copying a run or enabling background copying never removes it. Speak marks Latest and
named runs Archived and requires restoration when their checkpoints are missing; Base
continues using its pinned untuned weights and the reference clips that remain local.

Encryption is the exception to the standard-library capture/transfer preference:
`cryptography` is an exact-version optional `vault` extra, with its `cffi` and `pycparser`
closure hash-locked in `requirements/vault-macos.txt`. Only `storage_sealing.py` imports
it. The frozen Mac service runtime carries the closure for its copying worker; capture
kits and the vault server do not. Optional model stacks remain isolated.

Two limits are accepted. Sealing does not hide sizes or timing: each blob is exactly
33 bytes longer than its plaintext, permitting size correlation and guesses about
distinctive files. The host can also withhold or delete data. Encryption detects changed
content but cannot guarantee availability; there is no automatic second copy. The host's
privileged account is not trusted with plaintext. The server is a hardened non-root
container on an internal network, reached only through its dedicated HTTPS sidecar.
Deployment applies a dedicated tag and access rules with Cole; Tailnet Lock remains off.

As of 2026-09-19, the implementation and deployment templates are built and tested with
synthetic data. No real library is bound, no library key or tokens have been created for
it, no vault is deployed, and none of Cole's library has left the Mac. Step 15 requires
Cole before each remote or tailnet action and pauses for Cole to create credentials and
store the recovery sheet privately. The pending combined app build/install is a separate
decision before deployment. Base availability, narrowing the second archive verification,
and requiring an explicit approval timestamp remain unanswered implementation choices;
their existing behavior is unchanged by this decision.

See [docs/LIBRARY_VAULT.md](docs/LIBRARY_VAULT.md),
[docs/RUNBOOK.md](docs/RUNBOOK.md#9-library-vault), and
[deploy/vault/README.md](deploy/vault/README.md).

Amended 2026-10-06 (D-023): the direction reverses and the single destination becomes
several, all on Cole's own hardware. The PC holds the plaintext working library and seals;
ciphertext-only vaults run on the laptop (nightly) and on the MacBook Air (weekly; the
offsite copy), with an optional rotated USB drive later. "It is the intended durable copy,
with no second vault destination or automatic backup" and "there is no automatic second
copy" no longer hold. The library key becomes an encrypted systemd credential on the PC,
never held by any vault host. No cloud or paid storage is used (D-028).

## D-021 — The PC is the lab's home host

Cole accepted the combined voice platform design on 2026-10-06
([docs/design/VOICE-PLATFORM.md](docs/design/VOICE-PLATFORM.md), its §13 decision 6). The
lab's backend, asset root (`/data/voice-lab/lab`) and model workers move to the PC, the
Arch workstation (registry key `arch`, AMD RX 6900 XT, `gfx1030`). The Mac mini's intake
role is retired.

The PC is a GPU host under AGENTS.md's transfer rule. For uploads to it alone, a standing
home-host approval replaces the per-plan `plan_sha256` approval. It is shaped like D-020's
`VaultDestinationApproval`: it binds the endpoint FQDN, the node's stable Tailscale ID,
`tag:voice-lab`, the tailnet suffix, the file classes (`capture`, `measurement`) and a
mandatory expiry of at most 366 days. The client re-checks the node identity before each
upload session and writes a chained ledger entry naming the session's manifest digest
before the first byte. Any other destination of Cole's own needs the separate per-payload
approval; there is no hosted destination (D-028).

On the PC the network-facing API container cannot write `raw/`; only the intake publisher,
a host unit without network, publishes accepted audio, through the unchanged validator.
Model workers run in a private network namespace.

This amends AGENTS.md's transfer rule for this one destination, the "Mac mini" wording of
AGENTS.md's mission, ARCHITECTURE.md's trust model ("the intake publisher re-hashes…")
and D-008's reason for sequential residency. Those texts change with the design's phase 2,
which builds the host; until then they describe what runs.

## D-022 — Fine-tuning and Speak run on the PC, behind a consent record

Cole decided on 2026-10-06 (design §13, decision 7) that fine-tuning and Speak move from
the Mac that records (D-016, D-018, D-019) to the PC's job runner, and that both then
require an approved, checksum-bound consent record. This answers D-019's open consent
question.

- Fine-tuning needs a record whose purposes include `private-fine-tuning`; Speak needs one
  whose purposes include a new purpose, `private-home-host-synthesis`.
- Both need a new execution boundary, `home-host`: the host named by the standing
  home-host approval (D-021). A record for the PC authorizes synthesis nowhere else.
- Subject, approval state and file checksum are checked as on the lab's other rendering
  paths. Each run and each clip records the consent record's digest. A missing, pending,
  revoked or mismatched record refuses the job before a worker starts.
- Clips stream to the desktop app for listening and are not stored on the client. Speak
  clips keep the standing D-019 gives them.

`ConsentRecord.hosted_transfer_allowed` stays `Literal[False]`. The Mac-local fine-tuning
and Speak of D-016 to D-019 are unchanged until the design's phase 5 moves them. D-019's
retention question for Speak clips remains open.

## D-023 — The vault reverses direction, and every copy stays on Cole's hardware

Cole decided on 2026-10-06 (design §13, decision 3) that the offsite copy of the
recordings lives on his own hardware only, never in the cloud. D-020's vault is reused with
its direction reversed: the PC holds the plaintext working library and seals every
generation before it leaves.

- **The laptop** runs the existing `deploy/vault/` server as a stack: ciphertext only,
  nightly, from the design's phase 2.
- **The MacBook Air** runs a second ciphertext-only vault: the same standard-library
  server under launchd, as a dedicated non-admin account, behind its own userspace
  tailnet node `voice-lab-vault-air` (`tag:voice-lab-vault-air`). The Air travels with
  Cole, so it is the offsite copy. It is refreshed weekly; because it sleeps and travels,
  the PC probes it hourly, catches up a missed week the next time it is online, and
  resumes an interrupted push from a per-destination journal of stored chunks instead of
  starting again. The head moves only when a whole generation is stored. From phase 3.
- **A rotated USB drive** holding a `local-directory` vault may follow, only if Cole
  asks.

Each destination has its own binding, approval, state, manifest chain and ledger. The
library key is created on the PC as a root-owned encrypted systemd credential, with
D-020's offline recovery sheet; no vault host holds it. `memos/phone/` is copied as a new
`memos` class, because Apple's iCloud sync of the phone is a cloud service and is not
counted as a backup.

Residual risk, accepted: the PC and the laptop are on one site, so the Air copy is the
only real offsite copy, and only while the Air is away from home; it can lag a week or
more. If the only recovery sheet is kept at home, losing the house loses every copy, so a
copy of the sheet must be kept elsewhere. Losing the Air costs a copy, not privacy. D-020's
limits (sizes and times visible, a host can withhold or delete) apply to every
destination. The cloud-bucket option the design first recommended is withdrawn (D-028).

## D-024 — One intake protocol, one validator

voice-journey's resumable HTTPS intake protocol (chunked uploads with `Content-Range`, a
finalize request) becomes the transport for every capture, prompted takes and measurement
sessions alike. voice-lab's strict validator, run by the network-less intake publisher, is
the only publisher of raw audio; the API writes only `incoming/`. rsync capture sync is
retired with the Mac mini.

## D-025 — The desktop app replaces the Swift app, the legacy Qt desktop and the React SPA

After the design's phase 4 acceptance criteria, one `scshafe-qt` desktop app for macOS and
Linux replaces the Swift app, the legacy Qt Widgets desktop and voice-journey's React SPA.
D-015's continuity evidence contract is kept: the Swift app stays the macOS
prompted-capture path until a PortAudio capture worker passes the recording acceptance
matrix on both platforms.

The phone gets a server-rendered, read-only web view behind Pocket ID. Cole decided on
2026-10-06 (design §13, decision 8) that it plays no audio: it shows summaries only and
serves no recording, transcript or lyric.

## D-026 — Model runtimes on ROCm: spike, then decide

Candidates on the PC run their upstream PyTorch implementations on ROCm, with new runtime
and weight locks; MLX results stay valid historical evidence but are never ranked with
ROCm results in one bake-off. Each model gets a spike on `gfx1030` before phase 5 work
(design §13, decision 5). For a candidate that fails on the GPU, the fallbacks are, in
order: fp32 on the GPU; the PC's CPU; the Air's Apple runtimes, with a transfer approval
naming the Air and the payload digest; otherwise it is dropped from the matrix with the
reason recorded. No hosted GPU is used (D-028).

Amends D-007 (the official Chatterbox runtime is now the evaluated one) and D-008 (VRAM,
not Metal, is the residency constraint).

## D-027 — MIT licence

Cole decided on 2026-10-06 (design §13, decisions 1 and 4) that voice-lab is released
under the MIT licence, copyright "2026 voice-lab contributors", the form mailroom-desktop
and voice-journey-capture use. "Cole and Chloe" is dropped: "Chloe" is an agent persona,
not a legal person. `LICENSE` and `pyproject.toml` (`license = "MIT"`, authors
"voice-lab contributors") changed the same day. This settles the blocking item of
[docs/PUBLICATION-SCREEN.md](docs/PUBLICATION-SCREEN.md).

voice-journey has no licence file; Cole owns its code. Its code becomes MIT when it is
imported in the design's phase 3, and its analysis code stays in the public repository.
voice-journey-capture is already MIT. Third-party texts in the tree keep their own
notices (the CMU ARCTIC `COPYING`, the cited Harvard sentences), and model weights, which
never enter the repository, keep their own licences.

## D-028 — Free, and on the owner's own hardware

Cole decided on 2026-10-06 that everything in the platform is free and runs on his own
hardware: no cloud service and nothing paid. Concretely:

- no object storage or other cloud copy of any data, ciphertext included (D-023);
- no hosted or rented GPU, paid or free (D-026);
- no paid model, API or service;
- no paid Apple Developer ID and no notarization: the macOS app is ad-hoc signed, and its
  install page documents Gatekeeper's first-run prompt and how to allow it (System
  Settings → Privacy & Security → Open Anyway). Each new ad-hoc build asks for microphone
  permission again;
- no hosted CI beyond what is free for a public repository.

Allowed, because they are free and carry no private byte: GitHub, with Actions on the
public repository and GitHub Packages for public packages; PyPI; one-off, explicitly
flagged downloads of public code and weights at pinned revisions; and the fleet's existing
tailnet on Tailscale's free plan as the transport, whose coordination server sees node
metadata and never payload. SECURITY.md's approval rule for hosted GPU use stays as a
rule; this decision means the platform has no such use.

## D-029 — One rename, in phase 6

Cole decided on 2026-10-06 (design §13, decision 2) to rename the distribution
(`cole-voice-lab` → `voice-lab`), the package (`cole_voice_lab` → `voice_lab`) and the
environment variables (`COLE_VOICE_LAB_*` → `VOICE_LAB_*`) in one breaking release in the
design's phase 6. That release still reads the old variables as a fallback; the next
removes them. Stored identifiers never change: the `cole-voice-lab.<record>/vN` schema ids
and the `cole-voice-lab-common-v1` normalization id stay for ever, and old evidence stays
verifiable because it names the code it ran.

## D-030 — Import both histories

Cole decided on 2026-10-06 (design §13, decision 11) to import the histories of
`voice-journey` (its public, single-root history; the older private archive stays where it
is) in phase 3 and `voice-journey-capture` in phase 4, by `git subtree add`-style merges
into prefixes, each in one reviewed PR. The publication screen's method runs on each
imported history first. Both repositories are archived after phase 6, pointing at
voice-lab.

## D-031 — Standard-library HTTP server, Python 3.12 throughout

Cole decided on 2026-10-06 (design §13, decisions 9 and 10):

- The `/api/v1` server and the web view use the standard library's `ThreadingHTTPServer`,
  as `storage_server.py` does, with strict Pydantic request and response models and JSON
  refusals with stable codes. No ASGI framework; JSON Schemas are generated from the
  models into `schemas/api/v1/`.
- The desktop app and the backend are both on Python 3.12. `scshafe-qt` widens to
  `requires-python = ">=3.12"` in its own release.

## D-032 — The `pc` host, its URL, and host units through the runner lane

Cole decided on 2026-10-06 (design §13, decisions 12 and 14):

- voice-lab's `dev.toml` names `host = "pc"`; scshafe-dev resolves `pc` as an alias of the
  machines.toml registry key `arch`.
- The stack's node is `voice-lab`. For 90 days after phase 3's cutover the old
  `voice-journey.<tailnet>.ts.net` answers with a `308` redirect to the same path on
  `voice-lab.<tailnet>.ts.net`, from a stripped-down voice-journey stack that serves no
  data; then it is removed.
- The runner lane's deploy entrypoint installs the released wheel into a sealed,
  content-addressed release and installs and enables the stack's systemd host units
  (intake publisher, job runner, vault copiers). This extends infra's signed host tree and
  needs Cole's approval of its manifest hash before any host runs it; there is no hand
  install.

## D-033 — Phase 2 layout: one lab subvolume, and raw files stay owner-read-only

Recorded 2026-10-06 while building the design's phase 2, as deviations that reality forced
on two details of the design (§4.1, §9 phase 2 acceptance); the owner reviews them with the
phase.

- **The snapshot unit is the lab subvolume, not `raw/` alone.** The design makes `raw/` its
  own btrfs subvolume. But the unchanged validator publishes by renaming a checked copy
  from `tmp/` into `raw/` in one atomic step, and requires `incoming/`, `tmp/` and `raw/` to
  be one filesystem (`intake._same_filesystem`, `st_dev`); on btrfs every subvolume has its
  own `st_dev`, and a rename across subvolumes fails (`EXDEV`). So `/data/voice-lab/lab`
  (the whole asset root) is the subvolume, and the read-only snapshot taken after every
  pass that changed `raw/` (and nightly) is of the lab. `raw/` only ever grows, so the newest
  snapshot holds every accepted original; older snapshots are pruned after 14 days. The
  property the design wanted, that even a root mistake in `raw/` is recoverable from a
  read-only snapshot, holds.
- **Accepted raw files are `0400` in `0500` directories**, as `intake.py` has always made
  them (D-003), not the `0444`/`0555` the design's text names. Both are read-only; the
  stricter mode suffices because every reader (the API container and the host units) runs
  as the owning `voice-lab` user. The acceptance check is "read-only", verified as `0400`.

## D-034 — Per-destination vault records and tokens; the Linux credential keychain

Recorded 2026-10-06 while building the vault side of the design's phase 2 (§4.3), as the
concrete shape of D-023's "each destination has its own binding, approval, state, manifest
chain and ledger" and of the design's "library key as a root-owned encrypted systemd
credential". The owner reviews it with the phase.

- **Records per destination.** A destination is a short stable id (`laptop`, `air`;
  `[a-z][a-z0-9-]`, at most 32 characters), also the vault unit's instance name. Everything
  that describes one vault lives under `provenance/vault/destinations/<destination>/`: its
  binding, approvals, `state.json`, manifest copies, ledger, request log and `.push.lock`.
  The design wrote `provenance/vault/<destination>/`; the extra `destinations/` level keeps
  every destination name apart from `library.json` and lists the destinations in one
  directory. `library.json` (the library identity) and the library key stay one per
  library. Each destination keeps D-020's rollback, fork and ledger checks unchanged, and a
  push to one never takes another's lock, so an asleep Air never holds back the laptop.
- **Records name their destination.** `StorageBinding` and `VaultDestinationApproval` gain a
  `destination` field (schema `v2`): a binding or approval kept or used under another
  destination's name is refused, and an approval covers only a binding of its own
  destination.
- **Tokens per destination.** Two vault servers never share a token. The key stays
  `private.cole.voice-lab.vault.<library_id>.key`; a destination's tokens are
  `private.cole.voice-lab.vault-destination.<destination>.<library_id>.token` and
  `….read-token`. The destination comes before the library id, under a prefix of its own,
  because systemd's `ImportCredential=` accepts only a trailing `*`: the unit imports
  `private.cole.voice-lab.vault.*` (the key) and
  `private.cole.voice-lab.vault-destination.<destination>.*` (its own tokens, and no other
  destination's), with no library id written into the unit.
- **The Linux keychain.** `keychain_client()` is the login Keychain on macOS and, elsewhere,
  `SystemdCredentialKeychain` over `$CREDENTIALS_DIRECTORY`: an item is the file named by
  its service, read with the Keychain's rules (one plain secret word, no secret in a
  message). The operator creates the key (with D-020's recovery sheet) and each
  destination's tokens once, as `voice-lab`, into a private staging directory named by
  `CREDENTIALS_DIRECTORY`, encrypts each item with `systemd-creds encrypt --name=<item>`
  into the root-owned `/etc/credstore.encrypted/`, and removes the staging directory. Under
  systemd the directory is read-only, so no unit can add or replace an item; outside a unit
  that imports them, a command that needs a secret refuses and says how to load them. No
  new environment variable or flag was invented: `CREDENTIALS_DIRECTORY` is systemd's own.
  This is `ImportCredential=` rather than the design's `LoadCredentialEncrypted=`, which
  would need the library id written into the unit.
- **The copier units** are one fixed-name pair per destination, not a template (infra's
  host-unit validator refuses templates for now): `deploy/host/voice-lab-vault-laptop.service`
  (`vault push --destination laptop`, then `vault verify`, as `voice-lab`, tailnet-only
  network) and its nightly `voice-lab-vault-laptop.timer`; the Air adds its own pair in
  phase 3. *Amended 2026-10-06:* infra's host-unit validator also refuses
  `ImportCredential=`, so the pair moved to `deploy/host-manual/` and is installed by hand
  until infra has a rule for credentials (docs/design/PHASE-2-LOG.md).
- **The restore drill** is one command, `vault drill`: a restore through the bound
  destination into an empty target, then every restored file read back from disk and
  re-hashed against the authenticated manifest (size, SHA-256 and mode). `vault restore`
  already proves each file as it reassembles it; the drill also proves what landed on disk.
- **No migration.** D-020 was never deployed: no binding, approval, state, ledger or key
  exists anywhere, so the old flat layout and the `v1` binding and approval are simply
  refused, not migrated. A host that copies to one vault (the Mac's recording service, its
  native fixtures, every command run without `--destination`) uses the destination
  `default`.
- **Not changed here.** The approval's tag is still `tag:voice-lab-vault`; the Air's
  `tag:voice-lab-vault-air` and its resume journal are phase 3. `finetune archive` and
  `restore-run` still use the `default` destination; moving them to the PC is phase 5.

## D-035 — Phase 3 deviations

Recorded 2026-10-09 while building the design's phase 3, as the deviations that the
owner's phase-3 brief or the facts forced on the design; the owner reviews them with the
phase (docs/design/PHASE-3-LOG.md).

- **Both histories in phase 3.** The brief imports voice-journey-capture's history now,
  not in phase 4 (D-030). It lands in `apps/desktop/`, as design §5.2 says, unchanged and
  outside voice-lab's own lint, type and test tooling until phase 4 makes it the desktop
  app. The separate repository stays the live capture client until then.
- **`/view/` instead of the SPA on the PC.** Design §9 phase 3 serves voice-journey's
  React SPA unchanged from the PC. Building it needs the private `@scshafe/ui` package,
  which a public repository cannot ask for (§6.5) and the PC's runner cannot read. The
  PC serves the read-only web view of §6.5 (summaries only, no audio, D-025) from phase
  3; the SPA stays on the laptop until the cutover and is not imported. The acceptance
  item "the four SPA views work from the PC" reads "`/view/` works from the PC".

## D-036 — The Air vault as built

Recorded 2026-10-09 while building the Air's side of the vault (design §4.3, phase 3); the
owner reviews it with the phase. Where the build differs from the design:

- **Records.** The resume journal is `provenance/vault/destinations/<d>/resume.jsonl`
  (D-034's layout, not `provenance/vault/air/`), and every destination keeps one: it costs
  the laptop nothing. Beside it, `checked.json` records when the newest generation was last
  found current and last verified. Both are new local records with schemas
  (`replication-resume-entry`, `replication-check`); nothing else changes version.
- **Age counts from "last found current".** A push that finds the library unchanged does not
  write a generation, so the age of a copy (for the weekly cadence and the 14-day flag) is
  the time since a push last found the head holding the library, not since its generation was
  written. Otherwise an unchanged library would read as stale, and the hourly unit would
  re-read the whole library every hour once a week had passed.
- **Cadence as flags.** The Air's units are a fixed-name pair, `voice-lab-vault-air.service`
  and `.timer` (infra refuses templates; the design wrote `voice-lab-vault@air.timer`),
  running `vault push --if-older-than 7d --skip-unreachable` and `vault verify
  --if-unverified --skip-unreachable`. The probe is the copier's own identity check plus the
  tailnet's `Online` flag and one read of the head; a skipped run exits 0.
- **The approval's tag is chosen at approval time** (`vault approve --tag`), limited to
  `tag:voice-lab-vault` and `tag:voice-lab-vault-<suffix>`. The approval stays `v2`: the
  laptop's existing approval (the old fixed default) parses to the same bytes and digest.
- **Memos** are named `memos/<path>` in a manifest, read from `--memos-root` (a flag of the
  push, so the PC's `v2` bindings stay valid; a binding opts in with the `memos` class), and
  restored only into a `--memos-target` of their own, never into the library.
