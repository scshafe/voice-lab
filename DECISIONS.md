# Decision log

## D-001 — Sibling lab, not ChloeSpeech core

Capture, biometric storage, dataset construction, training, and comparative
evaluation remain in `voice-lab` (named `cole-voice-lab` until 2026-10). ChloeSpeech receives only a validated,
hash-pinned promotion manifest. This preserves ChloeSpeech's provider-neutral
runtime scope.

Superseded 2026-10-11 (D-044): ChloeSpeech is retired and voice-lab is the speech engine.
The promotion manifest and the ChloeSpeech handoff are removed; capture, storage, datasets,
training and evaluation stay in voice-lab as before.

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

Amended 2026-10-11 (D-044, D-045): ChloeSpeech, its adapters and promotion are retired, so
the provider-identity part of this decision no longer applies. The rule that the lab never
relabels an engine's output or fabricates watermark evidence stands for bake-off evidence.

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

Amended 2026-10-12 (D-050): consent records are retired. Fine-tuning needs no consent record,
on the Mac or the PC; Cole's standing decision covers it.

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

Amended 2026-10-12 (D-050): no consent record is required for fine-tuning or Speak, on any host.

Amended 2026-10-12 (D-053): the known gap above is closed another way. Native takes do not
become lab packages; `voice-lab corpus-import` takes them one by one, with their decisions and
trims, into the PC's corpus (D-052), under a transfer approval naming the export plan's
digest. The Mac keeps its takes and still fine-tunes on them (D-051).

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

Amended 2026-10-11 (D-044, D-047): the purpose `private-chloe-speech-rendering` named above is
retired; promotion and ChloeSpeech no longer exist as uses beyond local listening. The
purposes are `private-zero-shot-evaluation`, `private-fine-tuning`,
`private-home-host-synthesis` and `private-assistant-speech`. The retention question for
Speak clips stays open: D-046's retention covers audio rendered through the speech API only.

Amended 2026-10-12 (D-050): consent records are retired. Cole's standing decision permits his own
voice for listening (Speak), fine-tuning, assistant speech and benchmarks on his own hardware, and
no consent purpose or record is checked anywhere. The retention question for Speak clips stays
open.

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

Amended 2026-10-12 (D-050): `ConsentRecord` no longer exists, so its `hosted_transfer_allowed`
field is gone; the rule it expressed stands on its own: human audio never goes to a hosted
service, and hosted or GPU transfers still need their separate destination- and
payload-specific approval. The open question about consent for Speak and fine-tuning is closed:
neither needs a consent record.

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

Amended 2026-10-11 (D-047): a third use on the home host joins these two. Speech rendered for
a registered caller through the speech API (D-046) needs a record whose purposes include
`private-assistant-speech`, with the same `home-host` boundary and the same checks.

Superseded 2026-10-12 (D-050) in its consent part: fine-tuning, Speak and assistant speech need no
consent record, purpose or execution boundary; Cole's standing decision and the pinned subject
replace them. Where they run (the PC's job runner, from phase 5) is unchanged.

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

Amended 2026-10-11 (D-046), for synthesized speech only: audio rendered from Cole's voice
release may reach Cole's own devices, the phone included, through a registered caller of the
speech API. The web view itself is unchanged and serves no audio, and recordings, corpus
audio, transcripts and lyrics still never go to the phone.

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
- **The job runner has no network, so no ntfy.** voice-journey's watcher posted
  notifications to ntfy from inside the server; on the PC it is a network-less host unit
  (§3.2), so notifications are files in an outbox (`state/journey/notify-outbox/`) for a
  sender with tailnet egress, which phase 3 does not build. ntfy remains possible where a
  URL is configured.
- **One state directory for the journey.** The API's verdicts, goals and referee state and
  the watcher's state live in `/data/voice-lab/state/journey/` (the API's only writable
  mount besides `incoming/`); measurement sessions are read from the lab's `raw/`
  (`VOICE_JOURNEY_MEASUREMENT_DIR`), not from a `corpus/measurement/` copy.
- **The capture client's compatibility path** maps a voice-journey measurement session
  onto a capture package the unchanged validator accepts (suite
  `voice-journey-measurement`, subject `cole-self`, the `room-silence` segment as room
  tone). The separate client writes no D-021 ledger entry; that closes in phase 4.
- **No float tolerance.** §5.3 expected one for the filter's features; the port is
  bit-identical (exact integer sums, V8's `log2`/`log10`, sums in JavaScript's order), and
  the fixture parity has 0 differences at tolerance 0.

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

## D-037 — The desktop app's distribution, package and layout

Recorded 2026-10-11 while building the design's phase 4a (§5.2, §6.3); the owner reviews it
with the phase (docs/design/PHASE-4-LOG.md). What the design left open, and one deviation:

- **Names.** The distribution is `voice-lab-desktop` (§6.3), its import package
  `voice_lab_desktop`, its command `voice-lab-desktop`, its Pocket ID client and keychain
  service `voice-lab-desktop` (the public client of phase 2). Its settings file is
  `config.toml` in `~/.config/voice-lab-desktop/` (`~/Library/Application
  Support/voice-lab-desktop/` on macOS), its data in `$XDG_DATA_HOME/voice-lab-desktop/`, its
  environment `VOICE_LAB_DESKTOP_*`. The `voice-lab` rename of phase 6 (D-029) leaves these
  as they are.
- **No server in the code.** The server and the issuer have no default; the app opens on its
  Status screen until both are set. The capture client defaulted to the tailnet's names; the
  publication screen (§5) asked that they become configuration.
- **Layout.** `apps/desktop/src/voice_lab_desktop/`: the app (`app.py`, the composition root;
  `api.py`, `auth.py`, `config.py`, `playback.py`; `screens/`, one model per screen;
  `qml/`), and `measurement/`, voice-journey-capture's modules moved with their history and
  unchanged in behaviour. The measurement capture keeps its data in `<data>/measurement/`;
  `voice-lab-desktop --import-capture-data` copies the old client's level pin, its log, the
  cal file and the sessions there (copies, never moves; refuses to overwrite).
- **Deviation: no `contracts/` distribution yet.** §5.2 puts the `/api/v1` models in a
  separate `voice-lab-api` so that the app need not install the backend. The app depends on
  the backend distribution itself (`cole-voice-lab`, from this repository): its runtime
  dependency is pydantic alone, and the app needs more than the models — the capture
  package, its strict validator and the D-021 approval and ledger (4b) are the backend's.
  Splitting the models out is mechanical and belongs with phase 6's rename and the first
  PyPI release, when a separate install would matter.
- **Stand-ins.** What scshafe-qt does not have yet (§6.4) is a local QML module,
  `VoiceLab.StandIn`, with the API asked of scshafe-qt (PHASE-4-LOG.md, "scshafe-qt asks").
  Each file says it is marked for removal; adopting a release is deleting the file and its
  qmldir line.
- **Verification.** The app's suite runs in `scripts/verify` as its own pytest run
  (`-c apps/desktop/pyproject.toml`, pytest-qt, any Qt warning fails it); the backend's suite
  runs without pytest-qt (`-p no:pytest-qt`), whose hooks break the backend's tests that
  stand in a fake application. Ruff and strict mypy cover `apps/desktop`; the app's wheel
  is built into `dist/` beside the backend's, where the guard inspects it.

## D-038 — Playback in the desktop app

Recorded 2026-10-11 (phase 4a). The Corpus, Journey and Referee screens play recordings the
server streams (`GET /api/v1/recordings/{id}/audio`, only with the server's playback
approval, design §4.2). QtMultimedia is not in PySide6-Essentials, and PySide6-Addons is
several times its size, so the app decodes with PyAV (`av`, an exact pin; its wheels carry
FFmpeg's decoders), shapes with NumPy and plays through PortAudio (`sounddevice`), which the
measurement capture already uses. The bytes stay in memory and are dropped when the take
stops; nothing is written to disk (as D-022 has it for synthesized clips). There is one
player for the app, as the SPA had one `<audio>` element.

The referee's matched playback keeps the SPA's two measures: the server's per-side gain
(`gain_db`) and a low-pass at 8 kHz. The SPA's low-pass was a WebAudio biquad; the app's
removes everything above 8 kHz (an FFT brick wall), a stricter band limit for the same
purpose: a 2019 phone take and a 2026 take stop telegraphing their year through the chain.

## D-039 — The desktop app's PortAudio capture worker and its continuity evidence

Recorded 2026-10-11 (phase 4c; design §6.2, spike S-1). voice-lab-desktop records prompted
takes through a capture worker in its own process, one process per take
(`voice-lab-desktop --capture-worker`; in a frozen bundle the same executable). It opens the
UMIK-1 only (`measurement.devices.choose_umik`) through PortAudio: Core Audio on macOS, with
PortAudio told to fail rather than convert; ALSA `hw:` (or PipeWire's JACK at 48 kHz) on
Linux; mono, 48 kHz, 24-bit, a fixed block of 1,024 frames. The callback only copies a block
and its timing into a bounded queue; a writer thread writes the WAV (stdlib `wave`, s24le)
and reports each written block on stdout, which the screen turns into elapsed time
(D-014).

**The continuity rule.** PortAudio exposes no device sample counter like Audio Queue's
`mSampleTime`; the closest is each block's `inputBufferAdcTime`. Every block must carry a
finite, positive ADC time, and each block's must equal the previous block's plus its frames
at 48 kHz within a declared tolerance (default 64 frames, 1.3 ms; a lost buffer moves it by a
whole block). An input overflow or underflow, a missing ADC time, a deviation beyond the
tolerance or a full writer queue fails the take: capture stops, the partial is kept, and no
sample is inserted or dropped. The tolerance is a spike parameter: the evidence records the
largest deviation seen, so S-1's result can be judged against it (D-040).

**The evidence.** The worker writes `voice-lab-desktop.portaudio-continuity/v1` on success
and on failure: mode 0600, one link, at most 16 KiB, bound to the partial's name, SHA-256,
size and frame count, with the host API, the device, the block size, the tolerance, the
largest deviation, the overflow and underflow counts and the queue's high-water mark. The
parent validates it, inspects the WAV (`inspect_canonical_wav`) and checks the reported frames
against the file before `original.wav` exists; anything missing, malformed, unsuccessful or
mismatched fails closed and keeps both files. The evidence never leaves the capturing machine.

This is not D-015's `capture-continuity/v1`: that contract, its `coreaudio-audioqueue`
backend and the Swift app's worker are unchanged, and PortAudio never writes it. Packages
built from these takes say so: `CaptureDevice.backend` gains `"portaudio"` (an added enum
value; native recording groups still require `"coreaudio"`). Until S-1 passes on a platform
(D-040), the Swift app stays the macOS prompted-capture path and nothing about D-015 is
relaxed (D-025).

Amended 2026-10-12 (D-052, D-053): "The evidence never leaves the capturing machine" no longer
holds. Since Recording v1's step 3 (D-052), a take package carries the evidence to the PC as
`continuity.json`, bound to its WAV and validated again there (`corpus_takes`). D-015's
Audio Queue evidence travels too, inside the `seal.json` of a Swift take's imported-take
package (D-053).

## D-040 — The S-1 harness, and what "S-1 passes" means

Recorded 2026-10-11 (phase 4c). `scripts/s1-portaudio` runs the worker of D-039 on this
machine's UMIK-1: cold starts, warm takes, long takes, stopped and maximum-duration takes. For
each take it keeps the WAV, the evidence, a per-block timing trace and an independent
re-validation from disk. It writes a benchmark report whose rows are computed by the existing
latency benchmark's own rules (`tests/recording_latency_benchmark.py`; rows it does not
measure say `not-measured`), a verification root, runs `scripts/recording_acceptance_matrix.py`
over them, and writes `s1-report.md`.

S-1 passes on a platform when a run there on the UMIK-1, not simulated, has every take
successful at the declared tolerance, no overflow or underflow, an ADC time for every block,
and every take re-validated. The verdict is recorded here with the report's continuity block
and the commit, never with audio. The matrix's Swift-UI parity rows and its physical-gate rows
(permission states, headphone comparison, …) are not the harness's to answer: the owner
judges the app and the hardware against them. A simulated run is only ever "simulated: harness
check only". Running the harness on real hardware is the owner's.

**Verdict, macOS: passed** (2026-10-13; Recording v1 step 8). The run was the owner's, by the
Mac agent on the Air, with the UMIK-1, not simulated, at commit `bbc269f` (`main`, #62). The
report's continuity block:

- Host Darwin 27.0.0 (arm64), PortAudio host API Core Audio, device `UMIK-1 [Core Audio]`.
- Blocks of 1,024 frames; tolerance ±64 frames.
- Takes 43 (20 cold starts, 20 warm, 2 of 60 s, 1 stopped at its maximum duration):
  successful 43, re-validated 43.
- Largest ADC deviation between blocks 1.736 frames (every take between 1.464 and 1.736).
- Overflows 0, underflows 0, an ADC time on every block.

The jitter is about 36 µs, far inside the ±64-frame tolerance. A tighter tolerance would
need its own decision, made with this data. Of the performance rows the harness measures,
`stop_to_sealed_review` missed its target: p95 339.8 ms against 300 ms. The others passed:
record to first confirmed frames p95 223.4 ms (target 250), cold device start p95 244.5 ms
(750), stop reaches the controller p95 0.1 ms (50), worker memory growth 19.2 MB (64 MiB),
and exact timer/frame agreement. These rows are not part of S-1's verdict; the slower stop
is a performance follow-up. The acceptance matrix's 37 other rows (Swift-UI parity, physical
gates) remain the owner's (step 10).

Consequences:

- On macOS, takes recorded by voice-lab-desktop may leave the machine. The owner sets
  `corpus_sync = true` (and `prompted_uploads = true` for the session flow) in that Mac's
  `config.toml`.
- The Swift app stays until the owner's parity sign-off (Recording v1 step 10), and
  docs/S-1.md's Linux run (ALSA `hw:`) is still to do.

## D-041 — Packaging the desktop app (spike S-7)

Recorded 2026-10-11 (phase 4c; design §6.3). **Linux**: the uv tool (`uv tool install
--python 3.12 --with-executables-from cole-voice-lab ./apps/desktop`) plus a per-user desktop
entry, `voice-lab-desktop --install-desktop-entry`: a `.desktop` file whose `Exec` is the
installed command's absolute path, and a 256 px icon derived from the lab's icon, both under
`$XDG_DATA_HOME`; the app names its desktop file (`voice-lab-desktop`) so a Wayland
compositor matches its window. An AppImage only if asked.

**macOS**: `voice-lab.app`, built with PyInstaller by `scripts/build-desktop-bundle` on the Air
(GitHub's free macOS runners once the repository is public). PyInstaller rather than
Briefcase: its wheels pin and hash-check like the rest of `requirements/`
(`requirements/desktop-bundle-macos.txt`: PyInstaller 6.22.3 and its closure), it maintains
PySide6/QML hooks and contrib hooks for sounddevice's PortAudio, PyAV's FFmpeg and keyring, it
fetches nothing at build time, and a onedir `.app` comes from a generated spec; Briefcase
brings its own project layout and fetches its templates when it builds. The bundle id is
`private.cole.voice-lab-desktop` (not the legacy launcher's or the Swift app's, so TCC grants
stay apart); `NSMicrophoneUsageDescription` is set. The bundle is signed ad-hoc
(`codesign --sign -`, no paid Developer ID, no notarization; D-028) with the hardened runtime
and three entitlements: audio input, library validation off (ad-hoc signatures carry no team
id) and unsigned executable memory (cffi's callbacks); `--no-hardened-runtime` is the fallback
if a library will not load. Each build is verified (`codesign --verify --strict --deep`),
its Info.plist linted, zipped and recorded with its SHA-256. The first run of an ad-hoc-signed
app is the install page's (`apps/desktop/docs/INSTALL-MACOS.md`). Nothing of this has run on
macOS yet: building and the first run are the owner's.

## D-042 — Record → Prompts in the desktop app

Recorded 2026-10-11 (phase 4c; design §6.1, D-010 to D-018). The screen records the prompts
of a catalog through the capture worker of D-039, one take per private directory.

- **Catalogs.** The built-in pilot catalogs (`pilot-v2.json` by default, `pilot-v1.json`) or a
  prompt-suite file named by the setting `catalog_file`. Held-out evaluation, physical-test
  and corpus catalogs are refused, as are `procedure` prompts and a suite without room tone.
  The corpus catalog (D-017) needs the PC to serve it (`GET /api/v1/catalog`, design §3.4) and
  its pilot gate; neither is built in phase 4.
- **Batches and ids.** A batch is the chosen prompts plus every room tone, in catalog order
  (D-010); its package names the catalog itself, or a subset suite named by its prompts'
  digest (`native-subset-…` for prompt-suite/v2, `desktop-subset-…` for v1). Session ids are
  `native-desktop-<yyyymmdd>t<hhmmss>z-<6 hex>`: prompt-suite/v2 packages require `native-`,
  and `desktop` tells them from the Swift app's.
- **What stays on the machine.** Sessions and takes live in `<data dir>/prompted/sessions/`
  (0700 directories, 0600 records written atomically, take numbers never reused). Every take
  keeps its history of decisions; a removal can be undone (D-012). A trim is a recipe bound
  to the original's SHA-256; the original is never rewritten, and the preview plays exactly
  the trimmed frames. A take exists only once the recorder validated it; failed and damaged
  take directories are shown, never used.
- **The package.** Finish builds `<data dir>/prompted/packages/<session id>/`: the most
  recently kept take of each batch prompt, its original untrimmed bytes, finalized and
  validated by the lab's own `finalize_capture_session` and strict validator,
  `CaptureDevice.backend = "portaudio"`, `captured_at` the earliest kept take's request time,
  `finalized_at` the session's persisted finish time; rebuilt to the same bytes, never
  overwritten. Trims, take history, unchosen takes and continuity evidence never travel. So
  the PC sees untrimmed takes: carrying edit decisions to the PC is a contract for later
  (before fine-tuning on the PC uses trims).
- **Uploads wait for S-1.** Until spike S-1 passes on a platform (D-040), prompted packages
  stay on the machine: Uploads and Status show them as held. After recording the pass, the
  owner sets `prompted_uploads = true` in that machine's `config.toml`; they then upload as
  class `capture` through `/api/v1/intake` with the D-021 ledger, which the standing home-host
  approval must cover.

## D-043 — A stable local signing identity on Cole's Mac; the Mac agent

Recorded 2026-10-11 (after phase 4; amends design §6.3 and D-041's "ad-hoc signed").

**Why.** The project's agent works on the PC. It cannot build macOS apps, and by design it
cannot reach the Mac. Since the move to scshafe-dev, nothing has built or tested the Swift
app or produced a `.app` from a commit: `scripts/verify` skips the native suites off macOS.
Ad-hoc signing made each build cost Cole a microphone prompt. macOS binds a TCC grant to the
code's designated requirement. An ad-hoc signature's requirement is its code hash, which
every build changes.

**The Mac agent.** Cole keeps a workspace on the Mac (`~/voice-lab-mac`: a clone and a
`CLAUDE.md` copied from `docs/mac-agent/CLAUDE.template.md`) and starts an agent there. It
runs `scripts/mac-refresh`. One command fetches a commit (`main`, or a pull request's head),
runs `scripts/verify` with the Swift suites, builds the Swift app and voice-lab-desktop,
and, when asked, installs both into `/Applications`. The app each install replaces is kept,
and every run leaves a report the agent can post on a pull request. The Mac agent never
pushes, opens pull requests or merges, and never reads recordings. Installs default to
commits on `main` and never put an older Swift app over a newer one: an older native build
cannot open a library a newer one has opened. Overriding either needs Cole's go-ahead.

**The signing identity.** `scripts/mac-signing-identity` creates, once, a self-signed
code-signing certificate `voice-lab local signing` in Cole's login keychain:
- RSA 3072, ten years, extended key usage codeSigning, not a CA;
- trusted for code signing in the user's trust settings only;
- its key usable by `codesign` and never exported.

Both builders take `--signing-identity`, and `scripts/mac-refresh` passes this identity when
the keychains hold it. A certificate's designated requirement is the bundle identifier plus
the certificate, so a microphone grant survives rebuilds.

Ad-hoc stays the builders' default and the fallback. It is still free: no Developer ID and no
notarization (D-028). Locally built apps are not quarantined, so Gatekeeper is unchanged; a
build copied to another Mac still takes the Gatekeeper steps there, and the identity helps
only on the Mac that holds its key. Anything signed with that key would carry these apps'
microphone grants on this Mac, which is why the key stays in the login keychain. The
hardened runtime's entitlements are unchanged: a self-signed identity, like an ad-hoc one,
has no Team ID.

Nothing of this has run on a Mac yet. The Mac agent's first refresh is the check.

## D-044 — ChloeSpeech is retired; voice-lab is the speech engine

Cole decided on 2026-10-11: "please amend the rules. I want to get rid of `ChloeSpeech` and
move speech generation into voice-lab." voice-lab no longer hands a voice to another
renderer. It generates speech itself, with Cole's own voice (D-045), for callers it registers
(D-046).

- **Removed.** The one-way, manifest-only ChloeSpeech boundary (D-001, AGENTS.md, the
  design's goals and non-goals), `promotion.py` with its `promotion validate` and
  `promotion handoff` commands, and the records that existed only for it: the promotion
  manifest, the ChloeSpeech handoff, the promotion listening summary, the lab verdict, the
  winning-run record and the human-reference provenance record, with their generated schemas
  (`chloe-speech-handoff`, `promotion-manifest`, `promotion-listening-summary`, `lab-verdict`,
  `winning-run`, `human-reference-provenance`). `docs/CHLOESPEECH_HANDOFF.md` is deleted; git
  history keeps it.
- **Kept.** The bake-off (audition, normalization, diagnostics) and blind review stay as lab
  tools for comparing candidates and runs; their records and schemas are unchanged. Blind
  review can still inform which run Cole designates (D-045); it no longer feeds a manifest.
- **Stored identifiers.** The design's non-goal "no rewrite of stored identifiers" is about
  stored records. The repository records no promotion manifest, verdict, winning-run,
  listening summary, provenance record or handoff ever having been produced: CURRENT.md lists
  authoring them as an outstanding human gate, the CLI never authored them, and phase 5's
  bake-off on the PC has not run. If such a file exists in a private asset root, nothing
  reads it any more; its schema id is retired, never reused. The consent purpose
  `private-chloe-speech-rendering` is in every pending consent record `assets init` has
  installed, so the consent model still reads it (D-047).
- **Amends.** D-001 (superseded), D-007 (the provider-identity part), D-019 (the purpose
  list), AGENTS.md's mission and its ChloeSpeech invariant, the design's goals, non-goals,
  components, boundaries and API table (docs/design/VOICE-PLATFORM.md), README, RUNBOOK §7,
  FINETUNE, CURRENT.md and PROGRESS_CONTRACT.md.

Amended 2026-10-12 (D-050): consent records are retired; no consent purpose is read, granted or
required, so the retired purpose's readability no longer matters.

## D-045 — The voice is Cole's own clone, designated as a voice release

Cole decided on 2026-10-11: "The speech generation should explicitly use my personal voice
clone." Promotion is replaced by a **voice release**: Cole designates one finished,
fine-tuned F5-TTS run of his own voice as the voice that speaks.

- **The record.** A strict, checksum-bound `voice-release` record in the asset root
  (`provenance/voice-releases/`), written once and never edited. It names the subject, the
  fine-tune run id and the digest of its `run.json`, the checkpoint the run recorded and its
  SHA-256, the reference clip (its prompt id, its path in the run's dataset and its SHA-256),
  the consent record's id and SHA-256, the runtime identity the run trained with, when it was
  created and who designated it.
- **Designation.** `voice-lab voice release designate --run <id>` validates against the run's
  existing records: the run is finished, its subject is the consent record's subject, it is
  not archived, and its checkpoint and reference clip still hash to what the run recorded.
  It reads no audio beyond what the existing Speak seam already hashes. Designating is an act
  of judgement: a CLI command on the PC that Cole runs, never an API route and never an
  agent's step.
- **Only the release speaks.** Speech through the API uses only the current release. It
  refuses the pinned base weights, a zero-shot reference, another subject, and any run not
  designated. A checkpoint, reference clip or consent record whose digest no longer matches
  the release refuses the render until Cole designates again.
- **Rotation is append-only.** Each designation is a new record that names the digest of the
  one before it; the newest is current, and the history is kept. Revoking the consent record
  stops all API speech without touching the releases.
- **Blind review is optional evidence.** A release may name a blind review (its review id and
  score-sheet digest), checked to exist when given. It is not required: Cole may designate a
  run he has listened to.

Amended 2026-10-12 (D-050): a release names no consent record and designation reads none; it
designates only a run of the pinned subject (`cole-self`), and no caller can name another
subject. A release written before keeps the consent record's id and digest it was written with,
which nothing reads. Revoking consent no longer stops speech: designating another run, or none
being current, does.

## D-046 — Assistant speech over the tailnet API

Cole decided on 2026-10-11: "I'm not familiar with how long the TTS takes — if necessary, we
can make this an asynchronous style that leverages webhooks to communicate completion." The
caller he has in mind is his own agent server, later, which sends reply text to voice-lab and
plays the audio on his phone through his own app. Speech synthesis is therefore allowed over
the API, under these rules (design §3.6, [docs/SPEECH_API.md](docs/SPEECH_API.md)):

- **Tailnet only, free, own hardware.** voice-lab serves synthesis over its tailnet-only
  `/api/v1` to approved callers: text in, audio of Cole's voice release (D-045) out. No public
  endpoint, Funnel stays off, nothing paid, no cloud (D-028 stands). Human recordings still
  never go to a hosted service.
- **A queued job, completed asynchronously.** Synthesis is a job in the one GPU slot, never
  an always-on synthesis endpoint. `POST /api/v1/speech` answers `202` with a job; the
  caller polls it or receives a signed webhook when it ends (webhooks are terminal only). The
  API process only enqueues and serves finished files; the network-less job runner renders;
  delivery of webhooks runs in a network-capable process of its own, never in the job runner.
- **Progressive delivery of finished sentences.** The job runner splits the text into
  sentences and renders them one after another. Each finished sentence is published at once
  as a segment of the job: the job's status lists the ready segments, each can be fetched
  (`…/segments/{n}/audio`), and a per-job event stream (`…/events`, server-sent events:
  `segment.ready`, then the terminal event) announces them. The joined audio is served at
  `…/audio` once the job has succeeded. First audio therefore arrives after about one
  sentence's render rather than the whole text's. Audio is never streamed while it is being
  synthesized: a segment is published only when it is complete and checked.
- **A resident speech worker.** The F5-TTS model and the current voice release stay loaded
  in the GPU slot between jobs, in one long-running worker process on the network-less host
  side, instead of a process per request (a cold start costs about 20 s). It is unloaded after
  an idle time (15 minutes by default) or when the release changes. It needs about 1.3–3 GiB
  of the card's 16 GB, which llama-swap's models share.
- **Reproducible renders.** Sentences are rendered sequentially, never in F5-TTS's thread
  pool, each with a seed derived from the job's seed, which is derived from the text, the
  release and the flow steps. A rerun of the same text with the same release gives the same
  bytes, which phase 5's rerun acceptance needs; upstream's threaded chunking does not.
- **Flow steps.** The number of flow steps (`nfe`) is a configuration setting, 32 by default
  until Cole compares 16 and 32 by ear; each render's ledger entry records it.
- **Registered callers.** Each caller is listed in a caller registry: an id, the Pocket ID
  issuer and subject (and audience) its machine tokens carry, the consent purposes it may
  invoke, an allowlist of webhook URLs (`https` on `*.ts.net` tailnet hosts only), a path to
  its webhook secret file, and its limits (queued jobs, text length, daily characters). A
  token that matches no caller is refused; a caller's token reaches the speech routes and
  nothing else.
- **Where audio may go.** Rendered audio leaves the PC only to a registered caller, and may
  reach Cole's own devices through that caller. This amends D-025 for synthesized speech only.
- **The render ledger.** Every render is recorded in an append-only ledger: job id, caller,
  the SHA-256 and length of the text (never the text), the release id, the consent digest,
  the model and runtime fingerprint, the output digest and the timings.
- **Retention.** Rendered audio is kept for a bounded time, 7 days by default and
  configurable, then deleted; the job then reads `expired`. The ledger stays.
- **Amends** SECURITY.md ("Deliberately avoided: long-running synthesis endpoints" becomes
  "an always-on synthesis endpoint, audio streamed while it is synthesized, or an endpoint
  beyond the tailnet"), the design's non-goal "No long-running synthesis endpoint", D-019's
  process per Speak request where the speech API is concerned (the resident worker), and
  D-025 as above.

Recommended in this decision, for Cole to confirm in review:

- **No watermark, for now.** Rendered audio carries no inaudible watermark. Provenance is the
  render ledger and the output digests: any file can be checked against the ledger. F5-TTS
  has no watermark of its own, and adding one would add a dependency to the pinned runtime
  for a listener who is Cole. This is revisited before any rendered audio could leave Cole's
  own devices (for example, a message to someone else), together with SECURITY.md's
  disclosure rule.
- **Retention 7 days**, as above; the job's status record is kept 30 days, the ledger for
  ever.
- **Sharing the GPU slot.** Nothing preempts a running job. While a fine-tune or audition
  holds the slot (a fine-tune takes hours), `POST /api/v1/speech` is refused at once, `503
  gpu_busy` with `Retry-After` from the holder's expected end, so the agent can answer in text
  instead of waiting. The resident speech worker holds the slot while its model is loaded, but
  yields it: a long job that wants the slot says so, and the worker unloads as soon as it has
  no speech job in hand (at most one job's wait for the long job), rather than only after its
  idle time. Queued speech goes before a long job that has not started. A speech job not
  started within 10 minutes of its submission fails `queue_timeout`.
- **Format.** WAV, 24 kHz mono 16-bit, the synthesizer's own output. The API image has no
  ffmpeg; Ogg Opus is reserved and refused until the job runner encodes it with the host's
  ffmpeg.
- **Limits.** 1–2,000 code points per request (as Speak), at most 4 queued or running jobs
  per caller, 50,000 characters per caller per UTC day, 32 jobs queued in all.

Measured on 2026-10-11 on the PC (spike S-2: RX 6900 XT, `gfx1030`, ROCm; F5-TTS 1.1.22 with
the pinned base weights and F5-TTS's public English reference clip, so no recording of Cole's).
fp16 runs natively, without an HSA override. Warm real-time factor about 0.70–0.78 at 32 flow
steps and 0.35–0.39 at 16: one 15-word sentence takes 6.2 s at 32 steps and 3.1 s at 16; 62
words 23.6 s and 11.8 s; 206 words 84 s and 42 s. A cold start is about 20 s (7.4 s imports,
2.4 s loading, and a first request that warms the kernels). The CPU is about 19× slower than
real time, unusable; fp32 on the GPU is only 8–16% slower than fp16. torch 2.6.0+rocm6.2.4
and 2.14.0+rocm7.2 are within 2% of each other; PyPI's torchcodec 0.16.0 x86_64 wheel needs
CUDA 13, so the `+cpu` build is used, and F5-TTS's README pin `torch==2.9.1+rocm7.2` does not
exist. Upstream renders a long text's chunks in a thread pool sharing the global RNG: such
outputs differ between runs and peak VRAM rises to 7.3 GiB, against 1.6 GiB and identical
bytes when chunks are rendered sequentially with their own seeds. These figures scale with the
reference clip's length and the speaking rate; a release of Cole's own voice must be measured
again. Evidence (SHA-256 of the spike's records, which stay on the PC): `a-fp16.json`
c4f22f64e9dd05ace20a2bef2a9b041f9792fa955fe731c2fa246be29336ff5d, `b-fp16.json`
0f03cc2f998bbffaf37032852c94f1e7f0cf650d8deef743ede15eaf0789b5ea, `a-fp32.json`
122f3151d76941eae51846c28e939f0b023f8d63e7bfcd8bc7dd54bd3d1fa9ff, `cpu-a.json`
911a39ef40d17b50bd96dd8a8dc0f5621c85f9e45582989f5f8643b19a423246, `a-cold1.json`
198876f0f0dfd81d60650cb7b4e1cd2fb0685ecd7aa5342146f25dfd72e8b7ae, `pip-freeze-ft.txt`
8fd91a33f988720dadd128e48b7e0c2d7e1b4d4171d797526efef823d0b95885, `bench.py`
41d1017743eac551a7931ff55971ee17dc72fc5a3171f980a7e114b7b6dcef2f. The spike's fp16 attention
patch is not adopted. The PC's runtime lock (hash-locked, based on that freeze: torch
2.14.0+rocm7.2, torchcodec 0.16.0+cpu) is a follow-up.

Amended in review of the implementation (2026-10-11, before anything was deployed): the
ledger and the job record name the text by a keyed digest (HMAC-SHA256 under a per-install key
in the speech state) rather than a plain SHA-256, which a short reply's guess would match; a
caller sees a failure's code and a fixed sentence, the particulars staying in the runner's
journal; consent and the release are checked again before each sentence, so revocation stops
a running render; a webhook whose secret is missing is retried rather than given up, while a
caller or URL the registry no longer allows is refused for good; and an attempt has a total
deadline of 10 seconds. docs/SPEECH_API.md has the details.

Amended 2026-10-12 (D-050): the ledger names no consent digest, and neither the API at submit
nor the job runner before each job or sentence checks a consent record; before each sentence the
runner still checks that the release is current, so a new designation stops a render. A caller's
`allowed_purposes` stays as the caller's scope in the registry.

## D-047 — Consent for synthesis on the home host

Recorded 2026-10-11 with D-044 to D-046, from Cole's decisions above and D-022.

- **Purposes.** `private-chloe-speech-rendering` is retired. The purposes are
  `private-zero-shot-evaluation`, `private-fine-tuning`, `private-home-host-synthesis` (Speak on
  the home host, for Cole's listening; planned by D-022) and `private-assistant-speech`
  (synthesis for registered callers that deliver audio to Cole; new).
- **Boundary.** The `home-host` execution boundary of D-022 is added: the host the standing
  home-host approval names. A record for it authorizes nothing anywhere else.
- **API synthesis** requires an approved, checksum-bound consent record with a matching
  subject whose purposes include `private-assistant-speech` and whose boundaries include
  `home-host`, checked before the job is queued and again before the worker starts; each
  render records the record's digest in the ledger.
- **Stored records.** The consent model still reads the retired purpose, so a record written
  before today (every pending record installed by `assets init`) can be rotated into approval
  or revoked; nothing grants or requires it. `assets init` keeps an existing untouched pending
  record rather than refusing it. The pending template now lists the four purposes and the
  three boundaries.
- **Cole's step.** Approving a consent record stays Cole's own act (`consent preview`, then
  `consent approve-self`); no agent creates or approves one.

Superseded 2026-10-12 (D-050): consent records, their purposes and the `home-host` execution
boundary are retired; Cole's standing decision and the pinned subject replace them, and no one
approves a consent record any more.

## D-048 — Model families

Cole asked on 2026-10-11: "make sure we have designed the backend such that we should
theoretically be able to add multiple models to test with, assuming the audio corpus we are
recording works for that type of model … This doesn't need to be done in a configurable way
for the user, I just mean in terms of the code design, because ultimately, once we have a full
corpus of 800ish recordings, i'll be curious to try out different models and compare their
training time, inference time, and quality."

The speech path stops assuming F5-TTS. F5-TTS stays the one family registered, and nothing
about what speaks today changes.

- **A model family** is one kind of fine-tunable TTS model. In the speech path it is an entry
  in a small registry (`speech/families.py`) that gives the family's backend, its text
  segmentation limit (the longest text its model takes in one pass), its native sample rate,
  whether it honours a seed, and how a designated run's files are found and checked again.
  The job runner and the routes know only the backend port and a verified release.
- **The release is family-neutral.** A voice release names its `model_family`, binds the
  run's `run.json`, and lists every file the family needs by role (`files`: for F5-TTS
  `checkpoint` and `reference`), with a family-specific section (`model`, a union tagged by
  `family`: for F5-TTS the reference prompt, its transcript's digest and the training
  runtime). A family with no reference clip, with a speaker embedding or profile file, or
  with no notion of flow steps fits without a change to the common fields: it adds a member to
  the `model` union and one to the render settings' union. The render fingerprint and the
  ledger entry name the family too, the files loaded by role and digest, the family's native
  sample rate, whether it seeded, and its render settings (for F5-TTS the flow steps). The
  job's seed is derived from the digest of those settings rather than from F5-TTS's flow steps.
  No release or ledger record existed when the shapes changed, so their schema versions stay
  `v1`.
- **One output format.** Whatever a family renders natively, the API delivers 24 kHz mono
  16-bit WAV (D-046's format): a backend whose model renders at another rate resamples in its
  own runtime, and its fingerprint records the native rate. One format keeps the callers, the
  segment checks and the joining simple, and a comparison of families' quality belongs to the
  lab's bake-off and blind review, not to the assistant's API. A per-release rate was
  considered and rejected for that reason.
- **The corpus is model-neutral.** Takes are 48 kHz 24-bit mono with their prompt text; each
  family derives its own training format from them (sample rate, normalisation, transcript
  form), as the F5-TTS fine-tune does today with its 24 kHz dataset.
- **Not configurable for the user.** Which family speaks is decided by designating a release
  of it (D-045); the configuration only says where each family's runtime is (`runtime`, tagged
  by `model_family`). There is no per-request choice of model.
- **Adding a family** is one runtime lock, one training adapter, one speech backend and one
  registry entry ([docs/MODEL_FAMILIES.md](docs/MODEL_FAMILIES.md)). Training and comparing
  families (training time, inference time, quality) is the follow-up of this decision.
- **Amends** D-045 (the release's fields: files by role and the family's section instead of
  F5-TTS's checkpoint, reference and runtime fields) and D-046 (the segment limit is the
  family's; the seed comes from the render settings' digest; the backend is the family's).

## D-049 — Training and comparing model families

Follows D-048, from the same request of Cole's (2026-10-11): "once we have a full corpus of
800ish recordings, i'll be curious to try out different models and compare their training
time, inference time, and quality."

- **Training goes through a family's adapter.** The fine-tune pipeline selects the corpus
  itself and does everything after through the training adapter of the hyperparameters'
  family (`training.py`): the dataset, the runtime, the training, the progress, the
  checkpoints and the listening samples. F5-TTS is the first adapter, over the code that
  trained every run so far; its records keep their schemas and their bytes. Another family's
  run records `FamilyHyperparameters` and `FamilyRuntimeIdentity`, and its dataset may have its
  own sample rate.
- **A comparison record per measurement of a run** (`provenance/model-comparisons/`,
  write-once): the training cost (wall time, device, epochs and updates, dataset digest), an
  optional inference benchmark through the speech backend port on a fixed held-out text set
  (load time, time to first audio, real-time factor, peak memory, determinism), and an optional
  link to the blind review that judged the run. Quality stays blind listening's (AGENTS.md);
  the numbers are diagnostics.
- **Family-neutral common code.** A family builds its own settings (training and listening)
  from options, chooses its own reference clip, owns its runtime's pins, and declares the
  pinned-weight link the vault records by pin; the pipeline holds the hyperparameters, the
  adapter and the options to one family before it reads the corpus, and a run's
  hyperparameters and runtime must be of one family. Speak renders F5-TTS runs only, so its
  `latest` and `base`, and the runs the app lists, never include another family's run. What
  still has F5-TTS's shape is listed in docs/MODEL_FAMILIES.md.
- **A fair comparison.** Training and benchmarks record the host they ran on (CPU, memory,
  GPUs with driver and VRAM, ROCm or MPS); `families compare` composes each run's newest
  training summary, benchmark and review and flags runs on another corpus, unlike hosts and a
  GPU shared with another process (an advisory read without privileges). Benchmark seeds come
  from the run, its files' digests, the text set and the render settings, so reruns render
  the same. Peak memory names how it was measured. A review link is made from the score sheet
  itself, verified against the sealed review package, and only for a review that scored the
  run.
- **Benchmarks render in the speech runner's sandbox.** On the PC a benchmark is one start of
  `voice-lab-speech-benchmark.service` (no network, only the GPU's devices); the command
  refuses to render unless the unit's marker is set and only loopback is visible (a claim
  about the process's network, not proof of every fence), or
  `--i-render-outside-the-sandbox` accepts it, and the record says so. Its segments render in
  its own scratch under the speech state, never in the lab. A long job's want of
  the GPU slot names its process, boot and wait, and goes stale when any of them is gone.

Recommended in this decision, for Cole to confirm in review:

- **The benchmark's consent purpose is `private-home-host-synthesis`** on `home-host` (D-022,
  D-047: synthesis on the PC for Cole's own evaluation), not `private-assistant-speech`: a
  benchmark renders a run, designated or not, for measurement, and its audio is hashed and
  deleted, never kept, served or played. It runs only on an approved record that names that
  purpose, through an unwritten release preview, so it never designates anything.
- **The text set is fixed and public** (`benchmark-v1`, six sentences of different shapes),
  refused for a run that trained on any of them, and never edited: a new set is a new id.
- **Benchmarks hold the GPU slot** as kind `benchmark`, a long job: the speech API answers
  `503 gpu_busy` while one runs, as for a fine-tune.


Amended 2026-10-12 (D-050): the benchmark needs no consent record (`--consent-id` is removed);
its unwritten release preview still renders only a finished run of the pinned subject.

## D-050 — A standing decision for Cole's own voice; consent records are retired

Cole asked on 2026-10-12: "I feel like i've had to give my consent to clone my own voice many
times now. Do i technically need to keep doing this? Can we remove this so that these
processes can just be handled more smoothly?" After the options were laid out, he decided:
"yes, remove them entirely after #46 and #47 merge".

- **The standing decision.** This decision is Cole's recorded permission, standing until he
  records another: his own voice (the subject `cole-self`) may be used for fine-tuning, for
  listening (Speak), for assistant speech delivered to his registered callers, and for
  benchmarks and bake-offs (zero-shot evaluation, the model comparison), on his own hardware.
  It replaces every per-purpose consent record (D-019, D-022, D-047).
- **Removed.** `consent.py` and its `voice-lab consent` commands (`install-pending`, `preview`,
  `approve-self`, `status`, `revoke`), the pending record `assets init` installed, the
  `ConsentRecord` model and its `consent` schema, and every check of a consent record:
  reference publication and validation, dataset building and validation, the offline model
  workers, the audition's re-hash of the consent file, voice release designation
  (`--consent-id`), the speech API's check at submit (`503 consent_unavailable`), the job
  runner's checks before each job and each sentence (`consent_changed`), and the model
  comparison's benchmark (`--consent-id`, `VOICE_LAB_BENCHMARK_CONSENT_ID`). Fine-tuning and
  Speak never read a consent record in code (D-022's requirement waited for phase 5 and was
  never built), so for them only the documents change. The speech setup no longer approves a
  `cole-self-v2` record, and nothing is designated again because consent was renewed.
- **What remains, and why it is enough.** A consent record answered one question: may this
  subject's voice be synthesized for this purpose? The lab synthesizes one voice, Cole's, and
  the code makes that so:
  - *The subject is pinned in code.* `models.COLE_SUBJECT` is `cole-self`. The speech
    configuration's `subject_id` is `Literal["cole-self"]` and `release.SPEECH_SUBJECT` is the
    same constant. Designation refuses a run of any other subject, and no caller of
    `designate` (the CLI, the benchmark) can name another subject any more (strengthened
    today). The API and the job runner refuse a release of another subject. Reference
    publication and validation, dataset building and validation, and the offline workers now
    refuse any subject but `cole-self` too (new today: before, a consent record had to name the
    source's subject, but any subject could have one). A diagnostic subject is never the
    pinned subject.
  - *Only the designated release speaks* (D-045): never the base weights, a zero-shot
    reference or another run; its files are re-hashed before every job, and before every
    sentence the runner checks it is still the current release.
  - *The render ledger* (D-046) records every render, with the text's keyed digest only.
  - *Tailnet only, free, own hardware* (D-028): no public endpoint, nothing paid, no cloud.
  - *Human audio is never sent to a hosted service.* The separate transfer-approval rule stays:
    the hosted-transfer approval a fine-tuning transfer plan needs, the standing home-host
    approval (D-021) and the vault destination approvals (D-020). It was never a consent
    record about Cole's voice: it governs where audio may go, not whether his voice may be
    used, so it is kept whole.

  No one else's voice is recorded, stored or synthesized; diagnostic subjects are
  physical-test material and are never synthesized. A record of Cole's consent to his own
  voice, approved by Cole, added steps but no protection the pinned subject does not give.
  Fine-tuning and Speak work on the recording library's subject (`cole-self` by default) as
  they always have; a run of any other subject could never be designated, served or
  benchmarked.
- **Stored records.** Consent records already under `provenance/consents/` (and `history/`)
  stay as history, untouched, and nothing reads them. The vault's classification keeps their
  rules (`consents`, `consent-history`) and replication keeps copying them, so a pass never
  meets a file it cannot classify; the private-asset guard still treats an approved consent
  record as private evidence. Every record written before today that names a consent record
  still validates, byte for byte: reference and dataset manifests (`consent`), worker
  requests and audition plans (`consent_path`, `consent_sha256`), worker model evidence
  (`consent_sha256`), voice releases, render-ledger entries and model-comparison benchmarks
  (`consent_id`, `consent_sha256`). Those fields are optional and absent from every new record
  (never `null`, and the generated schemas offer no `null`), and a pair is present whole or
  not at all; no consent record they name is read. A worker running a trial of an audition
  plan written before D-050 carries the request's consent digest into its evidence and result
  unread, so such a plan can still finish its remaining trials. Republishing a reference or
  dataset published before D-050 stays idempotent: the comparison ignores the old binding.
  Schema ids stay `v1`, since every stored record still validates. The job failure code
  `consent_changed` and the refusal `consent_unavailable` are removed outright: no job, ledger
  entry or caller exists yet (speech is not live), as when D-048 changed those shapes.
- **The caller registry's purpose stays.** A caller's `allowed_purposes`
  (`private-assistant-speech`, D-046) is the caller's scope in the registry, not a consent
  record.
- **Supersedes** D-047. **Amends** D-016, D-018, D-019, D-020, D-022, D-044, D-045, D-046 and
  D-049; AGENTS.md's invariant "Recognizable-human synthesis requires a checksum-bound reference
  and an approved, checksum-bound consent record with a matching subject and purpose" (now the
  standing decision and the pinned subject); SECURITY.md; the design
  (docs/design/VOICE-PLATFORM.md); README; docs/SPEECH_API.md, docs/FINETUNE.md,
  docs/MODEL_FAMILIES.md, docs/REFERENCES_AND_DATASETS.md and docs/RUNBOOK.md; and
  deploy/speech/README.md and deploy/host-manual/.

## D-051 — Recording v1: the owner's decisions

Recorded 2026-10-12. The owner's answers to "what is this application", and the plan they set
([docs/design/RECORDING-V1.md](docs/design/RECORDING-V1.md)):

- The Swift app (Cole Voice Recorder) is the reference recording interface. voice-lab-desktop's
  prompt studio is rebuilt to its layout and behaviour, and **replaces the Swift app** on
  macOS once spike S-1 passes there and the studio reaches parity. The Swift app is then
  frozen with a tag (D-025).
- **The speech corpus lives on the PC** (catalog, takes, decisions, trims, coverage) and is
  served over `/api/v1`.
- **Singing recordings are held generically on the backend**: a singing recording is a
  recording, whatever made it. For now Voice Memos is its only source. A **singing studio**,
  similar to the prompt studio but distinct from it, follows v1.
- **Training**: fine-tuning stays on the Mac and trains on the takes already there. PC
  fine-tuning (phase 5) follows v1. Takes recorded into the PC's corpus are not trained on
  until then.

## D-052 — The corpus on the PC: one writer, a read-only API, take by take

Recorded 2026-10-12. Design: [docs/design/RECORDING-V1.md](docs/design/RECORDING-V1.md) §3–§4.

**Writer and reads.** The API stays read-only on the lab, except `incoming/` (D-021's split).
The intake publisher, the no-network host unit that already alone writes `raw/`, becomes the
only writer of the PC's corpus. It publishes take packages, which arrive through the existing
resumable `/api/v1/intake` with the D-021 ledger, class `capture`. It applies corpus requests
(decisions, trims) that the API drops into `incoming/`. And it keeps a read model in
`derived/corpus/`. The API serves that read model and the catalog snapshots, and never opens
the corpus database. Catalog edits stay the owner's `voice-lab catalog …` on the PC. No new
host unit and no new mount.

**Takes.** voice-lab-desktop's takes get their own versioned contracts: a take record, a take
package and a take's state. The Swift app's Core Audio-bound records are not reused. A take
is identified by its audio's SHA-256. It names the exact prompt and catalog version, and
carries its PortAudio continuity evidence (D-039). Takes travel one at a time, not as
sessions (D-018's corpus-first model, now on the PC).

**Clients.** Clients are offline-first and pin the catalog version they record against. This
supersedes docs/LIBRARY_VAULT.md's "serving prompts from a remote store is out of scope" and,
for HTTP clients, the recording service's relaunch pin.

**The vault.** Every new path is classified for the vault in the PR that first writes it.

## D-053 — The Mac's takes reach the PC as imported takes

Recorded 2026-10-12, for Recording v1's step 9 ([docs/design/RECORDING-V1.md](docs/design/RECORDING-V1.md)
§4 and §6): the Mac library's existing takes go to the PC first (D-051), before the Swift app
is retired.

**A separate contract.** A take of the Swift app (Cole Voice Recorder, D-015) cannot fill the
PC's take package honestly: that contract requires the PortAudio worker's evidence and backend
`portaudio` (D-039, D-052), and stays as strict as it is. A Swift take travels as its own
package, `cole-voice-lab.corpus-take-import/v1` (`corpus_take_imports`), of exactly eight
files: `manifest.json`, `qc.json`, `READY`, `original.wav`, the take's `seal.json`,
`reservation.json` and `group.json` byte for byte from the Mac's library (the seal holds its
`capture-continuity/v1` evidence), and `mac-state.json`, the Mac's decision and trim at export.
The manifest names the PC's take id (`take-<SHA-256 of original.wav>`, the scheme of the PC's
own takes, so a recording is imported once and a recording the PC already holds is a
collision), the Swift take's id, `cole-self`, the exact prompt and its digest, what the
take's group was recorded against (a recording-group/v2's `CatalogReference`, or a v1 group's
embedded suite id and digest), the PC's catalog version that counts the prompt, the seal's
request and first-sample times, the host, the Core Audio device, the client (Cole Voice
Recorder, the seal's recorder build, macOS), the audio, the four records' digests, and the
exporter's version and time.

**Validation.** `validate_take_import_package` is as strict as the other validators: the exact
file set; no link, hard link or directory; canonical JSON (the verbatim records too, as the
Mac's store requires them); every digest and byte count; each record parsed by its own strict
model; the WAV canonical and equal to the seal's digest, size and format; the seal bound to
the reservation by its digest (the Mac's own rule) and with a checked successful continuity;
the reservation's group equal to `group.json`, whose device is the seal's and is a UMIK-1 (the
capture packages' rule); the manifest's prompt, host, device, times and client equal to the
records'; the Mac's decision between the seal and the export, and its trim within the take.
`intake.validate_package` tells a capture package, a take and an imported take apart by the
manifest's `schema_version`, and so do the publisher's collision check, the take store and the
vault's raw source.

**Admission.** Not on a diagnostic root, and the manifest's catalog reference is the version
the PC's store published under that id and number (every field) and it counts the exact prompt
(`take_catalog_invalid`, `take_prompt_not_in_catalog`); a version the PC does not hold yet is
deferred, as for a take. There is no "simulated" rule: the Swift app has no simulated capture
device, and its seal's evidence attests a checked Audio Queue capture from the group's device.

**State on the PC.** An admitted package is published into `raw/<manifest_sha256>/` like any
package, and its first record carries the Mac's state instead of an undecided draft: the
decision at revision 1 (a draft stays at revision 0, whatever its Mac revision); a rejection's
reason (a removal's stays in the raw package's `mac-state.json`, since the PC's record has a
reason only for a rejection); decided when the Mac's records say (an explicit reject, remove or
restore), else when the PC accepted the take (a Save records no time on a Swift take); and the
trim at revision 1 unless it keeps the whole take. The record and the library's takes gain an
optional `origin` (`{"kind": "imported", "source_take_id": …}`), absent for a take a client
uploaded, so every earlier record keeps its bytes and the schema ids stay `v1`. Step 4's
decisions and trims then apply to an imported take like any other. The PC is canonical once it
holds a take: a change made on the Mac afterwards does not follow it (`plan` reports such
takes); it is made on the PC.

**The exporter and its approval.** `voice-lab corpus-import plan|approve|send` runs on the Mac,
against the Mac's library (`corpus_import`). `plan` reads the native takes through the take
store's read-only readers, packages every sealed take of `cole-self` whose exact prompt the
PC's version counts (its `GET /api/v1/catalog` answer, saved or fetched) under
`tmp/corpus-import/<take_id>/`, and writes the export plan: the version and every package's
take ids, manifest digest and every file's digest and size, without a time, so planning the
same takes again names the same payload. A package already there is reused. One never sent
(the home-host ledger names no upload of it) is packaged again when the PC's version or the
Mac's decision or trim has changed; one sent is never packaged again, and a later change on
the Mac is reported (`mac_state_changed_since_sent`). `plan` reports every other take with its
reason: room tones (in no cole-corpus version), prompts the version does not count, unsealed
or damaged takes, another subject's, audio another take already holds, and the legacy capture
sessions (Qt desktop, capture CLI, prepared packages) this step does not export.

`approve` is AGENTS.md's separate transfer approval, the owner's alone: it names the plan's
digest and the destination, the endpoint and node of the standing home-host approval
(D-021). A plan is approved per destination: after the home host's node changes (a
reinstalled PC), the same command approves it again for the new one, beside the first
approval. `send` uploads only an approved plan's packages, through the standing approval, the
node check and the ledger like every upload (class `capture`). The approval cannot be
bypassed and holds at each upload: `upload_session` refuses an imported take unless it is
given the plan's approval, so `voice-lab intake upload`, the desktop uploader and every other
generic path refuse one and point to `corpus-import send`; and immediately before each
upload's ledger entry it reads the plan and its approval again, for the destination of the
standing approval that very upload uses, and requires the upload's declaration (its manifest
and every file's digest and size) to be one the plan lists. `send` resumes, and a take the
ledger records as accepted is not sent again. The plan and its approvals are kept 0400 in
`provenance/host-transfers/` (`corpus-import-<plan_sha256>.plan.json`,
`.approval-<SHA-256 of endpoint and node>.json`), the records class of the phase-3 move's
approval; the packages never leave the Mac's library (Mac-only: copies of what the vault
already carries), and every directory of the export is reached through `confined_path`, never
through a link. The PC's raw packages and records keep their rules. The Mac keeps every take:
nothing is deleted.

**Amends** D-018 (its known gap) and D-039 (its evidence now travels), each with a dated note;
docs/NATIVE_RECORDING_UX.md §9 and §15 ("nothing leaves the Mac"); and RECORDING-V1.md §4's
last paragraph, which said the exporter maps Swift takes onto the PC's take contracts: it maps
them onto their own.
