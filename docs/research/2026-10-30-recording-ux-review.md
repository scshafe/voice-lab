# Catch bad takes at the microphone, not later

voice-lab's take loop gets the most improvement from **finding a bad take while the speaker is still on that prompt**. Today neither macOS recorder checks the input before a take. Neither gives a local quality verdict at review. In the Qt studio, problems surface only after sync, as *Needs review* from the PC's QC. By then the operator has usually moved several prompts on. That cost grows with **roughly 1,852 prompts and about 2 h 15 min of reading still to record** (as of the last documented count). The recorder is already fast: about 3 keystrokes per take and a sealed review in about 290 ms. The remaining friction sits in a few places:

- level and consistency checks;
- what a retake leaves behind;
- the absence of an opt-in rhythm mode;
- recording commands missing from the menu bar;
- a review loop that never brings flagged prompts back.

This report ranks nine improvements in order of impact. All new work targets the **Qt successor, `apps/desktop`** (voice-lab.app, Record → Prompts). The **Swift recorder in `apps/macos` stays the behavioural reference until the owner signs off parity** (Recording v1 step 10) and should not get new features. Two rules from the owner and the hardware shape every recommendation:

- **The app must never start recording on its own.** Every count-in, auto-arm or voice-activity feature below is off by default, opt-in, and visibly announced while active.
- **The UMIK-1's fixed low gain** puts raw takes near −50 dBFS. Level checks must therefore judge signal-to-noise and take-to-take consistency, or a level after a known gain, never raw ACX loudness targets.

## Two recorders share one loop, with about 1,850 prompts left

The macOS recorder is mid-handover. D-051 (2026-10-12) makes the Swift recorder "the reference recording interface". The Qt prompt studio is "rebuilt to its layout and behaviour". It replaces the Swift recorder once spike S-1 passes on macOS and parity is reached (`DECISIONS.md:1716-1719`; `docs/design/RECORDING-V1.md:9-15`). S-1 passed on the MacBook Air with the UMIK-1: **43/43 takes**, largest ADC deviation 1.736 frames (`docs/design/RECORDING-V1.md:504-509`). Parity was declared in offscreen tests on 2026-10-13, but the owner still has to sign it off on the Mac (`docs/design/RECORDING-V1.md:463-466`). Development has moved accordingly:

- `apps/desktop`: commits through 2026-10-27.
- `apps/macos`: last feature commit 2026-09-19.

So every change below is specified for the Qt studio. For each one, the report says whether it **adds** behaviour (safe to land before sign-off) or **changes** reference behaviour. A change should either wait for sign-off or be logged in the studio's list of deliberate differences (`apps/desktop/docs/PROMPT-STUDIO.md:277-282`), so the parity check stays honest.

### The take loop today

Both apps run the same loop:

1. Select the next missing prompt (or have it auto-selected).
2. Press **R**, read, then press **R** again (or Space in Qt) to stop.
3. Review the take.
4. Press **Return** for Save & Next.

Neither app arms the microphone automatically. Save & Next goes to the next prompt without a kept take and waits for R (`apps/desktop/src/voice_lab_desktop/screens/studio.py:934-936,1009-1018`). The timing is good:

| Step | p95 on the Air | Source |
|---|---|---|
| Record to first confirmed frames | 243.8 ms | `DECISIONS.md:1175-1186` |
| Stop to sealed review | 293.8 ms (target 300 ms, "the margin is thin") | `DECISIONS.md:1175-1186` |

The loop is well guarded. Continuity failures keep the partial take. Discard is limited to failed takes ("a good take is never deleted", `studio.py:744,862-871`). An unsaved trim always asks before it is lost (`qml/screens/StudioScreen.qml:69-104`).

### The remaining-prompts estimate

The active corpus catalog is version 2:

| | Prompts | Expected reading |
|---|---|---|
| Version 2 catalog | 1,886 | 8,376 s |
| Covered at last documented count | 34 | 282 s |
| **Remaining** | **1,852** | **about 8,094 s (~2 h 15 min)** |

Sources: `docs/CORPUS_STORE.md:229-232`; `migrations/active/corpus-store.md:34-36,93`.

**This estimate is uncertain, and it errs high.** The coverage count dates from the corpus-store migration in mid-September. The Qt studio has recorded and synced real corpus takes from the Air since 2026-10-14 (`docs/design/RECORDING-V1.md:510-511`). Any Swift-recorder takes made since September are also not in any document. The true remaining number is lower by an unknown amount. It is available only at runtime, from the studio's coverage line (`n / 1,886`) or `GET /api/v1/corpus/library`.

Session time is a further estimate. With 5–10 s of per-take overhead on top of reading, a full pass is roughly **5–7 hours at the keyboard**. That is an extrapolation from the audit, not a measured figure: no real-session timing study exists. Retakes are excluded. Microsoft's guidance that dataset talent "can only record for two or three hours a day" ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/record-custom-voice-samples)) puts that at **three or more sessions**. That makes resume, pacing and consistency first-class concerns.

## Ranked: nine changes, ordered by what they save across the remaining run

The ranking weighs two things: how often each problem recurs across about 1,850 prompts, and how expensive it is to discover late. A bad batch found after sync means re-reading prompts in a later session, which is the costliest failure. It also costs the most consistency, because tone drifts between days ([Coqui TTS docs](https://raw.githubusercontent.com/coqui-ai/TTS/dev/docs/source/what_makes_a_good_dataset.md)).

| # | Improvement | Adds or changes reference behaviour | Effort |
|---|---|---|---|
| 1 | Input check before a session; local level, consistency, clip and edge flags at review | Adds | M |
| 2 | One-key redo that retires the abandoned attempt (with ⌘Z) | Changes | S–M |
| 3 | A "Speak" cue and edge protection by default; an opt-in rhythm mode (count-in, auto-play, continuous, voice stop) | Adds; continuous mode needs an owner decision | M (L with voice stop) |
| 4 | A native Recording menu, visible key hints, more keys, Return accepted ahead of the seal | Adds | M |
| 5 | A flagged-prompt loop: split *Needs attention*, Next Flagged, an end-of-session review | Adds | S |
| 6 | Session pacing, break nudges and a match line | Adds | S–M |
| 7 | Open on the recorder and restore exactly where you left off | Changes (launch screen) | S |
| 8 | Keep any take from the picker, not only the newest | Changes | S–M |
| 9 | Status that says why, in place, plus a real Settings window | Adds | M |

Effort is rough: S is about a day, M a few days, L a week or more. It assumes the existing QML and Python structure of `apps/desktop`. Every verification step has to be done on the Mac: neither app runs on the Linux PC, and the audit read code without running either app.

### 1. Check the input before the session and flag weak takes at review

**Problem.** The studio has no level reading before a take:

- `SuiLevelMeter` shows only while recording (`qml/screens/StudioScreen.qml:524-546`).
- The level pin lives on the separate Measurement screen (`apps/desktop/README.md:19`).
- The microphone chip only says whether a UMIK-1 is present (`StudioScreen.qml:325-340`).
- At review the studio runs no local QC. *Needs review* means "its kept takes all failed the PC's QC" (`apps/desktop/docs/PROMPT-STUDIO.md:137-138`; `studio/library.py:23,98-100`).

Meanwhile low level is the corpus's documented chronic problem. "Raw UMIK-1 takes sit near −50 dBFS RMS". F5-TTS's −42 dBFS silence trim "discards nearly the whole clip" without the linear gain applied to training copies (`docs/FINETUNE.md:96-103`). The Swift recorder's QC flagged one take for sitting 0.196 dB under its −35 dBFS low-peak cutoff (`.claude/plans/2026-09-11-native-recording-ux.md:617-619`). That shows how a fixed absolute threshold misfires on this microphone.

**Practice.**

- Apple's Personal Voice starts with a **sound-quality check**: record a sample sentence and continue if it sounds right ([Hongkiat](https://www.hongkiat.com/blog/personal-voice-iphone/)).
- Audio Hijack treats calibrated Peak/RMS meters and **clipping indicators** as core recorder furniture ([Rogue Amoeba](https://weblog.rogueamoeba.com/2022/03/31/audio-hijack-4-has-arrived/)). Reviewers advise checking levels with a meter before recording ([Podfeet](https://www.podfeet.com/blog/2022/04/audio-hijack-4/)).
- Common Voice runs instant **TOO_SHORT / TOO_LONG / TOO_QUIET** checks on every clip ([speak.tsx](https://raw.githubusercontent.com/common-voice/common-voice/main/web/src/components/pages/contribution/speak/speak.tsx)).
- Microsoft's dataset checks judge **SNR above 35 dB** ("below 20 can result in obvious noise"), 100–200 ms of edge silence and clipping ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/record-custom-voice-samples)).
- The HIG asks for status "in the interface near what it describes", passively, and reserves alerts for critical problems ([HIG: Feedback](https://developer.apple.com/design/human-interface-guidelines/feedback)).

ACX's −23 to −18 dB RMS window ([ACX](https://help.acx.com/s/article/what-are-the-acx-audio-submission-requirements)) is the wrong yardstick here. The UMIK-1's gain is fixed in hardware, and its "sensitivity is indeed quite low" ([miniDSP community](https://www.minidsp.com/community/threads/increase-sensitivity-of-umik-1.13442/latest)). An ACX-calibrated "too quiet" warning would fire on every take and teach the operator to ignore it.

**Change.** The change has two parts.

The first is an explicit **Check Input** action. Run it at the start of a session and on resume: on demand only, with nothing running continuously between takes. It opens the UMIK-1 for a few seconds with a visible "Checking input, not recording" state. It measures the room's noise floor, then a spoken sample line, and shows:

- peak and RMS;
- SNR;
- the level the same audio would have **after the known training gain** in `FINETUNE.md`.

It writes no corpus take. It is an explicit, labelled action, so it respects the rule that the microphone never opens unasked. The per-take worker model (`DECISIONS.md:1051-1053`) is untouched.

The second part is a **local verdict at review**, computed on the sealed WAV off the GUI thread and shown as a badge on the take row and waveform:

| Flag | Fires when |
|---|---|
| Clip | Any sample at or near 0 dBFS. Uses a latched marker with a text label, not colour alone, per [HIG: Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility) |
| Low SNR | Speech RMS minus the session noise floor falls below a calibrated threshold |
| Inconsistent | Speech RMS drifts several dB from the rolling median of this session's recent kept takes |
| Short / long | The duration falls far outside the catalog's expected duration |
| Edge | Speech energy within the first or last ~100 ms, i.e. a likely cut onset or tail |

The thresholds should **reuse the PC QC's**, so the local flag predicts the later verdict. Any new thresholds are starting points, to be calibrated against the first hundred takes. Flags never block Save & Next.

**Target and verify.** Qt studio only; this adds behaviour, so it can land before sign-off. Check it on the Mac in four steps:

1. Run Check Input in the normal recording position and confirm a stable noise floor and SNR.
2. Record 20 ordinary prompts and confirm they produce no flags.
3. Deliberately record one take turned away from the mic, one with a knock near the capsule and one starting mid-word. Confirm each gets the matching flag.
4. After sync, compare the local flags with the PC's verdicts for the same takes. Agreement is the acceptance measure.

### 2. Give retake one key, and retire the abandoned attempt instead of hoarding it

**Problem.** The two apps disagree, and neither is clean:

- **Qt studio:** R during review starts a retake. The take under review is "saved as an undecided draft and its upload queued" (`studio.py:764-767,777-795,885-926`; `corpus/client.py:336-348`). The same happens when you leave the prompt. Drafts never count toward coverage, and a prompt with only drafts appears under *Needs attention* as *Draft* (`PROMPT-STUDIO.md:62,137-138`).
- **Swift recorder:** R in review is ignored (`RecordingWorkspaceState.swift:48-59`), and Retake is a button with no key (`RecordingWorkspaceReview.swift:158-181`).

Cleaning up an abandoned attempt in Qt takes about four clicks plus a typed reason, because Reject stays disabled until one is given (`qml/screens/studio/StudioTakePicker.qml:168-190`). Over a run with an unknown but non-trivial retake rate, every abandoned attempt becomes either clutter or chores.

**Practice.**

- Mimic Recording Studio re-takes with the same key and discards with **Esc** ([Record.js](https://raw.githubusercontent.com/MycroftAI/mimic-recording-studio/master/frontend/src/App/Record.js)).
- Common Voice re-records with number keys and discards with Esc ([speak.tsx](https://raw.githubusercontent.com/common-voice/common-voice/main/web/src/components/pages/contribution/speak/speak.tsx)).
- Logic's take folders keep every pass non-destructively and make the chosen one explicit ([Logic Pro guide](https://support.apple.com/guide/logicpro/aside/lgcp7629240f/mac)).
- Audio Hijack separates "Remove From List" from "Move to Trash" ([Audio Hijack manual](https://rogueamoeba.com/support/manuals/audiohijack?print=true)).
- The HIG prefers undo to confirmation and asks for undo "since the last logical step" ([HIG: Undo and redo](https://developer.apple.com/design/human-interface-guidelines/undo-and-redo)).

**Change.**

- In review, **R means Redo**. It starts a new attempt and marks the attempt under review **rejected with the automatic reason "superseded by retake"**, with no dialog and no typing. That is a state change, never a deletion, so it keeps "a good take is never deleted".
- **Esc while recording** stops and retires that attempt in the same way.
- **⌘Z** reverses the last keep, retire or redo, and the result is shown in place.
- The picker groups retired attempts under the kept take, like a take folder, so *Needs attention* no longer fills with drafts.
- Whether retired attempts still upload is an owner decision. Uploading preserves data; deferring saves bandwidth.

**Target and verify.** Qt studio. This changes reference behaviour, so log it as a deliberate difference or land it after sign-off. Leave the Swift recorder as it is. Check it on the Mac with R, R, R, R, Return on one prompt:

1. The picker shows one kept take and one "superseded" take.
2. No reason prompt appears.
3. The prompt is absent from *Needs attention*.
4. ⌘Z restores the earlier attempt to review.

### 3. Cue the speaker and protect the edges by default; offer rhythm only on request

**Problem.** The take loop has no count-in, pre-roll or tail hold; a grep for count-in, countdown and pre-roll finds nothing in either app. The microphone opens on R, and a fresh worker reaches first frames at about 244 ms p95 (`DECISIONS.md:1178-1179`). The UI does say "Starting the microphone" until frames arrive (`studio.py:73-79,834-835`). Even so, speaking on the keypress risks a cut first syllable, and stopping early risks a cut tail. Neither problem is documented in the new apps, so this is an inference. The other source of friction is the deliberately manual rhythm. The owner's 2026-09-07 plan says **"Never auto-record after Keep or Retry"** (`.claude/plans/2026-09-07-desktop-recording-experience.md:25`; also `docs/RECORDING_EDITOR_UX.md:89-90`, `docs/NATIVE_RECORDING_UX.md:16-17`).

**Practice.**

- Common Voice holds the stop **500 ms** and Piper holds it **1 s**, because people stop early ([speak.tsx](https://raw.githubusercontent.com/common-voice/common-voice/main/web/src/components/pages/contribution/speak/speak.tsx); [Piper record.html](https://raw.githubusercontent.com/rhasspy/piper-recording-studio/master/piper_recording_studio/templates/record.html)).
- Microsoft wants 100–200 ms of silence at each edge and a breath before each utterance ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/record-custom-voice-samples)).
- Logic offers a count-in toggle on **Shift-K** ([Logic Pro guide](https://help.apple.com/logicpro/mac/10.1/en.lproj/lgcpbe8774d7.html)).
- Apple Personal Voice's default **Continuous Recording** arms the next phrase "once the new phrase pops up", with **Stop at Each Phrase** as the alternative ([iPhoneLife](https://iphonelife.com/content/personal-voice-iphone)). Guides put its 150 phrases at about 15 minutes, or roughly 600 per hour ([Hongkiat](https://www.hongkiat.com/blog/personal-voice-iphone/)); others say 25–30 minutes.
- Mimic ends takes on voice-activity detection ([Record.js](https://raw.githubusercontent.com/MycroftAI/mimic-recording-studio/master/frontend/src/App/Record.js)).
- Audacity's level-triggered recording starts too soon when the threshold sits near background noise ([Audacity forum](https://forum.audacityteam.org/t/sound-activated-recording-starts-too-soon-solved/43438)).

**Change, by default (no automatic start).**

- After R, show a distinct **"Speak"** cue on the Record button and script card only once frames are confirmed plus about 200 ms. That guarantees head silence and gives the eye a go signal.
- Add a short **tail hold**, about 300 ms, before sealing. It costs latency against the 300 ms stop target, so measure it.
- *Suggest*, never apply, trim points at the speech edges. The owner explicitly chose explicit **Save trim** over auto-applied trims (`docs/RECORDING_EDITOR_UX.md:13-67`).

**Change, opt-in only.** A **Rhythm** submenu holds four checkable modes. All are off at every launch, never persisted on, and shown while active as a persistent header badge and a checkmark in the menu:

1. **Count-in after R.** Shows a visual 3-2-1 before the take; R still starts it.
2. **Auto-play on stop.** Plays the take back as soon as it seals.
3. **Continuous.** After Save & Next, the next prompt shows a visible countdown ring, about 2 s, labelled "Recording in 2…". Esc or Space cancels it. The mode switches itself off on any error, failed take, window deactivation, catalog switch or relaunch.
4. **Voice stop.** Ends the take after trailing silence. Its threshold is set relative to the noise floor from item 1, not to an absolute dB value.

Continuous mode reverses a default the owner set. It should ship only if the owner agrees, and only as an explicit opt-in for one session.

**Target and verify.** Qt studio. The cue and tail hold add behaviour; the rhythm modes are owner-gated. The Swift recorder is unchanged. On the Mac, first test the off state. With every rhythm mode off, Save & Next, Retake, Continue Missing, switching prompts, relaunch and wake from sleep must never start a recording. Then turn Continuous on and check four things:

1. The countdown is visible.
2. Esc cancels it.
3. The mode is off after relaunch.
4. Prompts per hour against a timed manual session of the same length.

### 4. Put every recording command in a native menu, and show the keys

**Problem.** The Qt studio has **no menu bar or menus** (grep of `apps/desktop/src/voice_lab_desktop/qml`). It also has no tooltips or key hints. It ignores scshafe-qt's existing shortcut overlay (`docs/design/VOICE-PLATFORM.md:856-857`). It binds only R, Space, Return/Enter and ⌘S (`qml/screens/StudioScreen.qml:40-67`). Several common actions have no key:

- Continue Missing;
- previous or next prompt without saving;
- Reject;
- a sidebar toggle (focus mode is "not built", `PROMPT-STUDIO.md:282`).

Return before the take seals is a no-op (`studio.py:733-743`), and the Swift recorder drops it silently (`RecordingWorkspaceModel.swift:327,615`). A fast operator's Save & Next keystroke can vanish without notice.

**Practice.** The HIG's app-specific menu guidance says: "Even when commands are available elsewhere in your app, it's important to list them in the menu bar… lets you assign keyboard shortcuts to them, and makes them more accessible to people using Full Keyboard Access." It also says to disable unavailable items rather than hide them ([HIG: The menu bar](https://developer.apple.com/design/human-interface-guidelines/the-menu-bar)). Custom shortcuts go only to the most frequent commands, without repurposing standard ones ([HIG: Keyboards](https://developer.apple.com/design/human-interface-guidelines/keyboards)). Audio Hijack keeps its transport in a Control menu with shortcuts ([Audio Hijack manual](https://rogueamoeba.com/support/manuals/audiohijack?print=true)). Piper prints the key on each button, as in "Finish [f]" ([record.html](https://raw.githubusercontent.com/rhasspy/piper-recording-studio/master/piper_recording_studio/templates/record.html)).

**Change.** Add a native macOS menu bar in the HIG's standard order. A **Recording** menu between View and Window holds:

- Record/Stop (R);
- Play/Pause (Space);
- Save (⌘S);
- Save & Next (Return);
- Redo;
- Reject Take;
- Check Input;
- Continue Missing;
- Previous/Next Prompt;
- the Rhythm submenu.

Every item is disabled, not hidden, when the phase forbids it. View gets Show/Hide Sidebar and text size. A Window menu with Minimize and Zoom is required even for one window. Pick new shortcuts against the HIG's standard table. Each transport button shows its key glyph, and the overlay opens with ⌘/.

Accept **type-ahead**: a Return pressed during "Stopping · checking the audio" is queued and applied when the seal lands. The status line says "Will save when ready" while it waits.

**Target and verify.** Qt studio; this adds behaviour. Recording the Swift recorder's own minimal menu (`RecordingApp.swift:21-33`) as a baseline gap is enough. Check on the Mac:

1. Each item enables and disables correctly through Ready → Recording → Stopping → Review.
2. With Full Keyboard Access on, the whole loop can be driven from menus.
3. VoiceOver reads the menu items.
4. A shortcut can be remapped under System Settings → Keyboard → Keyboard Shortcuts → App Shortcuts. That only works with real native menus.
5. A Return pressed within 100 ms of Stop still saves and advances.

### 5. Close the loop on flagged prompts instead of leaving them in a mixed bin

**Problem.** The PC's QC verdicts arrive after background sync. Sync runs about a second after a change and backs off to 5 minutes on retries (`apps/desktop/docs/CORPUS-CLIENT.md:112-125`). By then the operator has moved on. *Needs attention* mixes three different states: drafts, takes that failed the PC's QC, and unknown status (`PROMPT-STUDIO.md:137-138`). Nothing brings the operator back to a flagged prompt. The Swift recorder's "Needs review gates nothing" (`docs/NATIVE_RECORDING_UX.md:418`) has the same gap.

**Practice.** For large scripts, Microsoft recommends to "simply note any utterances with issues… You can always go back… and record the missed samples later" rather than stopping the flow ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/record-custom-voice-samples)). The HIG wants passive status near the item it describes ([HIG: Feedback](https://developer.apple.com/design/human-interface-guidelines/feedback)).

**Change.**

- Split the *Show* filter into **Flagged here** (item 1's local flags), **Failed PC QC** and **Drafts only**.
- Add **Next Flagged** to the Recording menu.
- Badge a sidebar row when a PC verdict lands for a prompt recorded this session.
- At the end of a session, or on demand, show a non-modal "7 prompts flagged this session · Review now" entry that walks them in order using the normal redo key.

**Target and verify.** Qt studio; this adds behaviour. On the Mac, record a session that includes a deliberately bad take, sync it, and confirm three things:

1. The prompt moves into Failed PC QC.
2. Next Flagged reaches it.
3. A redo plus Return clears it from the list.

### 6. Pace the reader across sessions and keep the voice consistent

**Problem.** The only progress signal is library coverage, `n / 1,886` (`PROMPT-STUDIO.md:38`). Nothing shows takes this session, time recording, reading time left or a break cue, and nothing helps the voice sound the same on day three as on day one.

**Practice.**

- Microsoft limits talent to two or three hours a day with regular breaks. It also recommends a **match file**: a reference utterance repeated "every page or so", "especially important when you resume recording after a break or on another day" ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/record-custom-voice-samples)).
- Mimic advises a break every 30 minutes and warns that "rushing lowers quality". It also shows a pace check against the speaker's own history ([Mimic README](https://github.com/MycroftAI/mimic-recording-studio)).
- Coqui warns that tone and pitch drift across clips degrades training ([Coqui TTS docs](https://raw.githubusercontent.com/coqui-ai/TTS/dev/docs/source/what_makes_a_good_dataset.md)).

**Change.**

- A compact session strip in the header, e.g. "Session · 42 kept · 3 redone · 31 min · 1,610 left ≈ 1 h 58 min reading". The last two figures are computed from the catalog's expected durations.
- A quiet break nudge at about 30 minutes. It is a strip message, never a dialog.
- An optional **match line**: a fixed reference sentence, played back from the first session, then re-read at session start and on resume. Its level, SNR and duration are compared with the reference on the spot.
- Match-line recordings stay local and outside the corpus. The catalog no longer holds room tone or non-prompt material (D-017, `DECISIONS.md:297-299`), so keeping them separate respects that choice.

**Target and verify.** Qt studio; this adds behaviour. On the Mac, run a 35-minute session and confirm:

1. The strip counts match the picker.
2. The nudge appears once.
3. A match line re-read the next day reports a level difference within the item 1 consistency tolerance.

### 7. Open straight onto the next prompt and restore the window

**Problem.** The Qt app opens on Corpus or Status and never on Prompts (`apps/desktop/src/voice_lab_desktop/app.py:221`). Its only start-screen flag is `--measure` (`cli.py:69,170`). Only the reading size persists (`studio.py:1050-1076`). Nothing in `apps/desktop/src` uses QSettings or restoreGeometry, so the window frame, filter and search reset on every launch. The Swift recorder already restores the last prompt and take through its service cursor (`RecordingWorkspaceModel.swift:401-405,894-904`). Here the reference is ahead of the successor.

**Practice.** "Restore the previous state when your app restarts… display windows in the same state and location" ([HIG: Launching](https://developer.apple.com/design/human-interface-guidelines/launching)). Apple's Personal Voice saves progress implicitly and resumes with **Continue Recording** ([Apple Support](https://support.apple.com/en-us/104993)).

**Change.** When the operator is signed in and a catalog is cached, launch onto Record → Prompts with these restored:

- the last prompt, or the first missing one;
- window geometry;
- the sidebar width and filter;
- the reading size.

Keyboard focus lands so that R records at once. Persist the window title too: the HIG says not to title windows with the app name ([HIG: Toolbars](https://developer.apple.com/design/human-interface-guidelines/toolbars)), so title it with the current prompt's catalog ID.

**Target and verify.** Qt studio. This changes the launch screen; it closes a parity gap rather than opening one. On the Mac:

1. Quit mid-session, relaunch, and confirm the same prompt, frame and filter with zero clicks before R.
2. Repeat after a reboot.

### 8. Let the operator keep the better take, not only the newest

**Problem.** Save and Save & Next work only when the selected take *is* the review take (`studio.py:733-743`). If the operator opens the picker to compare an earlier attempt, the keep path is disabled until the newest take is reselected. That is the wrong way round when the earlier read was better.

**Practice.** Logic's take folders and Pro Tools playlists keep every pass and make the chosen take an explicit, non-destructive selection ([Logic Pro guide](https://support.apple.com/guide/logicpro/aside/lgcp7629240f/mac); [Production Expert](https://www.production-expert.com/home-page/2018/5/10/tutorial-how-to-loop-record-and-use-track-comping-for-adr-in-pro-tools)). Common Voice uses number keys to act on a specific clip in its batch ([speak.tsx](https://raw.githubusercontent.com/common-voice/common-voice/main/web/src/components/pages/contribution/speak/speak.tsx)).

**Change.**

- Number keys **1–9** audition this prompt's takes, newest first.
- **Return** keeps whichever take is selected and advances. Item 2's retire step then applies to the other undecided attempts.
- The picker marks the take that will be kept, so the choice is never ambiguous.

**Target and verify.** Qt studio; this changes reference behaviour, so log it as a deliberate difference. On the Mac, record three attempts and press 2, then Return. Confirm that attempt 2 is the kept take, the others are retired, and coverage counts the prompt once.

### 9. Say why, in place, and move configuration into Settings

**Problem.** The Qt status region has two lines. Take state, errors and sync share it, with one action button at most (`PROMPT-STUDIO.md:47,174-199`). The Swift recorder's single truncated line also carries fine-tune progress (`RecordingStatusRegion.swift:56-90`).

The owner has repeatedly hit "it didn't say why" moments. The first corpus take on the Air "sat *queued* with no attempt, because no approval stood… and the studio did not say why" (`docs/design/RECORDING-V1.md:510-520`). First-run setup still means hand-editing `corpus_sync = true` into `config.toml` (`apps/desktop/docs/FIRST-RUN.md:19-34`; `apps/desktop/README.md:80-100`).

**Practice.**

- "Show people when a command can't be carried out and help them understand why", and put status near what it describes ([HIG: Feedback](https://developer.apple.com/design/human-interface-guidelines/feedback)).
- Settings opens with ⌘, in its own window, remembers its pane, and holds only a few settings ([HIG: Settings](https://developer.apple.com/design/human-interface-guidelines/settings)).
- Practitioners name a Settings window "not a tab" as a basic mark of a good Mac citizen ([Coyote Tracks](https://coyotetracks.org/blog/app-feel-on-mac/)).

**Change.**

- Give **take state and its flags** the waveform's own caption.
- Move **sync** into a separate, always-present badge that carries its own single action: Approve, Sync now or Details.
- When Record is disabled, the button's tooltip names the blocker. The Swift recorder already does this (`RecordingTransportControls.swift:14-55`).
- Add a **Settings window** (⌘,) holding the server, uploads (the `corpus_sync` toggle and approval), the input device, the Rhythm defaults kept off, and the item 1 thresholds.

**Target and verify.** Qt studio; this adds behaviour. On the Mac, with approval revoked, record a take. Then confirm:

1. The sync badge alone explains the queue and offers Approve.
2. Errors never overwrite the take caption.
3. ⌘, opens Settings on the last-used pane.
4. A fresh install reaches a synced take without editing a file.

## The take loop today versus proposed

Counts are keystrokes on the happy path. "Listen" means Space to play and Space to pause. Today's figures are from the audit's walk-through of the code (`StudioScreen.qml:40-67`; `studio.py:733-743,885-926`; `RecordingWorkspaceState.swift:48-59`; `StudioTakePicker.qml:168-190`).

| Step | Qt studio today | Swift recorder today | Proposed, manual (default) | Proposed, Continuous opt-in |
|---|---|---|---|---|
| Start a session | 1 click to Prompts, then R | 0 clicks if the cursor restores, else 1 click | 0 clicks (item 7), optional Check Input | Same, plus turning the mode on via the menu |
| One take, no listen | 3 (R, R, Return) | 3 (R, R, Return) | 3, with Return accepted before the seal | 2 (stop, Return), or 1 (Return) with Voice stop |
| One take, with listen | 5 | 5 | 3–5: listen mainly when a take is flagged | 2–3, or Auto-play with no extra keys |
| Retake from review | +2 (R, R), and the old attempt becomes a synced undecided draft | +1 click + 3 keys (Retake has no key) | +2 (R, R); the old attempt is retired with no typing | +2 (R, then stop) |
| Tidy one abandoned attempt | ~4 clicks + a typed reason | 3 clicks | 0 (done by Redo); ⌘Z to reverse | 0 |
| Keep an earlier take | Not directly: reselect the review take first | Picker popover | 1–9, then Return | 1–9, then Return |

Across 1,852 prompts, the default manual loop stays at about **5,550 keystrokes**, the same as today's floor. The gain comes from fewer listens, no cleanup and fewer late re-records, not from fewer presses. The opt-in Continuous mode would bring the floor to roughly **3,700 keys**, or about **1,850 with Voice stop**, against today's 5,500–9,300. It does that without ever starting a take the operator did not choose to arm for that session. Whether this run should use that mode is the owner's call, given the recorded rule.

## Stale documents point readers at the wrong recorder

Two top-level documents now describe a world that no longer exists:

- `CURRENT.md:27-32` says the native Swift UX is "planned, not implemented; the existing desktop remains the current application".
- `docs/NATIVE_RECORDING_UX.md:3-5` says "proposed… The application still uses Qt".

Both predate the Swift recorder's build-out and the October Qt studio. Under D-051 they are now doubly wrong: the Swift recorder exists, and it is the one being retired. The Swift plan's open Step 17, "cutover and Qt removal" (`.claude/plans/2026-09-11-native-recording-ux.md:312,629`), runs in the opposite direction to D-051 and should be marked superseded.

No document records three things:

- which recorder the owner uses day to day;
- whether the Mac → PC import (D-053) has run;
- current coverage. The RECORDING-V1 progress log ends 2026-10-15.

Updating these is cheap and would also fix the remaining-prompts uncertainty above. A one-line coverage entry after each session would turn "about 1,852, probably fewer" into a real number.

## Conclusion

The current design is fast and data-safe. What it lacks is a feedback loop for the speaker. The owner's own record shows the pattern: surprise and silence ("it didn't say why") cost more than milliseconds do. Items 1, 5 and 9 address that directly. Items 2, 4 and 8 remove the small chores that compound over thousands of takes. Item 3 is the only one that trades against a stated owner rule, which is why everything in it beyond the "Speak" cue is optional, announced while on, and off at every launch.

Two points follow for planning:

- **Calibrate the level checks before building on them.** Item 1's value depends on SNR and consistency thresholds that suit a fixed-gain measurement microphone. Those thresholds can only be set from the first hundred real takes on the Mac.
- **Sequence the work around the parity sign-off.** Items that only *add* behaviour (1, 4, 5, 6, 9) can land now and make sign-off easier. Items that *change* the reference loop (2, 3's rhythm modes, 7, 8) should follow the sign-off, or be logged as deliberate differences, so the owner judges parity against a stable target.

Microsoft notes that past a few hundred utterances more data mainly reduces glitches rather than raising naturalness ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/record-custom-voice-samples)). Consistency across the remaining sessions may therefore matter more than raw speed through them.
