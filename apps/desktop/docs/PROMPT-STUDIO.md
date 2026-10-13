# The prompt studio

**Record → Prompts** is the prompt studio: the Swift app's recording workspace, rebuilt in Qt
over the corpus client (D-051, D-052; [RECORDING-V1.md](../../../docs/design/RECORDING-V1.md)
§5 and step 6). It is corpus-first (D-018): there are no sessions, groups or packages. A take
is recorded against one prompt of the catalog version the app pinned for this run. Save makes
it durable on this machine at once, and the corpus client sends it to the PC later
([CORPUS-CLIENT.md](CORPUS-CLIENT.md)).

Step 6a built the fixed workspace and the record, stop and save loop. Step 6b filled in
the take picker, the waveform, trims, playback, take history and the decisions beyond Save.
Step 6c adds the keys.

## The workspace

The layout follows docs/NATIVE_RECORDING_UX.md §4. Every region keeps its place and size
whatever the take, the transport or the sync is doing. Only the window's size changes them:

- a taller window gives 70% of the extra height to the script and 30% to the waveform;
- a window shorter than the workspace gives the prompt the room first: the waveform shrinks
  down to 64 points, then the script card down to 160 points; only a window shorter still
  scrolls.

The prompt is always shown whole. It is set at the reading size, 24 points, when it fits the
card, and otherwise at the largest smaller size that does, down to 16 points. The longest
catalog prompt fits whole at every window size the app allows: at 17 points on the smallest
(960×600), at 24 points at 1440×900. The size depends only on the card and the prompt, never
on the transport.

| Region | What |
| --- | --- |
| **Library** (left, 280 points; 240 below a 1,000-point screen) | `Library` with the covered count (`n / 1,886`), and where it comes from. Then **Continue Missing**, the search, **Show** (All prompts, Missing, Needs attention) and **Labels**. Then the catalog version this run records against, and the switch the next session makes. Then every prompt, with its status. |
| **Header** | The prompt's title and its label chips. Chips that do not fit collapse into `+N`, but the category always shows. The microphone is on the right. |
| **Script card** | For a spoken prompt, `PROMPT · READ ALOUD` and the text, whole (see above). For room tone or a test step, a banner and "No spoken prompt" instead. The `INSTRUCTIONS · NOT READ ALOUD` section is always there, with the guidance and the duration. |
| **Take picker** | The prompt's takes, newest first, from this machine and the PC: when, how long, status, where. Then **Remove**, **Reject…**, **Restore** and **History** (every take, with changed elsewhere marked). |
| **Waveform** | The selected take on the dB display scale, its kept frames between the trim handles, the playhead. While recording: the live waveform, in red. When a take fails or does not start, or there is no microphone, the whole message shows here instead. |
| **Trim row** | **In** and **Out**, set from the playhead (**Set In**, **Set Out**) or by dragging the handles; **Save trim**; **Reset**. |
| **Timer** | While recording, the take's length from the frames the worker confirmed (D-014), never a clock, and the peak level. Otherwise the playhead within the kept frames, and their length. |
| **Playback row** | **−** and **+** (Zoom) and **Fit**; **−0.1 s**, **Play**/**Pause**, **+0.1 s**. |
| **Footer** | The 56-point Record/Stop control, then **Retake**, **Discard**, **Save** and **Save & Next**. Actions that do not apply stay in place, greyed out. |
| **Status line** | Two lines at most on each side, never cut short. On the left, what the take is doing. On the right, the sync, or a notice. |

## Recording

1. Select a prompt in the library, or press **Continue Missing**. Selecting is locked while
   a take is recorded or saved.
2. **Record** starts the capture worker (D-039) into a new take directory bound to that
   prompt (`CorpusClient.begin_take`). **Stop** ends it. The worker's take is then checked
   (its continuity evidence, its WAV) and waits for review.
3. **Save** keeps it: the take is saved into the spool, decided `kept`, and queued for the
   PC. This runs off the GUI thread, and the prompt stays selected. **Save & Next** does the
   same, then selects the next missing prompt.
4. **Retake** records the prompt again. The take it replaces is saved as an undecided draft,
   so no recording is lost. A take you leave by selecting another prompt is saved the same
   way. A draft does not count as coverage: its prompt shows *Draft* under Needs attention.
5. A take that fails (a continuity break, a worker that stopped) says why in the status line.
   **Discard** removes it (and can drop an unsaved take). Retake, or selecting another
   prompt, discards a failed take too.

Without a microphone (no UMIK-1), the header says *No microphone* and Record stays off.
Click the chip to look again after plugging the UMIK-1 in.

## Takes, review and trims

- **The picker** lists every take of the prompt, merged by take id: this machine's (the
  spool, with the decisions made here, ahead of the PC while they wait to be sent) and the
  PC's (`GET /api/v1/corpus/prompts/{id}/takes`, read again whenever the PC's library
  changes). Selecting a prompt in the library selects its latest kept take; Continue
  Missing and Save & Next select none. After Stop, the take under review comes first.
- **Decisions** go through the corpus client: **Remove**, **Reject…** (a dialog: the reason
  is required, and the button stays off without one) and **Restore**. The library's status
  and the coverage follow at once. **Save** on a selected draft of this machine keeps it. A
  take recorded on another machine is shown and played, but decided where it lives: the
  client holds this machine's takes only. A take whose state the client adopted from the PC
  (another client changed it) reads *changed elsewhere* in the picker and History.
- **The waveform** draws the selected take on the fixed dB display scale (0 dBFS at the full
  half-height, −30 dBFS at half of it, −60 dBFS and below as a hairline). The PC's takes are
  played from memory, never written here (D-038), and only under the PC's playback approval.
  **+** halves the view around the playhead, down to a twentieth of a second; **−** doubles it;
  **Fit** shows the whole take. A click moves the playhead; a double-click plays.
- **While recording** the waveform follows the worker's own partial WAV as it grows (the
  worker writes the checked PCM there block by block and exposes no other level stream), in
  a rolling five-second view, with the peak level beside the timer. If the partial cannot be
  read, the waveform says so.
- **Trims** are recipes, never edits of the original. **Set In** and **Set Out** take the
  playhead. On a saved take of this machine, **Save trim** keeps it (`set_trim`), and **Reset**
  goes back to the whole take. On the take under review, Save keeps the take with its trim,
  as does Retake (which saves the replaced take as a draft). An edit not saved is dropped when
  another take is selected, and a toast says so.
- **Playback** plays exactly the kept frames (D-012) through the app's one player, from the
  playhead. **−0.1 s** and **+0.1 s** move the playhead within the kept frames, also while
  playing. Nothing plays while a take is recorded.

## Coverage and status

A prompt is **Recorded** when it has a kept take, **Needs review** when its kept takes all
failed the PC's QC, **Draft** when it has only undecided takes, and **Missing** otherwise.
Rejected and removed takes count for nothing. Two sources count:

- the PC's library, as last read (each sync pass reads it; it is cached on disk, so offline
  you still see what the PC had);
- this machine's takes, with the decisions made here.

The better of the two stands. *Recorded · this machine* means the PC does not show the
prompt as recorded yet. When the PC has moved to another catalog version, its library still
counts the prompts the two versions share. The coverage count is the Recorded and Needs
review prompts over the pinned version's prompts.

**Continue Missing** and **Save & Next** go to the next prompt without a kept take after the
selected one, in catalog order, wrapping once. Search, Show and Labels never change where
they go. Search is case-insensitive and matches the title, id, prompt, instructions, guidance
and labels. Labels combine with OR, and the three filters with AND. When the selected prompt
is filtered out, it stays reachable at the top of the list.

## The catalog version

The studio records against the version pinned when the app started. When the PC activates
another version, the library says *Version N is active on the PC: it switches at the next
session*. The switch
happens at the next start, or at **Switch now** (only between takes, never with one under
review). With no catalog cached and no PC, nothing can be recorded. The studio says *No
catalog yet* until the PC has answered once.

## The status line

The right half reads the corpus client's `SyncStatus`:

- `Uploads off until S-1 passes (corpus_sync) · N takes here` while sending is held
  (`corpus_sync = false`, the default). This is a statement, not an error.
- `Offline · N queued · M sent · last synced HH:MM`.
- `N takes waiting for the PC's state` after the PC refused a change and its state is read
  again.
- Damaged takes and recordings an earlier run could not save come first.
- Otherwise `Waiting to sync`, `Syncing` or `Up to date`, with the queued and sent counts.

A notice from the client takes the right half, in a few words that name its cause: *changed
elsewhere*, *a change waits: the PC cannot read it*, *the PC refused a change
(trim_out_of_bounds)*, *the PC refused this take*. **Details** opens the client's whole
message, with **Dismiss**. A line that leaves something out (for example why uploads cannot
start) has the same **Details**.

## Switching back to the session flow

Phase 4c's session flow (D-042) stays in the app until the studio reaches parity. To use it
on a machine, set this in `config.toml` and restart the app:

```toml
prompts_mode = "sessions"   # the default is "studio"
```

Record → Prompts then shows the session screen (`PromptsScreen.qml` over
`screens/prompts.py`), and the corpus client does not run. Remove the line to go back. The
two keep their data apart: the studio in `<data dir>/corpus/`, the sessions in
`<data dir>/prompted/`.

## Code

| Where | What |
| --- | --- |
| `screens/studio.py` | `StudioModel`, the screen's model over a `CorpusClient`. Every connection goes to a declared `@Slot` (#41). |
| `screens/studio_takes.py` | `TakeReviewModel` (`app.studioModel.takes`): the picker, the decisions, the waveform, trims and playback. |
| `studio/takes.py` | A prompt's takes from the spool and the PC, merged, with their status and where they live. |
| `studio/audio.py` | Samples in memory, the dB display bins, exact clips, the live waveform. |
| `studio/presentation.py` | How a prompt is shown: kind, banner, sections, labels, search. |
| `studio/library.py` | Statuses from the PC's library and the spool, coverage, filters, the next missing prompt. |
| `studio/status.py` | The status line's words. |
| `corpus/library.py` | The PC's library, cached on disk and read by each sync pass. |
| `qml/screens/StudioScreen.qml`, `qml/screens/studio/` | The workspace and its regions. |

What 6c attaches to (the screen installs no shortcuts yet):

- R: `studio.toggleRecord`; Return: `studio.saveNext`; Ctrl+S or ⌘S: `studio.save`;
  Space: `takes.togglePlay` (the Swift app's play/pause);
- ← and → in the waveform: `takes.seekBy(∓4800)` (or `stepBack`, `stepForward`); Home and
  End: `takes.seekToStart`, `takes.seekToEnd`;
- `menuOpen` on the screen says when a menu, the Details popup, the take picker's list,
  History or the Reject dialog is open, so keys can step aside.

Tests: `tests/studio/` drives the screen offscreen over the PC rig
(`tests/corpus_client/corpus_rig.py`), with a stand-in recorder that leaves the worker's files
(`studio_fakes.py`; its partial WAV grows as frames are confirmed).
`test_studio_worker.py` runs the real worker on its simulated UMIK-1. `test_studio_takes.py`
covers 6b: the picker over local and PC takes, each decision through the client, Reject's
reason, trims with the preview's exact frames, Zoom and Fit, the steps, the live waveform,
the geometry across states, and changed elsewhere.
