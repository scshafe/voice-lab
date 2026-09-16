# Decision log

## D-001 — Sibling lab, not ChloeSpeech core

Capture, biometric storage, dataset construction, training, and comparative
evaluation remain in `cole-voice-lab`. ChloeSpeech receives only a validated,
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

## D-008 — Sequential candidate residency

The 16 GiB mini runs one candidate in one fresh subprocess at a time. Model
process exit is the Metal-memory release boundary. Weight caches may also need
sequential explicit lifecycle management because the 10 GiB free-space floor
prevents all quality profiles from coexisting on the current disk.

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
