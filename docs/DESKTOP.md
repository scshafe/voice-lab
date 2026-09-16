# Native recording library

Cole Voice Lab opens a standalone Python desktop window. The interface uses
native Qt widgets; Python controls buffered macOS Audio Queue capture in an isolated
local worker. It does not
use a browser, webview, or local web server.

## Open the application

On Cole's configured Mac, the installed entry is in Finder's main
**Applications** folder at `/Applications/Cole Voice Lab.app`. Double-click it,
or run:

```bash
open "/Applications/Cole Voice Lab.app"
```

The main `/Applications` folder is different from the `Applications` folder
inside a user's home directory. The earlier personal-folder launcher has been
preserved as a recoverable backup outside Applications; use the main entry.
For a permanent Dock shortcut, drag this app from Applications into the Dock.
Do not pin the running Python process: the local interpreter-based launcher
still has Python's process identity, although Qt sets its live Dock artwork.

From the development checkout, install the separately hash-pinned desktop
dependencies into the existing `.venv` once:

```bash
.venv/bin/python scripts/bootstrap-desktop
.venv/bin/python scripts/bootstrap-desktop --execute --allow-downloads
.venv/bin/voice-lab desktop --asset-root <absolute-private-assets>
```

The first command is a download-free preview. Model locks are unchanged; the
native capture worker uses the standard library and macOS frameworks. The `desktop` extra is also available when installing the application
package; `dev` includes the widgets so verification can exercise the real UI.
For an older development environment missing the declared build backend, add
`--with-build-tools` to the bootstrap command; it installs the exact setuptools
and wheel artifacts required by `scripts/verify`.

An existing capture-host install's `assets` directory can be used directly.
Opening the desktop does not start recording or approve consent. A microphone
is required only when beginning capture, so the library remains browsable when
the UMIK-1 is disconnected.

For a new asset root, initialize it explicitly first with `voice-lab assets init`.
Do not use the repository as the private root.

Create a double-clickable macOS launcher in an existing application directory:

```bash
.venv/bin/python scripts/build-desktop-app \
  --output '/absolute/application-directory/Cole Voice Lab.app' \
  --asset-root <absolute-private-assets> \
  --device UMIK-1
```

The launcher exclusively creates a new application and refuses to overwrite an
existing one. It bundles the supplied app artwork as a multi-resolution macOS
icon; the running native window also explicitly sets its application icon.
The icon's former baked-in checkerboard border is now transparent; the interior
artwork is preserved unchanged. Reopen the app to update an already-running Dock tile.
Missing or malformed artwork fails clearly before publishing a launcher or
opening the recording window. Finder and runtime icon setup follow
[Qt's macOS application-icon guidance](https://doc.qt.io/qt-6/appicon.html#setting-the-application-icon-on-macos).
It uses the current checkout's installed interpreter; it is a
local launcher, not a portable bundled Python distribution. Keep that checkout
and environment at their current paths. Double-click the resulting application
to open the desktop without a Terminal window.

## The full recording table

The initial library includes all 36 pilot prompts, about five minutes of audio,
across room tone, references, neutral speech, conversation, warm narration,
technical speech, numbers, and expressive speech. The full prompt and delivery
guidance appear when a row is selected.

The window has two panes: the library on the left and a single recording-task
inspector on the right. The default table shows status, category, and a compact
recording-task preview. Optional recording-detail columns expose latest recording date, age,
and take count; sorting remains available to find the oldest recordings.
Search and category/status filters help find missing or older material. The
inspector shows spoken scripts in 24-point type under **Read aloud**, with delivery instructions,
transport controls, and dated history for that same prompt. It uses the native
system font and palette. The right pane groups the prompt and its delivery
guidance in one card, and takes, waveform, trimming, playback, and QC in another.
A fixed footer contains session and take decisions, including the primary
**Save & next** action. Long scripts scroll within their card, keeping the editor
and footer available at laptop sizes down to 1060 by 700 logical pixels.
Dates are stored in UTC and displayed in local time. Older capture packages only
contain a session date, and the history identifies that lower precision.

“Recorded” means the take exists in a capture session. “Finalized” means the
session has its complete checksum-bound package. “Accepted raw” means the package
has also passed intake into immutable raw storage. These states do not authorize
synthesis or certify that a reference has been reviewed for promotion.

## Record and refresh material

Select a row and press **Record** to record that exact prompt when it is ready.
When reviewing existing audio, choose **Retake** first to prepare another attempt,
then press Record; preparing a retake never opens the microphone. **Record selected**
prepares a batch without starting the microphone; **Record missing** prepares the
missing material. Batches still include required room-tone recordings. Clicking
another row immediately changes the recording target while idle or reviewing;
selection is disabled during capture. Pending takes are preserved for later review.
Switching outside the current batch saves it for Resume and prepares the new prompt.
The large script, target identifier, selected row, and recording controls stay aligned.

### Silent room tone versus spoken scripts

For **Room Tone**, do not speak or read any displayed words aloud. These tasks
have a separate **Silent room tone** inspector and a muted-speaker instruction
card headed **Do not speak**. The card shows the requested duration, instructions,
and purpose: capturing background noise for quality checks. The library preview
also identifies silence rather than presenting the instructions as a transcript.

Filter the library to **Room Tone**, select `room-tone-start` or `room-tone-end`,
and press **Record silence** (or R). Stay still in your normal recording position
for about the displayed duration—15 seconds for each pilot room tone—then press
**Stop silent recording** (or R), review, and **Save & next**. The requested duration
is guidance, not an automatic stop. Save & next does not start the next recording.

Spoken tasks instead use **Read aloud** above the script. Only that script should
be spoken; the separate **Delivery guidance** line describes how to say it. The
approximate length is labeled **not a limit**: finish the full script naturally,
without rushing or stretching it to match a number. The tooltip and accessibility
description clarify that the guidance is not part of the spoken script. Selecting
another row, resuming, or advancing after Save & next updates this distinction
to match the actual recording target. The presentation does not change prompt
identities, recording bytes, or whether earlier takes need review. A previous
room-tone take containing speech should be retaken using the existing history controls.

### Transport controls

After pressing Record, wait for **Starting microphone — wait** to change to
**Recording** (or **Recording silence**) before beginning the task. Startup may
take a moment; the timer stays at zero until the recorder reports written PCM audio.
Stop remains available during microphone startup once the capture process is open.

The live counter measures confirmed PCM samples, not time since the Record click.
It updates in steps as capture progress arrives; it does not estimate progress
between updates. Pressing Stop immediately shows **Stopping — checking audio**
and freezes the last confirmed count while the recorder finishes and the WAV is checked.
Review uses the final WAV sample count, including any last buffered samples. The
header shows whole seconds; the waveform/editor exposes fractional seconds.
Microphone initialization and post-stop checks are not included in audio duration.
The Stop button stays red while capture is active, including during background
status checks. It changes to its disabled appearance during actual shutdown.
This does not pad, trim, normalize, or otherwise change captured audio. The
ten-minute abandoned-take safety limit still measures wall time independently.

### Capture continuity and older sessions

New desktop sessions use Core Audio's Audio Queue input API, selecting the UMIK-1
by its stable hardware UID without changing the Mac's default input. Multiple
native buffers feed a bounded writer queue in a separate Python process; callbacks
do not write files, update widgets, or wait for playback/QC work. The output remains
unprocessed mono 48 kHz, 24-bit PCM WAV. No silence is inserted and no missing
speech is synthesized or smoothed over.

Every nonempty input buffer must have a valid, continuous sample timestamp.
Timestamp gaps/overlaps, queue overflow, device/format changes, and writer failures
stop the take with an error. Recoverable partials remain private and are not
published as completed audio. Continuity diagnostics live separately under
`provenance/capture-continuity/<session-id>/`, outside transferable raw packages.
The frame counter measures received/written audio; it alone does not establish
that no input was lost.

Saved-audio QC also looks conservatively for repeated sharp 512-frame-boundary
joins and short internal digital-zero runs. Findings
`repeated-buffer-boundary-discontinuities` and `short-digital-silence-or-dropout`
mean the take needs review; they do not identify the exact failure stage or
recover missing duration. A low noise floor, no clipping, or a passing automated
check is not a substitute for listening.

After updating, finish/review the current take, choose **End session**, close and
reopen the installed app, and start a **new** session. Older FFmpeg/AVFoundation
sessions remain available for playback and history, but the default desktop
resolver refuses further capture in them. This prevents changing the recorder
identity on a session that already contains older takes. No saved recording or
historical manifest is rewritten. Use a new session to retake an affected prompt.
The explicitly invoked legacy console/terminal capture path still uses FFmpeg;
do not use that path for new training takes while its input-loss issue remains.

After a continuity error, end the failed session and start a new one; do not try
to keep its partial. A short new speech take and headphone review are the final
operator acceptance before resuming the corpus.

The editor changes emphasis with the task: a large **Record** button when ready,
**Stop recording** during capture, then the waveform and centered playback controls
during review. Retake becomes a secondary footer action; it prepares a new attempt
without starting capture. A text-and-color state indicator and elapsed counter
distinguish recording, playback, and review. QC belongs to the current reviewed
take and is not presented as the score for a different historical selection;
it does not replace headphone review.

Native macOS playback supplies the real media clock. The centered controls provide
return-to-trim-start, skip backward one second, a larger play/pause button, skip
forward one second, and Stop. Skips stay within the retained trim. The playback
slider and waveform can also seek. Recording and the takes list remain locked
while playback is active or paused; press Stop before changing takes or recording.
Changing the library prompt while paused stops preview and remembers its cursor.
An audio control failure blocks recording
until the app is reopened. The capture timer still limits an abandoned take.

### Waveform, trimming, and history

After recording, the editor displays the **full original waveform**. The compact
**Takes** list is open by default immediately above it: entries identify a numbered
take, its local recording date, status, and, when applicable, **Current**. Select an
entry to load that audio; selecting a take never plays it automatically. Each take's
playhead position is remembered while moving between takes in the open window.
The Takes disclosure can hide the list when more editing space is useful.

Drag the large blue In/Out grips to exclude leading or trailing audio. Darker
shading identifies the excluded regions. The numeric **In** and **Out** fields
below the waveform stay at the left and right edges of the editing row and track
the handles' values; they do not move around with the handles. They accept times
to three decimal places. Hover over the waveform for a millisecond timestamp and
a cursor indicating an available drag action. Trimming is manual: the editor
does not guess speech boundaries or snap automatically to presumed silence.

Click or drag within the waveform, or move the slider, to position the playhead.
Double-click within retained audio to seek there and start or resume playback.
If playback is already running, double-click seeks without pausing. Double-clicking
a trim grip or an excluded region does not start playback.

**Fit height** is enabled by default so quiet recordings are easier to inspect.
The waveform labels this as display-only scaling. Turning it off restores the
original amplitude scale. Neither setting changes PCM, playback volume, trim
times, or files; visually enlarging a waveform is not an audio-quality fix.

Changing trim bounds immediately changes the selection that Play will preview,
but does not save an edit. Choose the blue **Save trim** button to persist the
selected take's trim without advancing. For the current pending take, **Save & next**
saves any pending trim, keeps the take, and prepares the next unrecorded prompt in one action.
It is available only when that current take is both selected in Takes and loaded
in the editor. If an earlier take is selected, return to the entry marked Current
before advancing; its separate Save trim action does not approve an unseen take.
Neither save action starts the microphone.

**Reset** clears the selected take's saved trim and restores the complete original
selection; it does not need a second Save trim click. Unsaved bounds require
confirmation before switching takes or closing. Saving or resetting while paused
ends that preview and reloads the saved bounds; playback never resumes automatically.

Trims create frame-exact PCM copies with checksum-bound recipes under private
`derived/recording-edits/` and `provenance/recording-edits/`. Original recordings
and finalized/raw packages remain unchanged. Existing reference/dataset contracts
remain raw-bound: a trim is not silently substituted into a training dataset.
Future derivative export must explicitly carry its edited-audio identity.

Use **Takes** to select an older dated recording. **Retake** prepares a fresh
recording for its exact prompt while keeping the previous audio. The trash icon
removes a wrong take from usable coverage, with confirmation; **Restore** undoes
this removal. This is recoverable exclusion, not physical deletion. Removed takes
cannot be played as eligible takes or selected through the shared accepted-raw
reference/dataset boundary. No original audio is purged.

Native transport, refresh, retake, and trash icons have tooltips and accessible
names. Green checks identify finalized/accepted recordings; readable status text
remains alongside the colored symbols.

### Keyboard transport

| Key | Action |
| --- | --- |
| R | Start a ready take, or stop the current recording |
| Space | Play or pause the selected trim |
| Return / Enter | Save the selected, loaded current take and trim; prepare the next unrecorded prompt |
| Left / Right in waveform | Seek by 0.1 seconds |
| Home / End in waveform | Seek to trim start / end |

These shortcuts apply only inside the active recording window. They do not
intercept typing in editable controls or interact with open dialogs/popups;
holding a key does not repeatedly trigger transport. Shortcuts remain usable
while the read-only script or silent-room-tone card has focus. Space no longer
starts the microphone.

Save & next advances through the catalog to the next prompt without a usable
recording, wrapping to earlier gaps when needed. It skips existing recordings
across sessions, including room tone; rejected or removed takes alone do not
count as recorded. Table sorting and filters do not change this order. Moving
outside the current batch saves it for Resume and prepares the remaining missing
prompts. When every prompt has a recording, navigation stops. A complete accepted
batch can be finalized; unfinished batches remain available for review and
finalization through Resume, including any room tones required for that batch.
Capturing out of order does not change the canonical package order.
While Save & next checks the library and prepares the next target, the saved
prompt and waveform remain visible with recording controls disabled. The display
switches directly to the resolved target when it is ready.
Save & next and Retake never start recording automatically. The next take waits
for Record.

The normal review loop is: select the prompt, Record, Stop, listen and adjust
In/Out if needed, then Save & next. Comparing older takes is optional and does not
change which pending take must be reviewed before advancing. The complete feedback
decisions and intentional adaptations are in
[Recording editor UX decisions](RECORDING_EDITOR_UX.md).

When every take in the batch has been kept, finalize the session. A partial batch
never publishes `READY`. Finish a headphone review before any transfer.

To refresh an older recording, select its row and record another batch. The older
recording remains available in history with its original date and bytes. Retrying
the current take preserves its rejected audio outside the transferable session.
Closing during recording retains the interrupted partial for the established
resume workflow. Resume revalidates existing takes and restores their original
timestamps and pending review status.

## Larger catalogs and evidence

`--suite /absolute/prompts.json` displays an explicit strict `PromptSuite`.
Prompt content and the catalog digest are part of recording identity: editing a
transcript never makes an older recording appear to satisfy that changed prompt.
The full training corpus is still conditional on the existing pilot review.
For a `capture-corpus` suite, pass the approved private `--pilot-gate-path` and
`--pilot-gate-sha256` before capture; the gate and its accepted sources are checked.
Held-out evaluation prompts remain separate from training capture.

Per-take history is canonical private evidence under
`provenance/capture-desktop/`, outside each transferable capture package.
The desktop also discovers validated finalized packages in `tmp` and `raw`.
Copies of the same finalized package are counted once. History errors are shown
explicitly; missing or changed evidence is never silently presented as “not yet
recorded.” All existing raw, reference, consent, and transfer boundaries apply.

## Physical acceptance

Automated verification uses synthetic audio and an offscreen native window.
Before relying on the first real session, check microphone permission for the
launched application, UMIK-1 capture, headphone playback, stop/keep/retry, closing
and resuming an interrupted take, and a full pilot finalization. Inspect the
recording dates after reopening, and confirm that a later retake leaves the
earlier take playable. Check R/Space/Enter from the script and library, and
confirm that typing in search does not operate transport. Check waveform seeking,
double-click start/resume, bounded one-second skips, pause/resume, trim boundaries,
Save trim, Reset, and recoverable removal on a disposable take. Compare two Takes
entries, verify the Current marker and remembered cursor, and confirm Save & next
cannot approve the current take while a historical take is selected. Toggle Fit
height to verify only the display changes. The automated gate never opens a real
microphone. Any hardware diagnostic
is separately approved, announced, automatically bounded, and kept outside the corpus.
Also check the startup wait cue, Stop during startup, and the counter against
the saved waveform duration. Begin reading only after Recording appears; the
startup cue does not itself suppress microphone-settling noise in the raw audio.

After a checkout update, an already-open app retains the interface it loaded.
Stop and review an active take first; finalize a complete batch or use End
session to preserve an unfinished batch, then close and reopen the local app
when convenient. Development verification does not restart an operator's app.
