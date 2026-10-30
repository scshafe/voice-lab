# The prompt studio

**Record → Prompts** is the prompt studio: the Swift app's recording workspace, rebuilt in Qt
over the corpus client (D-051, D-052; [RECORDING-V1.md](../../../docs/design/RECORDING-V1.md)
§5 and step 6). It is corpus-first (D-018): there are no sessions, groups or packages. A take
is recorded against one prompt of the catalog version the app pinned for this run. Save makes
it durable on this machine at once, and the corpus client sends it to the PC later
([CORPUS-CLIENT.md](CORPUS-CLIENT.md)).

Step 6a built the fixed workspace and the record, stop and save loop. Step 6b filled in
the take picker, the waveform, trims, playback, take history and the decisions beyond Save.
Step 6c added the keys, decisions on takes recorded elsewhere, the question before an unsaved
trim edit is left, and the text-size choice. The parity checklist is at the end.

## The workspace

The layout follows docs/NATIVE_RECORDING_UX.md §4. Every region keeps its place and size
whatever the take, the transport or the sync is doing. Only the window's size changes them:

- a taller window gives 70% of the extra height to the script and 30% to the waveform;
- a window shorter than the workspace gives the prompt the room first: the waveform shrinks
  down to 64 points, then the script card down to 160 points; only a window shorter still
  scrolls.

The prompt is always shown whole. It is set at the chosen reading size (24, 28 or 32
points) when it fits the card, and otherwise at the largest smaller size that does, down to
16 points. The size depends only on the card and the prompt, never on the transport or the
sync line. What the layout tests assert (`test_the_longest_prompt_is_shown_whole`, for each
reading size at 960×600 and at 1440×900): the rig catalog's longest prompt is whole at the
chosen size or below it, never below 16 points, inside the card and without scrolling; the
size and every region stay the same while recording, in review and with the sync line's
**Details** shown; at 1440×900 the default 24 is untouched. Each geometry is read only after
the window has drawn a frame, twice, the two the same. Exact sizes below the chosen one
depend on the fonts installed, so no test pins them.

| Region | What |
| --- | --- |
| **Library** (left, 280 points; 240 below a 1,000-point screen) | `Library` with the covered count (`n / 1,886`), and where it comes from. Then **Continue Missing**, the search, **Show** (All prompts, Missing, Needs attention) and **Labels**. Then the catalog version this run records against, and the switch the next session makes. Then every prompt, with its status. |
| **Header** | The prompt's title and its label chips. Chips that do not fit collapse into `+N`, but the category always shows. On the right: the reading size (**Aa 24**, 28 or 32, kept for you in `<data dir>/studio/view.json`) and the microphone. |
| **Script card** | For a spoken prompt, `PROMPT · READ ALOUD` and the text, whole (see above). For room tone or a test step, a banner and "No spoken prompt" instead. The `INSTRUCTIONS · NOT READ ALOUD` section is always there, with the guidance and the duration. |
| **Take picker** | The selected take in a short line (`12 Oct 07:01 · 2.0 s · Kept · here`), whole on the smallest window. Open, it is the history: every take of the prompt, newest first, from this machine and the PC, with date and time, length, status (trimmed, changed elsewhere, imported from the Mac) and where it lives. Then **Remove**, **Reject…** and **Restore**. |
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
   same, then selects the next missing prompt. Return or ⌘S pressed while the take is still
   checked (*Stopping*) is not lost: the status line says *Stopping · will save when ready*,
   and the save is done the moment the take is ready. A take that fails drops it.
4. **Retake** records the prompt again. The take it replaces is saved as an undecided draft,
   so no recording is lost. A take you leave by selecting another prompt is saved the same
   way, and one left by closing the app stays on disk and is saved at the next start. A
   draft does not count as coverage: its prompt shows *Draft* under Needs attention.
5. A take that fails (a continuity break, a worker that stopped) says why in the status line.
   **Discard** removes it. Retake, or selecting another prompt, discards a failed take too.
   Discard is for a failed take only: a good take is never discarded. To set one aside,
   save it (or let Retake keep it as a draft), then **Remove** it.

At start, the corpus client lists this machine's takes. That finishes what an earlier run
left (it may copy and hash a WAV) and runs on a worker, as does the first read of the PC's
library cache. Meanwhile the status line says *Checking this machine's takes* and Record
stays off; the window answers throughout.

Without a microphone (no UMIK-1), the header says *No microphone* and Record stays off.
Click the chip to look again after plugging the UMIK-1 in.

## Takes, review and trims

- **The picker** lists every take of the prompt, merged by take id: this machine's (the
  spool, with the decisions made here, ahead of the PC while they wait to be sent) and the
  PC's (`GET /api/v1/corpus/prompts/{id}/takes`, read again whenever the PC's library
  changes). Selecting a prompt in the library selects its latest kept take; Continue
  Missing and Save & Next select none. After Stop, the take under review comes first.
- **Decisions** go through the corpus client for every saved take: **Remove**, **Reject…**
  (a dialog: the reason is required, and the button stays off without one) and **Restore**.
  Restore goes back to the decision before the rejection or removal. When this machine does
  not know it (a take recorded elsewhere, first seen here already rejected or removed, or a
  state adopted from the PC), it asks **Restore as kept**, **Restore as draft** or
  **Cancel**; it never guesses. This includes a Swift take the Mac rejected or removed
  before it was imported: the PC's records do not carry what it was before (the Mac's
  answer stays in the raw package's `mac-state.json`). The library's status and the
  coverage follow at once. **Save** on a selected draft keeps it. A take recorded elsewhere (another client's, or a Swift take imported from the Mac,
  step 9, D-053) is noted in the client's spool when it is first changed
  ([CORPUS-CLIENT.md](CORPUS-CLIENT.md), "Takes recorded elsewhere"); its changes chain on the
  PC's revisions and wait offline like any. Its audio stays on the PC. A take whose state the
  client adopted from the PC (another client changed it) reads *changed elsewhere*; one the
  PC says was imported reads *imported from the Mac* (*Mac import* in the short line).
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
  as does Retake (which saves the replaced take as a draft); looking at another take keeps
  the edit for it. Leaving a saved take with an edit not saved (another take or prompt,
  Continue Missing, Record, another screen, closing the window, Quit) asks **Save the
  trim?**: **Save trim**, **Discard edit** or **Cancel**. Cancel stays, and keeps the app
  open. An edit shows its In and Out in amber, with *edited* beside them where the row has
  room. On the smallest window the row leaves out the *In* and *Out* captions: **Set In**
  and **Set Out** name the times beside them.
- **Playback** plays exactly the kept frames (D-012) through the app's one player, from the
  playhead. **−0.1 s** and **+0.1 s** move the playhead within the kept frames, also while
  playing. Nothing plays while a take is recorded.

## Keys

| Key | What |
| --- | --- |
| R | Record; Stop while recording |
| Space | Stop while recording; otherwise play or pause the selected take. It never starts a recording (the Swift app's rule: Space never opens the microphone) |
| Return, Enter | Save & Next; typed ahead while the take is checked, done when it is ready |
| Ctrl+S (⌘S on macOS) | Save; typed ahead the same way |
| ← / → (waveform focused) | The playhead a tenth of a second back or on, within the kept frames |
| Home / End (waveform focused) | The playhead to the kept frames' start or end |

A click in the waveform gives it the keyboard focus. The keys belong to this screen only.
They do nothing while you type in a field (the search, the reason), while a menu, a list or
a dialog is open (`menuOpen`), or on a held key's repeats.

## The core loop

Open the prompts, **R** to record, **R** or **Space** to stop, **Return** to save and go to
the next prompt, and again, rapid fire. The owner's rule (D-058) is that this loop gets
faster, never slower. `test_studio_core_loop.py` holds it, over six prompts in a row: no
dialog, menu or popup opens, the keyboard focus never moves, the keys work the moment the
next prompt shows (R would record in the very change that shows it), and a Return pressed
before the take seals is done when it seals. Every change to the studio keeps it passing.

## Coverage and status

A prompt is **Recorded** when it has a kept take, **Needs review** when its kept takes all
failed the PC's QC, **Draft** when it has only undecided takes, and **Missing** otherwise.
Rejected and removed takes count for nothing, and so does a take the PC no longer has. The
status comes from the best-known state of **each take**:

- A take this machine knows (recorded here, or recorded elsewhere and changed here) stands
  with its state here. That state is ahead of the PC's while a change waits to be sent. When
  the PC's history shows the take at a newer revision (another client changed it), the PC's
  state stands instead.
- A take only the PC knows stands with the PC's state.

The PC's library (each sync pass reads it; it is cached on disk, so offline you still see
what the PC had) gives only a status and a count per prompt. It is used alone only for takes
this machine does not know. When it cannot be split (the PC's newest kept take is one known
here, and the PC counts takes this machine does not know), the studio reads that prompt's
history from the PC, cached until the library changes. Until then, only the takes here
count. A status is never inferred from a count: a kept take removed here makes its prompt
*Missing* at once, whatever the PC's row still says. Each take is counted once, here or on
the PC. *Recorded · this machine* means the PC does not show the prompt as recorded yet. When the PC has moved to another catalog version, its library still
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
- `Uploads need your OK · N queued` when a pass (with `corpus_sync` on) found no home-host
  approval for the server in this machine's library (none yet, expired, another PC, or
  without `capture`), with **Approve** beside it: the app's approval dialog (FIRST-RUN.md
  §4). Approving syncs at once.
- `N takes waiting for the PC's state` after the PC refused a change and its state is read
  again.
- Damaged takes and recordings an earlier run could not save come first.
- Otherwise `Waiting to sync`, `Syncing` or `Up to date`, with the queued and sent counts.

A notice from the client takes the right half, in a few words that name its cause: *changed
elsewhere*, *a change waits: the PC cannot read it*, *the PC refused a change
(trim_out_of_bounds)*, *the PC refused this take*. **Details** opens the client's whole
message, with **Dismiss**. A line that leaves something out (for example why uploads cannot
start) has the same **Details**. **Sync now** runs a pass now, operations waiting out a
backoff included; it is beside the line while operations wait, the PC did not answer, or
takes wait for its state. The line has one button at most (**Approve**, **Sync now** or
**Details**), so every line stays whole, in two lines at most, on the smallest window
(`test_every_sync_line_is_whole_on_the_smallest_window`). **Approve** and **Sync now** wait
while a take is being recorded or saved.

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

The keys are the screen's `Shortcut`s (`studioKey…`), enabled by `keysEnabled`; they call
`studio.toggleRecord`, `studio.spaceKey`, `studio.saveNext` and `studio.save`; the waveform's
own keys call `takes.seekBy`, `seekToStart` and `seekToEnd`. `takes.guard(action)` asks
before an unsaved trim edit is left; `AppModel.navigate` goes through it too, and so does
closing: Main.qml's `onClosing` asks `app.mayClose()`, and `QuitGuard`, an event filter on
the application (`cli.run`), holds Quit, which QApplication does not route through the
window's close. `closeApproved` closes the window once the answer allows it.

Tests: `tests/studio/` drives the screen offscreen over the PC rig
(`tests/corpus_client/corpus_rig.py`), with a stand-in recorder that leaves the worker's files
(`studio_fakes.py`; its partial WAV grows as frames are confirmed).
`test_studio_worker.py` runs the real worker on its simulated UMIK-1. `test_studio_takes.py`
covers 6b: the picker over local and PC takes, each decision through the client, Reject's
reason, trims with the preview's exact frames, Zoom and Fit, the steps, the live waveform,
the geometry across states, and changed elsewhere. 6c: `test_studio_remote.py`,
`test_studio_keys.py`, `test_studio_confirm.py`, `test_studio_polish.py`. The layout tests
(`geometry`) read every region only after the window has drawn a frame, then again after the
next, and fail unless the two agree.

## Parity checklist (RECORDING-V1.md §5)

The Swift app's interface, item by item, with the test that covers each (in
`apps/desktop/tests/studio/`). Every test runs offscreen on synthetic audio. Cole signs
parity off in step 10, on the real Mac, after S-1 (step 8).

| §5 item | Done | Covered by |
| --- | --- | --- |
| Library sidebar with coverage (`n / 1,886`) | yes | `test_the_workspace_loads_the_pinned_catalog_and_its_coverage` |
| A prominent Continue Missing | yes | `test_continue_missing_walks_the_catalog_in_order` |
| Search; Show (All / Missing / Needs attention); Labels | yes | `test_the_filters_and_the_search`, `test_show_labels_and_search_combine_with_and` |
| Per-prompt status | yes | `test_the_workspace_loads…`, `test_record_stop_save_keeps_a_local_take`, `test_a_prompt_stands_on_the_best_known_state_of_each_take`, `test_the_pcs_count_never_makes_a_take_removed_here_kept`, `test_a_removal_here_is_never_hidden_by_the_pcs_kept` |
| Header with label chips | yes | `test_the_workspace_loads…`, `test_labels_are_in_effective_order_and_titled_verbatim` |
| A text-size choice | yes | `test_the_text_size_is_chosen_and_kept` |
| Script card: PROMPT · READ ALOUD, or INSTRUCTIONS for silent prompts | yes | `test_the_category_alone_decides_what_is_read_aloud` (silent kinds as a unit test: the rig's catalog has no room tone), `test_the_longest_prompt_is_shown_whole` (each reading size, 960×600 and 1440×900) |
| Take picker, waveform, trim row, timer, playback row | yes | `test_the_picker_lists_this_machines_and_the_pcs_takes`, `test_zoom_and_fit`, `test_trims_save_and_reset…`, `test_steps_of_a_tenth_of_a_second` |
| 56 pt Record/Stop footer and a status line | yes | `test_transport_never_moves_the_layout`, `test_the_sync_line_for_each_state`, `test_nothing_is_cut_off_on_the_smallest_window`, `test_every_sync_line_is_whole_on_the_smallest_window` |
| Transport state never moves the layout | yes | `test_transport_never_moves_the_layout` (with and without the sync line's Details), `test_transport_and_review_never_move_the_layout` (two window sizes, every region inside the content area), `test_the_longest_prompt_is_shown_whole` |
| The live waveform while recording | yes | `test_the_live_waveform_while_recording` (from the worker's partial WAV) |
| Take actions: Retake, Remove, Reject (with a reason), Restore | yes | `test_retake_keeps_the_earlier_take_as_a_draft…`, `test_a_good_take_is_never_discarded`, `test_each_decision_goes_through_the_client`, `test_reject_without_a_reason_is_impossible`, `test_takes_recorded_elsewhere_are_decided_and_trimmed_here`, `test_restore_asks_when_what_came_before_is_not_known` |
| Save, or Save & Next in context; Return = Save & Next, ⌘S = Save | yes | `test_record_stop_save…`, `test_save_and_next_goes_to_the_next_missing_prompt`, `test_r_space_return_and_save` |
| Trims with In/Out, Save trim and Reset | yes | `test_trims_save_and_reset_with_a_preview_of_exactly_the_kept_frames`, `test_another_take_save_discard_or_cancel`, `test_closing_the_window_or_quit_asks_first` |
| Zoom and Fit | yes | `test_zoom_and_fit` |
| Playback with −0.1 s and +0.1 s steps | yes | `test_steps_of_a_tenth_of_a_second` |
| Take history across every session, with dates and status | yes | `test_the_picker_lists…`, `test_a_take_changed_elsewhere_shows_so`, `test_a_take_imported_from_the_mac_says_so`, `test_a_late_history_never_moves_the_selection` |
| Keys R, Space, Return, ⌘S; ←/→/Home/End in the waveform | yes | `test_r_space_return_and_save`, `test_the_waveform_keys` |
| The core loop, rapid fire; a Save typed ahead before the seal (D-058) | yes | `test_the_core_loop_rapid_fire`, `test_a_save_typed_ahead_stays_and_a_failed_take_drops_it` |
| Keys ignored while typing, in dialogs, on auto-repeat | yes | `test_keys_are_ignored_while_typing_in_menus_dialogs_and_elsewhere`, `test_the_waveform_keys`. The page's shortcuts are checked to refuse repeats (`autoRepeat: false`): QtTest cannot send a repeat through the window's shortcut map |
| Corpus-first: no sessions, groups or packages; Continue Missing walks the whole catalog in order | yes | `test_the_workspace_loads…`, `test_continue_missing_walks_the_catalog_in_order` |

Two differences from the Swift app remain, both deliberate:

- **Space stops a recording.** The plan's keys say "R and Space to record and stop". The
  Swift app's rule is that Space never opens the microphone. Here, Space stops a take and
  plays or pauses otherwise, but never starts one.
- **Focus mode** (hiding the library) is outside §5 and not built.

What only the real Mac proves is outside this checklist: the UMIK-1 through PortAudio (S-1),
playback through a device, the bundle, light and dark rendered by macOS, and VoiceOver.
