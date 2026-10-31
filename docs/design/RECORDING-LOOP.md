# The prompt studio's recording loop (D-058)

**Requested 2026-10-30** by the owner, from the recording UX review
([docs/research/2026-10-30-recording-ux-review.md](../research/2026-10-30-recording-ux-review.md)).
The owner's decisions are D-058. This file is the checklist; each PR ticks its line and says
what the Mac must check.

## Context

- **Target:** the Qt prompt studio only (`apps/desktop`, voice-lab.app, Record → Prompts). The
  Swift recorder (`apps/macos`) is the reference until parity is signed off (D-051) and is not
  touched.
- **The core loop** (open the prompts, R, R or Space, Return, again) gets faster, never
  slower. Its acceptance test (`apps/desktop/tests/studio/test_studio_core_loop.py`, PR 2)
  runs in every later PR's verify.
- **The app never starts recording on its own.** No rhythm mode is built (D-058).
- **Changes to the reference behaviour** (the inspector, Redo, keys 1–9, edge protection, the
  launch screen) are logged as deliberate differences in `apps/desktop/docs/PROMPT-STUDIO.md`'s
  parity checklist, so the owner's parity check stays honest.
- **Not settled by the owner, so today's behaviour stays:** an attempt retired by Redo is saved
  and its upload queued, as a retake's draft is today (the review's open question on
  uploading retired attempts).

## Steps

One PR each, verified (`scripts/verify`, `dev check .`), CI green, merged with
`Merge #N: <title>`, the deploy watched.

| # | What | Review item | Kind |
| --- | --- | --- | --- |
| 1 | The review in `docs/research/`, D-058, this plan; the stale documents (CURRENT.md, docs/NATIVE_RECORDING_UX.md, the Swift plan's step 17) | — | docs |
| 2 | The core loop's acceptance test; a Return (or ⌘S) pressed while the take seals is queued and applied when it lands ("Will save when ready") | B, 4 | adds |
| 3 | Quality flags at review, off the GUI thread, never blocking: the PC's own QC on the sealed take (`qc_metrics`), clipping, low SNR, inconsistency with this session's recent kept takes, short or long against the catalog's expected duration, cut edges. **Check Input**: the room's floor, a spoken line, SNR and the level after the training gain; no corpus take | 1 | adds |
| 4 | The menu bar in the HIG's order, with a Recording menu holding every command and its key, disabled (not hidden) when the phase forbids it; key hints on the transport; ⌘/ opens the shortcut overlay | 4 | adds |
| 5 | Flagged prompts: Show gains Flagged here, Failed PC QC and Drafts only; **Next Flagged**; a row's badge when the PC's verdict lands for a prompt recorded this session; "N prompts flagged this session · Review now" | 5 | adds |
| 6 | The session strip (kept, redone, minutes, prompts left and their reading time) and a quiet break hint at about 30 minutes; the match line, off by default | 6 | adds |
| 7 | The sync badge, apart from the status line, with its one action; the take's caption keeps the take's state; a disabled Record names its blocker; **Settings** (⌘,): server, uploads (`corpus_sync`, Approve), the input, the match line | 9 | adds |
| 8 | The inspector (⌥⌘I): take history and picking, the flags' details, long delivery notes, pacing, sync. The centre keeps the prompt, the waveform and Record, Retake, Save & Next. The library collapses (⌃⌘S, Focus mode) | D | changes the layout |
| 9 | R in review is Redo: the attempt under review is rejected as "superseded by retake", no dialog, no typing; ⌘Z reverses the last keep, retire or redo; the picker groups retired attempts under the take kept | 2 | changes |
| 10 | Keys 1–9 audition this prompt's takes, newest first; Return keeps the selected take (the other undecided attempts are retired as in 9); the picker marks the take Return keeps | 8 | changes |
| 11 | Edge protection: a pre-roll (the Speak cue on the Record control and the script card once frames are confirmed and about 200 ms have passed) and a post-roll before the seal | 3 | changes |
| 12 | Open on Record → Prompts at the last prompt (or the first missing), with the window's frame, the library's width and filter, the reading size and the panes restored; R records at once; the window is titled with the prompt's id | 7 | changes |

## Progress

- [x] 1: the review, D-058, this plan, the stale documents
- [x] 2: the core loop's acceptance test; a Save typed ahead before the seal
- [ ] 3
- [ ] 4
- [ ] 5
- [ ] 6
- [ ] 7
- [ ] 8
- [ ] 9
- [ ] 10
- [ ] 11
- [ ] 12

## What the Mac must check

Filled in by each PR; the last one hands the whole list to the owner.

- **The core loop (2).** With the UMIK-1 and the real worker: R, read, R, Return as fast as
  you can, ten prompts in a row. Each Return pressed within about 100 ms of the stop saves and
  goes on (the status line says *Stopping · will save when ready* while it waits); no prompt
  is skipped and none is saved twice.
