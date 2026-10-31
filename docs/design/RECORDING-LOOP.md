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
- [x] 3: quality flags at review; Check Input
- [x] 4: the menu bar, key hints, ⌘/; ⌘J, ⌘↑ and ⌘↓; the View menu's text sizes
- [x] 5: Show's Flagged here, Failed PC QC and Drafts only; Next Flagged; badges; the flagged pass
- [x] 6: the pace strip ("So far · …"), the break hint, the match line (off by default)
- [x] 7: the sync badge, the take's caption, Record's blocker; Settings (⌘,)
- [x] 8: the inspector (⌥⌘I) and the library that folds away (⌃⌘S, Focus)
- [x] 9: R in review is Redo (the attempt set aside, no dialog); ⌘Z reverses
- [x] 10: keys 1–9 audition the prompt's takes; Return keeps the one picked
- [ ] 11
- [ ] 12

## What the Mac must check

Filled in by each PR; the last one hands the whole list to the owner.

- **The core loop (2).** With the UMIK-1 and the real worker: R, read, R, Return as fast as
  you can, ten prompts in a row. Each Return pressed within about 100 ms of the stop saves and
  goes on (the status line says *Stopping · will save when ready* while it waits); no prompt
  is skipped and none is saved twice.
- **Quality flags and Check Input (3).** In the normal recording position:
  1. Check input: the room's floor and the SNR are stable over two or three checks, and the
     verdict reads *Good to record* (or says why not).
  2. Record 20 ordinary prompts: none is flagged. A flag that fires on ordinary takes is a
     threshold to calibrate (`studio/quality.py`), not a fault in the take. If *PC QC* fires
     for the PC's −35 dBFS peak on ordinary takes, that is the PC's own threshold: the owner
     decides whether the PC should change it.
  3. On purpose: one take turned away from the microphone (*Low SNR*), one with a knock near
     the capsule (*Clipped*), one started mid-word (*Cut start*). Each gets its flag.
  4. After they sync, the PC's verdicts agree with the local *PC QC* and *Clipped* flags.
- **The menu bar (4).**
  1. The menu bar reads File, View, Recording, Window, Help (after the app's menu), each
     Recording item with its key, and each item enables and disables correctly through
     Ready → Recording → Stopping → Review.
  2. With Full Keyboard Access on (System Settings → Keyboard), the whole loop runs from the
     menus: Record, Stop, Save & Next. VoiceOver reads the items.
  3. A key remapped under System Settings → Keyboard → Keyboard Shortcuts → App Shortcuts
     (for example Save & Next) runs its item.
  4. Every key runs its command once, never twice: R records (it does not record and stop at
     once), ⌘S saves once, ⌘↓ moves one prompt. Typing R or Return in the library's search
     never records or saves.
  5. ⌘/ shows the shortcuts; the buttons show their keys (⌘S, ↩).
- **Flagged prompts (5).** Record a session that includes a deliberately bad take (one the
  PC's QC fails: turned away, or very quiet), let it sync, and check:
  1. its row gets the *Failed PC QC* badge, and Show → Failed PC QC lists it;
  2. Next Flagged (⇧⌘J) reaches it;
  3. a redo and Return clear it from the list, and the line *N prompts flagged since you
     started · Review now* walks the rest with R, R, Return.
- **Pace and the match line (6).** Over a sitting of about 35 minutes:
  1. the strip's counts match the picker (kept, redone), and its minutes the clock;
  2. the break hint appears once, quietly, and OK puts it away;
  3. with the match line on, the next day's Check Input reports a level within the
     consistency tolerance (6 dB) of the first day's reference, and Play reference plays it.
- **Say why, and Settings (9).** With the approval revoked, record a take, then:
  1. the sync badge alone explains the queue (*Needs your OK*) and offers Approve;
  2. errors never overwrite the take's caption under the waveform;
  3. ⌘, opens Settings, on the pane last used, from the app's menu as well;
  4. on a fresh install, the Uploads pane's *Send takes to the PC* and Approve reach a synced
     take without editing a file.
- **The layout (8).** At the smallest window and at full screen:
  1. ⌥⌘I and ⌃⌘S (and the header's two buttons) hide and show the inspector and the library,
     the View menu says which, and both stay as left after a relaunch;
  2. with both shown on the smallest window, the footer and the header still read whole;
  3. the core loop is no slower with the inspector shown: the keys, the focus and Return work
     as before.
- **Redo and ⌘Z (9).** On one prompt, R, R, R, R, Return:
  1. the picker shows one kept take and, under it, one *Rejected (superseded by retake)*;
  2. no reason was asked for;
  3. the prompt is not under Needs attention;
  4. ⌘Z brings the kept take back as a draft, and ⌘Z again the one set aside.
- **Keys 1–9 (10).** Record three attempts of one prompt (R, R three times), press 2, then
  Return: attempt 2 is the kept take, the other two are set aside, the picker had marked
  attempt 2 (*Return keeps*) and coverage counts the prompt once. Each key plays its take.
