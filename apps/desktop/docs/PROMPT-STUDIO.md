# The prompt studio

**Record → Prompts** is the prompt studio: the Swift app's recording workspace, rebuilt in Qt
over the corpus client (D-051, D-052; [RECORDING-V1.md](../../../docs/design/RECORDING-V1.md)
§5 and step 6). It is corpus-first (D-018): there are no sessions, groups or packages. A take
is recorded against one prompt of the catalog version the app pinned for this run. Save makes
it durable on this machine at once, and the corpus client sends it to the PC later
([CORPUS-CLIENT.md](CORPUS-CLIENT.md)).

This is step 6a: the fixed workspace and the record, stop and save loop. Step 6b adds the
take picker, the waveform, trims, playback, take history and the decisions beyond Save. Step
6c adds the keys.

## The workspace

The layout follows docs/NATIVE_RECORDING_UX.md §4. Every region keeps its place and size
whatever the take, the transport or the sync is doing. Only the window's size changes them:

- a taller window gives 70% of the extra height to the script and 30% to the waveform;
- a window shorter than the workspace shrinks the script card down to 120 points, then
  scrolls.

| Region | What |
| --- | --- |
| **Library** (left, 280 points) | `Library` with the covered count (`n / 1,886`), and where it comes from. Then **Continue Missing**, the search, **Show** (All prompts, Missing, Needs attention) and **Labels**. Then the catalog version this run records against, and the switch the next session makes. Then every prompt, with its status. |
| **Header** | The prompt's title and its label chips. Chips that do not fit collapse into `+N`, but the category always shows. The microphone is on the right. |
| **Script card** | For a spoken prompt, `PROMPT · READ ALOUD` and the text at 24 points. For room tone or a test step, a banner and "No spoken prompt" instead. The `INSTRUCTIONS · NOT READ ALOUD` section is always there, with the guidance and the duration. |
| Take picker, waveform, trim row, playback row | Held at their final size for step 6b. For now the picker's row says how many takes the prompt has. The waveform's frame turns red while recording. |
| **Timer** | The take's length, from the frames the worker confirmed (D-014), never a clock. |
| **Footer** | The 56-point Record/Stop control, then **Retake**, **Discard**, **Save** and **Save & Next**. Actions that do not apply stay in place, greyed out. |
| **Status line** | On the left, what the take is doing. On the right, the sync, or a notice. |

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
another version, the library says *Version N switches at the next session*. The switch
happens at the next start, or at **Switch now** (only between takes, never with one under
review). With no catalog cached and no PC, nothing can be recorded. The studio says *No
catalog yet* until the PC has answered once.

## The status line

The right half reads the corpus client's `SyncStatus`:

- `Uploads off until S-1 passes (corpus_sync) · N takes on this machine` while sending is
  held (`corpus_sync = false`, the default). This is a statement, not an error.
- `Offline · N queued · M sent · last synced HH:MM`.
- `N takes waiting for the PC's state` after the PC refused a change and its state is read
  again.
- Damaged takes and recordings an earlier run could not save come first.
- Otherwise `Waiting to sync`, `Syncing` or `Up to date`, with the queued and sent counts.

A notice from the client (*Changed elsewhere*, a change the PC cannot read, a refusal) takes
the right half until you press **Dismiss**.

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
| `studio/presentation.py` | How a prompt is shown: kind, banner, sections, labels, search. |
| `studio/library.py` | Statuses from the PC's library and the spool, coverage, filters, the next missing prompt. |
| `studio/status.py` | The status line's words. |
| `corpus/library.py` | The PC's library, cached on disk and read by each sync pass. |
| `qml/screens/StudioScreen.qml`, `qml/screens/studio/` | The workspace and its regions. |

What 6b and 6c attach to:

- `toggleRecord`, `saveNext` and `save` are what R (record, stop), Return (Save & Next) and
  Ctrl+S or ⌘S (Save) will call. The screen installs no shortcuts yet.
- `menuOpen` on the screen says when the Show or Labels menu is open, so keys can step aside.
- The placeholders (`studioTakePicker`, `studioWaveform`, `studioTrimRow`,
  `studioPlaybackRow`) are the regions 6b fills.
- `CorpusClient.decide`, `restore`, `set_trim` and `takes_for_prompt` are the calls 6b needs.

Tests: `tests/studio/` drives the screen offscreen over the PC rig
(`tests/corpus_client/corpus_rig.py`), with a stand-in recorder that leaves the worker's files
(`studio_fakes.py`). `test_studio_worker.py` runs the real worker on its simulated UMIK-1.
