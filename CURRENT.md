# Current status

## Current operator focus

On 2026-09-17, Cole settled the open questions in
[the library vault plan](.claude/plans/2026-09-17-library-vault-on-ubuntu-pc.md)
and asked for it to be built: an encrypted, content-addressed copy of the prompt
database, recordings, edits, datasets and finished fine-tune runs, held in a
container reached over the Tailnet, with the key kept on the Mac. The storage
implementation now includes: the storage and catalog ports with their
conformance suites, the local and encrypted content-addressed stores, the
manifest chain with verification and restore, the vault server and its
identity-checked client, the destination approval, the binding and the
`voice-lab vault` commands, background copying in the recording service, and
`finetune archive`/`restore-run` for finished runs. Steps 12 and 13 add the
sidebar vault line, Speak's archived-run blocker, the synthetic native `vault`
suite, and the offline-verified container/deployment templates. D-020 and the
[Mac operator runbook](docs/RUNBOOK.md#9-library-vault) finish the policy and
procedures in Step 14. This describes checkout capabilities, not an installed
app or deployed vault. The combined corpus/fine-tune/Speak/vault build and
install still needs Cole and the UMIK-1; the frozen runtime must be rebuilt with
its encryption dependencies. Step 15 remains outstanding: no real binding
exists, no vault is deployed, and no byte of Cole's library has left the Mac.
Cole chooses the training opt-in and approves each deployment action and the
first copy. See [the host runbook](deploy/vault/README.md).

On 2026-09-11, Cole requested a complete implementation plan for a faster,
Voice Memos-like native recording interface with a real-time waveform and stable
layout. [The native recording plan](.claude/plans/2026-09-11-native-recording-ux.md)
and [design/acceptance contract](docs/NATIVE_RECORDING_UX.md) specify this future
increment, including independent takes and background verification. It is planned,
not implemented; the existing desktop remains the current application.

On 2026-09-07, Cole chose to record the existing first pilot batch and return to
interface review next. [Corpus research notes](docs/CORPUS_RESEARCH.md) preserve
the findings, sources, proposed training/evaluation budgets, and prerequisites
for later bulk capture. No expanded corpus or training run is approved by that
discussion. Cole subsequently supplied interface feedback and authorized its
implementation: a two-pane library/inspector, large script, grouped transport,
safe keyboard controls, and clear state. Work is tracked in
[the desktop experience plan](.claude/plans/2026-09-07-desktop-recording-experience.md).
Cole then requested waveform trimming, seek/pause playback, direct selected-row
recording, recoverable deletion/retakes, semantic icons, and removal of the Dock
icon border. This follow-up is tracked in
[the waveform editor plan](.claude/plans/2026-09-07-waveform-recording-editor.md).
Room-tone instructions were subsequently mistaken for words to read aloud.
[The task-presentation follow-up](.claude/plans/2026-09-07-room-tone-instructions.md)
separates silent instructions from spoken scripts without changing the corpus.
On 2026-09-08, Cole reported that the live timer exceeded saved clip duration by
about two seconds. [The media-clock fix](.claude/plans/2026-09-08-recording-media-clock.md)
excludes startup and post-stop checking from the displayed audio duration.
Cole then reported intermittent chopping. Original-file analysis and an explicitly
approved local timing probe established capture discontinuities, tracked in
[the capture-continuity fix](.claude/plans/2026-09-08-capture-continuity.md).
Cole confirmed the new spoken take sounds good, then requested a more cohesive
recording/editor workflow. [The review-flow plan](.claude/plans/2026-09-08-recording-editor-review-flow.md)
and [item-by-item UX decisions](docs/RECORDING_EDITOR_UX.md) track this increment.

## Implemented increment

New desktop sessions now select a Core Audio device UID and use an isolated Python
Audio Queue capture worker with multiple input buffers, a bounded writer queue,
and per-buffer sample-timestamp continuity checks. Failures preserve private
partials and report errors, never filled-in silence or completed takes. A separate
private continuity diagnostic accompanies native takes. The historical
AVFoundation backend remains readable; further desktop recording requires a new
native session, preserving past session identities and original bytes. Saved PCM
QC additionally flags conservative repeated buffer-boundary joins and short
internal digital-zero runs. The increment passed full verification and a separately
approved, announced UMIK-1 continuity probe; Cole subsequently confirmed that the
new spoken playback sounds good. This does not certify the entire corpus.

The editor groups the prompt, takes/waveform, and fixed workflow actions into
distinct sections. Larger grips, exclusion shading, precise hover positions,
display-only amplitude fitting, centered playback/skips, and Save trim support
manual review. Save & next requires the loaded and selected current take, preventing
approval of unseen historical/current audio. Per-take cursors are remembered;
paused edits reload their persisted bounds without restarting playback. No original
audio, capture backend, training contract, or consent evidence changes.

The primary capture interface is now `voice-lab desktop`, a standalone Python
Qt window with the full 36-prompt pilot table on the left and one large-script
inspector on the right. The inspector combines 24-point reading text, delivery
instructions, grouped Record/Keep actions, QC, and dated take history. Optional
library columns preserve date/age/count sorting alongside category/search/status
filters. R toggles recording, Space toggles play/pause, and Enter keeps a reviewed
take; typing, dialogs, key repeats, and conflicting playback actions are guarded.
Text-and-color state indicators and elapsed timers show transport state.

The capture counter follows confirmed PCM frame counts from the recorder, not process
wall time. Startup explicitly says **Starting microphone — wait** until audio
arrives; Stop/End/close freeze the last confirmed duration during cleanup. Final
review uses verified WAV frames, and successful controlled capture requires its
progress evidence to agree with those frames. Missing/malformed progress is
an explicit error, not a guessed timer. The independent wall-time watchdog,
recording-request timestamps, native playback clock, and original PCM stay intact.

Room-tone tasks now use a separate **Silent room tone / Do not speak** instruction
card, requested silence duration, and **Record silence / Stop silent recording**
controls. Spoken prompts remain under **Read aloud**, with a **Delivery guidance**
footer and approximate length explicitly marked as not a limit. A tooltip and
accessibility description clarify that the guidance is not spoken script content.
Library previews, resume, selection, and Keep
follow the same task distinction. Prompt text, IDs, catalog digests, capture
behavior, and saved audio remain unchanged; the requested duration is not auto-stop.

The highlighted prompt is the recording target; row selection is disabled during
capture. Desktop batches can be recorded out of order without changing canonical
package order or omitting required room tones. Keep prepares the next unkept prompt
without automatically opening the microphone. Retakes preserve earlier
sessions. Private desktop provenance binds per-take UTC start times, device
selection, and audio hashes, and restores review after interruption. Legacy
packages show their known session date. The native recording engine, immutable
raw intake, and consent boundaries are retained.

The original PCM waveform now has draggable trim handles, precise In/Out fields,
and a sliding seek cursor. Native macOS play/pause/stop uses the real media clock.
Trimmed previews contain only selected frames; saved edits create private
checksum-bound derivatives and preserve original bytes. Raw-only training/reference
contracts are not silently changed to edited audio. Delete is recoverable exclusion
with Restore, enforced at the accepted-source eligibility boundary. Historical
take actions and exact current-take review remain distinct. Playback errors block
recording until reopen; session-start failures preserve a resumable session.

The desktop uses optional pinned Qt widgets; its local macOS application
launcher uses the checkout's existing environment. The installed Finder entry
is `/Applications/Cole Voice Lab.app`, with Cole's supplied app artwork bundled
for Finder and assigned to the native application/window. The former personal
Applications launcher is retained outside Applications as a recoverable backup.
The gray/checkerboard exterior is now transparent, with every retained artwork
pixel unchanged. The installed ICNS was updated and Finder's exported icon checked;
the operator's already-running app was not restarted. Reopen safely to load the
new interface and live Dock icon. Native semantic icons are palette-aware.
See [docs/DESKTOP.md](docs/DESKTOP.md).
Its automated capture/history and real-widget tests use synthetic audio only;
full-session GUI/headphone acceptance remains an operator step. The older browser
console remains available through the explicit `capture console` command.

Verification on 2026-09-08: `scripts/verify` passed all 1149 tests in 371.23 seconds
at 91.44% branch-aware coverage, including strict types, lint/format, package build,
schemas, and the private-asset guard. The suite includes 147 GUI, 116 capture,
61 native-capture, 78 recording-library, 40 PCM-QC, 55 native-playback, and 62 waveform
tests. Editor-review screenshot QA covered synthetic speech and room tone in
light/dark palettes at 1060×700 and 1440×980. The installed launcher resolves the
updated checkout; save/end the current session before quitting and reopening.
This UX verification opened no microphone, played no audio, and changed no private
recordings. A previously approved and announced bounded UMIK-1 native capture confirmed
continuous timestamps, identical progress/WAV frame counts, valid hash-bound
diagnostics and no repeated buffer-boundary QC finding under concurrent test
activity. The microphone stopped automatically; diagnostic audio stayed private
and outside the corpus. The original reported narration take retains its checksum
and is now flagged by the stricter QC. No operator app restart or audible playback
was performed by that probe workflow. Cole subsequently reported the new spoken
take sounds good; corpus-wide quality assessment remains separate.

Earlier, a real FFmpeg
synthetic-only native-widget check exercised two-second startup delay, slow Stop,
and exact packet-count/WAV-frame agreement; a timestamp-offset case independently
confirmed that media duration ignores capture timestamp offsets. Compact light/dark
startup, recording, stopping, and review screenshots were inspected. Startup
failure regressions also verify capture shutdown before failed acknowledgment.
Native light/dark-palette previews were checked down to 1060x700, including separate
room-tone instruction cards, appearance changes, and restored spoken scripts. Additional
real-backend native-widget acceptance exercised trim/original retention, seek,
pause, cross-batch selection, remove/restore, retakes, exact current-take review,
and out-of-order recording through required room tones to finalized READY.
Native AVAudioPlayer loaded and sought synthetic 24-bit PCM without calling play.
Those earlier synthetic acceptance checks did not open a microphone or play audio.

The pilot-to-bake-off code path is implemented and verified with synthetic
fixtures: capture, transfer planning, intake, consent, references, datasets,
candidate/source locks, isolated workers, normalization and blinded review. The
CLI persists and resumes the all-candidate audition matrix, runs one candidate at
a time, and provides digest-confirmed model-cache eviction for the constrained
mini. It does not author the human listening verdict. Real voice trials begin
only after Cole records and personally approves the pilot scope. (Promotion and
the ChloeSpeech handoff were removed on 2026-10-11, D-044; the voice release and
the speech API replace them, D-045 and D-046.)

The capture-console increment is implemented and synthetic-tested end to end.
`voice-lab capture console` wraps the resumable native-FFmpeg take engine with
an authenticated `127.0.0.1` page, in-memory 16-bit previews, per-take QC,
explicit retake forensics outside the session, final summary, and the existing
`finalize_capture_session` publication boundary. The legacy terminal-only
`capture run` remains available. No real microphone, browser, capture host, or
private asset root was used to make this implementation claim.

Integrity hardening now binds the held-out prompt suite and equal candidate
matrices on every load, launches installed workers without inherited import
paths, hash-locks every runtime artifact (including transitive distributions),
records before/after executable byte evidence, requires a completely clean
pinned Seed checkout, seals each installed runtime tree and exact Homebrew
CPython/formula/macOS closure, and rehashes the complete
result/evidence/receipt chain before resume or status reports completion.
Every new trial requires an externally retained resident-receipt digest and
copies its source-manifest/Git-integrity preimages so historical validation
remains complete after deliberate cache eviction.

## Candidate matrix

- Qwen3-TTS 1.7B Base through MLX-Audio
- Chatterbox Turbo
- Chatterbox multilingual V3 through MLX-Audio
- F5-TTS v1 Base (non-commercial pretrained-weight caveat)
- Seed-VC (separate voice-conversion track)

## Known host constraint

The Mac mini currently has limited free disk. Runtime planning must reserve
operating-system headroom and may require sequential model caching or a separate
approved runner before all weight sets can coexist.

All five candidates use the installed CPython 3.12 base. Seed-VC's dependency
graph now resolves as a complete macOS-arm64 binary lock on 3.12, but this is a
compatibility hypothesis until its pinned checkout, weights, and MPS path pass
a real cold/warm smoke. No release claim treats lock resolution as that smoke.

## Deployment gates still human/external

- Complete the native desktop physical acceptance in `docs/DESKTOP.md`: microphone
  permission, UMIK-1 capture and playback, interruption/resume, dated history,
  retained old takes after a retake, and full pilot finalization.
- Rebuild and independently authenticate the capture kit from the final clean
  commit, then bootstrap it into a fresh MacBook Air sibling root; retain the
  old root until all of its temporary sessions are synced and accepted.
- If using the older browser console, complete its seven unchecked acceptance items in runbook
  section 3, including UMIK-1 selection, Safari/Chrome preview, Ctrl-C/resume,
  full-pilot `READY`, sync planning, and idempotent mini intake.
- Verify the mini host key and Tailnet ACL out of band.
- Install and denial-test the pinned write-only `rrsync` receiver/key lane.
- Install the verified application wheel non-editably under the content-addressed
  mini release root, seal its install receipt, and only then render/load intake.
- Run candidate downloads/renders explicitly and sequentially.
- Register a speech caller (docs/SPEECH_API.md §11; no consent record is needed since
  D-050). No designation is needed (D-055): the base voice speaks until a finished run of
  Cole's is released automatically.
