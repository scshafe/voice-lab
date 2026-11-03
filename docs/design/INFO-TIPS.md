# Info tips, a hideable sidebar and a compact list header (D-059)

The owner's work of 2026-11-02, for the Qt desktop app (`apps/desktop`): a less cluttered app.
Explanations move behind small info icons (ⓘ) whose popover shows the details on hover. The
left sidebar hides and comes back on hover. The prompt library's header becomes a search field
and a filter icon.

## The rule

Every explanatory or secondary text on every page and pane is inventoried below. Each one
either stays visible, moves behind an ⓘ, or is dropped as redundant.

- **Needed during a recording or in the loop: stays visible.** The minimal loop is open
  Record → Prompts, R, R or Space, Return. It gains no click, no focus trap and no key conflict.
- **Status that changes stays visible.** This covers the sync badge, blockers, flags, errors
  and live readings. Only its explanation moves.
- **An explanation moves behind an ⓘ.** The ⓘ sits beside the heading, label, title or chip
  it explains.
- **Long-form reading collapses.** The Journey's education card is too long for a popover. It
  collapses under a disclosure, closed by default.
- **Dropped** only when something visible already says it, named in the row.
- **The Swift reference app** (`apps/macos`) changes only where it already shows the same thing.
  That is the library's Show picker and Labels menu, which become its filter menu (step 4).
  The Mac builds the Swift app.

## The components come from scshafe-qt 0.10

The owner wants the three components in scshafe-qt (`Scshafe.Ui`), not in the app. The owner
has asked scshafe-qt's agent for a 0.10.0 release with:

- **An ⓘ with a help popover.**
  - It opens on hover after a short delay (about 300 ms), and at once when another tip is
    already open.
  - A click pins it open.
  - From the keyboard: Tab reaches it, and Space or Return opens it. Esc closes it.
  - A hover or a click never takes the focus. A focused tip claims only Space and Return, so
    R, Space and Return stay the studio's.
  - A press outside closes it and still reaches what was pressed.
  - VoiceOver reads its name and the explanation.
  - It also comes as an `info` property on checkboxes, switches, fields, selects, dialogs,
    empty states and headings.
- **A sidebar that hides and comes back on hover as an overlay.** The hot zone is the very
  top-left of the window, below the title bar and never over the traffic lights. The app keeps
  ⌃⌘S.
- **A compact list header.** It is a search field plus a funnel icon. The funnel's popover
  holds the filters and a Clear. The funnel shows when a filter is active.

The exact needs, with every behaviour above, were written as an agent request. The broker
refused to file it, because scshafe-qt does not accept agent requests (2026-11-02), so the owner
relays it. The app bumps to scshafe-qt 0.10 once it is on PyPI. None of the three is built in
the app.

## Steps

One small PR each. CI must be green before each merge, and each deploy is watched.

1. **This plan.** D-059, and the Mac's checks added to
   [RECORDING-LOOP.md](RECORDING-LOOP.md) § "What the Mac must check".
2. **Drops and stale wording.** These need no component; each is marked *drop* or *stale*
   below. "On the Status screen" now reads "in Settings (⌘,)": Settings is its own window
   since D-058.
3. **scshafe-qt 0.10.** This bumps both `pyproject.toml`s and `apps/desktop/uv.lock`, from which
   the Mac bundle's hash-locked closure is exported. There is no visible change.
4. **Record → Prompts.**
   - The library header: the search plus the funnel, which holds Show and Labels, with Clear.
   - The catalog and source lines go behind an ⓘ.
   - The hideable sidebar and its hover reveal.
   - The waveform's legend.
   - The inspector.
   - Check Input.
   - The Swift app's filter menu.
5. **The Settings window** and the Approve dialog.
6. **Status and Uploads.** Also the sign-in placeholder and the player bar.
7. **Measurement.**
8. **Corpus.**
9. **Journey.**
10. **Practice and Referee.**
11. **The legacy sessions screen** (`prompts_mode = "sessions"`).

Each page's PR adds tests:

- the moved text is the ⓘ's, and opens on hover, on a click and from the keyboard (the
  component's own behaviour is scshafe-qt's to test);
- nothing it explains is lost;
- the core loop's acceptance test (`test_the_core_loop_rapid_fire`) passes unchanged.

## Progress

- [x] 1. The plan (#141).
- [x] 2. Drops and stale wording: the sign-in placeholder, the approval and Uploads ("in
  Settings"), Measurement's distance, its level message when it matches the pin and its
  empty state's first sentence, the Practice why chips and goals line, the legacy screen's
  three; the Uploads banner keeps its title while the approval row says why.
- [x] 3. scshafe-qt 0.10.0 (`SuiInfoTip`, `SuiListHeader`, and `SuiAppShell`'s hideable
  sidebar). The lock's sha256s match the release's `SHA256SUMS`.
- [ ] 4. Record → Prompts.
  - [x] The library header (`SuiListHeader`): the search (⌘F) and the funnel, which holds Show
    and Labels, with Clear filters. Clear leaves the search.
  - [x] The library's ⓘ (`InfoTip`, `VoiceLab.Controls`): the catalog line and the count's
    source. The count is tinted *warn* while it is partial, since `SuiInfoTip` has no tone.
    The switch notice shows only while it has something to say.
  - [x] The waveform's legend (its scale, behind an ⓘ that keeps its place left of the
    caption), the inspector (the flags by name; why and the levels behind the ⓘ beside
    "Flags"; the sync's message behind the ⓘ beside "Sync") and Check Input (what it does,
    beside its title; the match line, beside its checkbox).
  - [x] The Swift app's filter menu (`library.filters`): Show and the labels in one menu behind a
    funnel beside the search, with Clear filters. This machine has no Swift toolchain: the Mac
    builds it and runs `swift test` (check 7).
  - [x] The screens list hides, Notes-like (the owner's answer, below).
    - `sidebarHideable`, with the pin, View → Hide Screens List (no key: ⌃⌘S is the
      library's) and the top-left hover. It is kept in `window.json`.
    - On macOS, the content runs under the title bar: the list is full height, and the screens
      start at the safe area.
    - Prompts and Measurement have icons of their own (`waveform`, `chart`).
    - The library is headed "Library" in bold, with its count muted under it.
- [x] 5. The Settings window. Server and Pocket ID issuer are bare labels; the address patterns,
  and the pane's note that the environment's values win, are behind their ⓘs. The explanations
  of Send takes to the PC, Approve, the match line and Quality flags are behind ⓘs beside
  them. The Approve dialog stays whole: it is the review (AP2, AP3).
- [x] 6. Status, Uploads, the sign-in placeholder and the player bar.
  - Every screen's tagline is behind an ⓘ beside its title. `ScreenHeader` does it once for
    all, so steps 7–11 have their taglines (ME1, CO1, JO1, PR1, RE1, LG1) already.
  - `Card` gains `info`, an ⓘ beside its heading. `subheading` stays for a line that must stay
    visible.
  - Status: the setup banner's text, the settings' source, and the explanations of the
    Uploads, gates, job runner, spool and report cards.
  - Uploads:
    - where uploads go, behind the Spool's ⓘ while they can;
    - the whole cause, with the CLI's way, behind the not-ready banner's ⓘ while the approval
      row says why;
    - the Sessions card's explanation;
    - on each row, the receipt's digests, the resume note or a held session's how-to, behind
      an ⓘ beside its state. The cause stays.
  - The placeholder's how-to when the app is not set up.
  - The player's reason when playback is off.
- [ ] 7–11.

`InfoTip` is `SuiInfoTip` with plain text by default (a tip often shows server or catalog
strings) and Space and Return claimed while it has the focus. `SuiInfoTip` takes them as key
presses. A window `Shortcut` gets those first unless the focused item claims them as a
shortcut override, and the studio's Space and Return are window shortcuts. A click never focuses
the tip, so this costs the loop nothing. The funnel (`SuiListHeader`'s filter button) claims
nothing: after a mouse choice the focus returns to it, and Space and Return must stay the
studio's. From the keyboard, ↓ opens it.

What 0.10 has, against the ask above:
- There is no hand-off from one open tip to the next.
- There is no tone.
- There is no `info` property on other controls, so a page puts an `InfoTip` beside the
  control.
- The filter menu scrolls with no scroll bar.

The app needs none of these to adopt the components.

## The owner's answer: like Notes (2026-11-03)

**Only the screens list hides.** That is the list of screens: Status, Uploads, Record and the rest. It comes back as an overlay while
the pointer rests at the window's top-left. The prompt library stays, and keeps its own
fold-away, ⌃⌘S included.

**The app should feel like macOS Notes.**
- The screens list is Notes' folder sidebar, the prompt library its note list, and the
  recording area its editor.
- The sidebar runs the window's full height, with the window buttons and the sidebar toggle at
  its top.
- Its rows are an icon, a name and a muted, right-aligned count, with a rounded highlight on the
  selected row and small, muted section headers.
- The list column is headed by a bold name with a muted count under it.
- The search is compact, and its magnifier carries a small chevron for its scope: our funnel.
- The toolbar is a few grouped, rounded icon buttons.

**How step 4 takes it.**
- **In the app, with Qt and scshafe-qt 0.10:**
  - The screens list hides: `SuiAppShell`'s `sidebarHideable`, its pin, the hover reveal, and
    the choice kept across launches.
  - On macOS, the window's content extends under the title bar (Qt's
    `ExpandedClientAreaHint`). The sidebar then runs the full height, with the window buttons
    inside its top. The screens start below the title bar.
  - Each screen gets an icon of its own.
  - The library is headed "Library" in bold, with its count muted under it and the ⓘ beside it.
- **Already as Notes:** the rounded highlight and the small, muted section headers
  (`SuiSidebarList`).
- **Asked of scshafe-qt, through the owner:**
  - the toggle beside the window buttons, which stays while the sidebar is hidden;
  - a translucent sidebar;
  - a plain, muted count;
  - the funnel as a chevron on the magnifier;
  - a toolbar group;
  - icons for recording and measuring.

  The app adopts each as it is released.

## Inventory

In the tables, *ⓘ heading* means an ⓘ beside that heading, label or title. QML paths are under
`apps/desktop/src/voice_lab_desktop/qml/`.

### Record → Prompts (the studio: `screens/StudioScreen.qml`, `screens/studio/`)

| # | Where | Text | Decision |
|---|---|---|---|
| ST1 | Library · coverage | "1,234 / 1,886" | visible |
| ST2 | Library · `studioCoverageSource` | "The PC's library (read 16:15) and this machine's takes"; "This machine's takes only: the PC's library has not been read yet"; "… · the PC is rebuilding its library" | ⓘ beside the coverage (owner, (c)); the ⓘ is tinted *warn* while the source is partial |
| ST3 | Library · `studioCatalog` | "cole-corpus · v2" | in the same ⓘ (owner, (c)). "No catalog yet" stays visible as the coverage "–" and the switch notice |
| ST4 | Library · `studioSwitchNotice` | "Version 3 is active on the PC: it switches at the next session" (Switch now); "Recording starts once the PC has answered" | visible while it has something to say; it no longer reserves its two lines |
| ST5 | Library · Continue Missing | "Selects the next missing prompt in catalog order; ignores search and filters." | already hidden (accessible description) |
| ST6 | Library · Show (select) and Labels (button) | All prompts, Missing, Needs attention, Flagged here, Failed PC QC, Drafts only; the labels | the funnel (owner, (b)): both in its popover, Clear resets both, the search keeps its own clear |
| ST7 | Library · `studioCurrentOutside` | "Current prompt · outside filter" | visible |
| ST8 | Library · `studioLibraryEmpty` | "No prompt matches the search and filters." | visible |
| ST9 | Library · rows | kind · status, the flag chip | visible |
| ST10 | Script card | PROMPT · READ ALOUD, INSTRUCTIONS · NOT READ ALOUD, the prompt, placeholder, banner, guidance, duration | visible (read during the take) |
| ST11 | Header | prompt title, reading size, microphone | visible |
| ST12 | Speak cue | "Speak" / "Wait…" | visible (the take) |
| ST13 | Flags strip | the flags' chips; their details popup on a click | visible; the details stay behind the chips |
| ST14 | `studioProblem` | the error | visible |
| ST15 | `studioWaveNote` | "Kept · this machine · dB display scale", "Live · dB display scale" | the take's words visible; "dB display scale" moves to an ⓘ: the waveform's scale, −12 dBFS at 80% of the half-height |
| ST16 | Level meter, `studioLevelText` | "−23.4 dBFS" | visible (the take) |
| ST17 | Record's tooltip | why Record is off | already a hover tooltip |
| ST18 | Status line, sync badge, Details | the status; the badge; the details popup | visible; the details stay behind the badge |
| ST19 | Trim and Restore dialogs | "This take's In and Out were changed and not saved."; "What this take was before it was set aside is not known on this machine." | visible (the question's substance) |
| ST20 | Check Input dialog | "About ten seconds: two of quiet, then the line below read aloud … Not a take: the audio is measured, then deleted." | ⓘ beside the title; the stage's instruction ("Quiet, please", "Read this aloud"), the line, level, time left, results and summary stay visible |
| ST21 | Check Input · match line | "Keeps your first reading of this line on this machine (never sent) and compares each later check with it: level, SNR, length." | ⓘ beside the checkbox |
| ST22 | Check Input · reference | the reference kept, Play, Forget | visible |
| ST23 | Inspector · Microphone | the device | visible |
| ST24 | Inspector · Flags | each flag's why, then the levels (speech, room, SNR, peak, training gain, Check Input's room) | the flags visible (as in the centre); their why and the levels move to an ⓘ beside "Flags" |
| ST25 | Inspector · Delivery notes | the notes, guidance, duration | visible (read during the take) |
| ST26 | Inspector · Pace | the strip, the break hint (OK), "N prompts flagged since you started" (Review now), the pass | visible |
| ST27 | Inspector · Sync | the line; the client's message | the line visible; the message moves to an ⓘ beside "Sync" (*warn* when it is an error) |
| ST28 | Take picker | the history note in its list | visible |

### Settings window (`screens/SettingsWindow.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| SE1 | General | "Where the app finds the voice-lab server and Pocket ID. Values set in the environment win and cannot be changed here." | ⓘ beside the pane's first label |
| SE2 | General · Server | "Server (https://voice-lab.<tailnet>.ts.net)" | "Server" visible; the pattern in an ⓘ |
| SE3 | General · Pocket ID issuer | "Pocket ID issuer (https://id.<tailnet>.ts.net)" | "Pocket ID issuer" visible; the pattern in an ⓘ |
| SE4 | General · distance | "Measurement: mouth to capsule distance, cm" | visible |
| SE5 | Uploads · Send takes to the PC | "Takes, decisions and trims go to the PC's corpus (corpus_sync). Off, they stay on this machine until it is on." | ⓘ beside the switch |
| SE6 | Uploads · Approve | "The PC accepts takes only under your approval for it (D-021): Approve uploads to my PC writes it here, from Tailscale and these settings." | ⓘ beside the button (the dialog is the review) |
| SE7 | Uploads · sync line | the studio's sync line | visible |
| SE8 | Recording · microphone | "Microphone: …", "No microphone: …", "Record → Prompts uses the session flow on this machine." | visible |
| SE9 | Recording · match line | "Keeps your first reading of its line on this machine, never sent, …" | ⓘ beside the checkbox |
| SE10 | Recording · Quality flags | "Quality flags: starting levels, to calibrate on real takes" | "Quality flags" visible; the rest in an ⓘ |
| SE11 | Recording · thresholds | "Low SNR under, dB", "Inconsistent over, dB" | visible |

### The Approve dialog and the sidebar (`Main.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| AP1 | Sidebar · account | the account; "Not signed in"; "Not set up" | visible |
| AP2 | Approve · description | "The app sends your recordings to this PC only, until the date below. Each upload is written in this machine's ledger …"; or why it cannot approve | visible (it is the review) |
| AP3 | Approve · fields, reason | PC, node, what, until, by; "It renews the approval …" | visible |

### Status (`screens/StatusScreen.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| SA1 | Tagline | "Sign-in, the voice-lab server, and the sessions waiting on this machine." | ⓘ beside the title |
| SA2 | Set up banner | "Set up the server" (Settings…); "Set the voice-lab server and the Pocket ID issuer before signing in." | title and button visible; the text in an ⓘ |
| SA3 | Settings source | "Settings from <file> + environment (VOICE_LAB_…)" | ⓘ beside Settings… |
| SA4 | Account, client update, error, "Asking the server…" | the states | visible |
| SA5 | Uploads to the PC · subheading | "Recordings go only to the PC you approve, for a year at most (D-021). …" | ⓘ heading |
| SA6 | Uploads to the PC · summary | "Uploads to X are approved until …" | visible |
| SA7 | The server's gates · subheading | "What the operator enabled on the server. Playback needs a recorded approval there; …" | ⓘ heading |
| SA8 | Job runner · subheading | "The PC's journey pipeline (GET /api/v1/watch)." | ⓘ heading |
| SA9 | Job runner · line | "Last run: … N new.", "Not running (…)." | visible |
| SA10 | Spool · subheading | "Measurement sessions stay on this machine until the server accepts them." | ⓘ heading |
| SA11 | Spool · line | "Sessions: 2 waiting for a receipt; …" | visible |
| SA12 | Report · subheading | "The web view renders the evidence report; it opens in your browser, behind the same sign-in." | ⓘ heading |

### Uploads (`screens/UploadsScreen.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| UP1 | Tagline | "Sessions waiting for the server's receipt, and what it accepted." | ⓘ beside the title |
| UP2 | Not ready | "This machine cannot upload yet" + the cause; the CLI fallback "… or approve the home host once with voice-lab home-host approve …" | title and cause visible; the CLI fallback in an ⓘ; *drop* the banner's text while the approval row below says the same; *stale* "on the Status screen" |
| UP3 | Ready | "Uploads go to <endpoint> under the home-host approval <id> (until …), covering …; each is recorded in this machine's ledger." | ⓘ beside "Spool" |
| UP4 | Approval, summary, progress | the states | visible |
| UP5 | Empty · description | "Measurement and prompted sessions recorded on this machine wait here until the server accepts them." | ⓘ beside "No local sessions" |
| UP6 | Session rows · detail | "Refused at T: …", "Accepted at T by S. Package P, manifest abc…", "… It resumes where it stopped.", the held session's how-to | the cause visible; the receipt's digests, the resume note and the how-to in a row ⓘ beside its state chip |

### Sign-in placeholder and player bar (`screens/NeedsServer.qml`, `screens/PlayerBar.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| NS1 | Title, Go to Status | "Sign in to see …", "Set up the server first" | visible |
| NS2 | Description | "The app reads the voice-lab server with your Pocket ID sign-in." / "Set the voice-lab server and the Pocket ID issuer on the Status screen." | signed out: *drop* (the title says it); not set up: ⓘ, *stale* (Settings) |
| PB1 | Error | the player's error | visible |
| PB2 | Unavailable | "Playback requires the release-gated launch." | ⓘ beside the disabled Play |

### Measurement (`screens/MeasurementScreen.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| ME1 | Tagline | "The UMIK-1 ritual: a calibrated session, recorded as 48 kHz / 24-bit WAV and kept here until the server accepts it." | ⓘ beside the title |
| ME2 | Microphone | "No UMIK-1 found: plug in … At launch it is the only supported device (chain umik1); built-in and other microphones are refused. Seen: …"; the found device | the error and what to do visible; the device policy and the found device's chain in an ⓘ beside "Microphone" |
| ME3 | Input level · pinned | "pinned at 70% (pipewire) since …" | visible |
| ME4 | Input level · message | "Matches the pin"; "… not pinned yet. Set … then press Pin level. Re-pinning later is a logged chain era event."; "… moved … A moved slider invalidates the SPL reference: …" | the state and what to do visible; why a re-pin or a move matters in an ⓘ; *drop* "Matches the pin" (the chip says it) |
| ME5 | Calibration file | "No cal file imported: sessions are recorded uncalibrated (calFileSha256 null)."; "7012345.txt, serial … (sha256 …)" | visible; "(calFileSha256 null)" and the digest in an ⓘ |
| ME6 | Session · distance | "Mouth to capsule: 35 cm (Status → Settings). Same corner, same distance, every session." | the distance visible; the advice in an ⓘ; *stale* "Status → Settings" |
| ME7 | Session · blocker | "Pin the input level first." … | visible |
| ME8 | Ritual · empty | "No session yet" + "Plug in the UMIK-1, pin its input level once, … The ritual: room silence, (calibrator), /a/ soft and loud, a glide, the anchor song, free practice." | title visible; the ritual in an ⓘ; the first sentence *drop* (the cards to the left say it) |
| ME9 | Ritual · abort, segments, prompt, meter | the states and the live instructions | visible (the recording) |
| ME10 | Abort and re-pin dialogs | what is kept; why a re-pin matters | visible (the decision's substance) |

### Corpus (`screens/CorpusScreen.qml`, `screens/corpus/`)

| # | Where | Text | Decision |
|---|---|---|---|
| CO1 | Tagline | "Spot-check workbench over committed manifests. Playback is release-gated and per-click only." | ⓘ beside the title |
| CO2 | Signed out | as NS1, NS2 | as NS1, NS2 |
| CO3 | Error, status line, search placeholder | the states | visible |
| CO4 | Table tooltips | "Stream this take (per-click, release-gated)" … | already on hover |
| CO5 | Detail · bucket and year, errors, rationale, transcript state | the data and states | visible |
| CO6 | Detail · Listen | "Playback requires the release-gated launch." | as PB2 |

### Journey (`screens/JourneyScreen.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| JO1 | Tagline | "Seven years of practice, measured: range, sustain, timbre, vibrato, and the same-song verdict." | ⓘ beside the title |
| JO2 | Signed out, loading, error | as NS; "Loading trends…" | visible |
| JO3 | No trends | "No voice trends yet" + "Voice trends need the features manifest: …" | title visible; the text in an ⓘ |
| JO4 | Flagged toggle | "include the flagged low-reliability takes (hollow in scatters)" | the label visible; "(hollow in scatters)" in an ⓘ |
| JO5 | Verdict · subheading | "Slopes per year measured inside recurring songs (Theil–Sen per song, bootstrap 95% CI …). The strictest lens …" | ⓘ heading |
| JO6 | Verdict · summary | "2 improving · 3 flat · 0 declining across 12 songs. …"; the steady variant's reading | the counts visible; the reading in an ⓘ |
| JO7 | Range River · headline | "Working top: G3 (2019) → D4 (2024) · +7 semitones of reach, held since." | visible; the fallback "Where your voice lives, quarter by quarter." in an ⓘ |
| JO8 | Range River · legend | "reach (p05–p95 envelope)", "typical (p25–p75)", "median", "register strip: high ≥300 Hz · mid · low <200 Hz" | the swatches and their short names visible; the percentiles and the Hz bounds in an ⓘ |
| JO9 | Cause & Effect · subheading | "Practice cadence above; what it buys below. Shaded bands are your high-practice eras, …" | ⓘ heading |
| JO10 | Cause & Effect · eras | "Playback needs the release-gated launch (corpus root + approval)" (accessible only) | an ⓘ while the era buttons are off, which now say why |
| JO11 | Vibrato · subheading | "Median pitch-modulation per quarter with interquartile bands. The blue zone marks settled classical vibrato (5–6 Hz) …" | ⓘ heading (the chart's "settled 5–6 Hz" stays) |
| JO12 | Fingerprint · caption | "One dot per take: extent across, rate up, brighter = more recent. Click a dot to inspect that take." | ⓘ heading |
| JO13 | Fingerprint · years | the year swatches | visible |
| JO14 | What Not To Trust · subheading | "Two measures of "voice quality" that disagree — on purpose. …" | ⓘ heading (the panels' titles keep the gist) |
| JO15 | Tuning tile | "Tuning — now measured on note cores. 1,234 takes scored … median error 22.1 cents, steady …"; "Tuning accuracy — not yet meaningful. …" | the title and the reading visible; the method and the pipeline in an ⓘ |
| JO16 | Song Scorecards · subheading | "The songs with enough history to judge (≥6 reliable takes across ≥1.5 years), …" | ⓘ heading |
| JO17 | Repertoire Threads · subheading | "Your eight biggest recurring songs across the years — dot area is takes that year. …" | ⓘ heading |
| JO18 | Education card | per tab, a title, an intro and long sections | collapsed: a disclosure, closed by default (too long for a popover) |
| JO19 | Take inspector | "Loading…", the error | visible |

### Practice (`screens/PracticeScreen.qml`, `screens/practice/`)

| # | Where | Text | Decision |
|---|---|---|---|
| PR1 | Tagline | "Tonight's session, argued from the data — due songs and frontier flags from the coach." | ⓘ beside the title |
| PR2 | Signed out, loading, error | as NS; "Consulting the coach…" | visible |
| PR3 | Due · subheading | "Spaced re-recording for the same-song engine: …" | ⓘ heading |
| PR4 | Due · why chips | "last sung 123 d ago", "18 takes, 2019–2024", "in the same-song index …", "1 clean take short of same-song eligibility" | the index and eligibility chips visible; *drop* the first two (the row's columns say them) |
| PR5 | Due · empty | "Nothing due — every recurring song has a take inside the 90-day window. Go sing something new." | "Nothing due — go sing something new." visible; the window in an ⓘ |
| PR6 | Frontier · subheading | "One step away, measured: songs sitting just under a threshold, …" | ⓘ heading |
| PR7 | Frontier · chip and detail | "range stretch"; "top notes sit 1.2 st under your working top — a stretch, not a leap" | visible (the detail is the reading itself) |
| PR8 | Frontier · empty | "No frontier flags right now — thresholds move as you do; check back after the next ingest." | the first clause visible; the rest in an ⓘ |
| PR9 | Goals · subheading | "Declare the metric, target, and sample size before you sing; …" | ⓘ heading |
| PR10 | Goals · error, scope, progress, placeholders, song link | the states and data | visible |
| PR11 | Goals · empty | "No goals yet — declare one below and it starts counting from your next take." | "No goals yet — declare one below." visible; *drop* the counting rule (the ⓘ says it) |

### Referee (`screens/RefereeScreen.qml`)

| # | Where | Text | Decision |
|---|---|---|---|
| RE1 | Tagline | "Blind same-song A/B trials. Years stay hidden until you judge." | ⓘ beside the title |
| RE2 | Signed out, error, loading | as NS; "Loading the next pair…" | visible |
| RE3 | Gate | "Playback is off on the server" + "The referee needs release-gated playback. … 1,234 blind pairs are waiting. Existing results still show below." | the title and the pairs waiting visible; the approval's explanation in an ⓘ |
| RE4 | This pair · subheading | "Listen to both, then call it. … loudness-matched and band-limited (8 kHz) …" | ⓘ heading |
| RE5 | Pool, reveal, progress, choices | the states and counts | visible |
| RE6 | Results · subheading | "Bradley–Terry strengths from your judgments, log2 relative to the earliest judged year. …" | ⓘ heading |
| RE7 | Results · flags | "Chained-cluster flags: lyric-cluster-003 ×1 — these feed the cluster repair pass." | the flags visible; the tail in an ⓘ |

### The legacy sessions screen (`screens/PromptsScreen.qml`, `prompts_mode = "sessions"`)

| # | Where | Text | Decision |
|---|---|---|---|
| LG1 | Tagline | "Read each prompt into the UMIK-1, keep the take you like, and finish the session as a package. …" | ⓘ beside the title |
| LG2 | New session · subheading | "Choose a catalog and the prompts to record. Every room tone of the catalog is recorded too." | ⓘ heading; *drop* the second sentence ("room tone included" says it) |
| LG3 | Device, refused catalog, count, finish blocker | the states | visible |
| LG4 | Package banner | "Packaged as … It waits in Uploads; takes, trims and history stay on this machine." | visible; *drop* the tail (the tagline says it) |
| LG5 | Script · style and guide, silent card | the direction for the take | visible (the take) |
| LG6 | Transport, takes, errors, trim | the states | visible |
| LG7 | Review · subheading | "The trim is kept here as a recipe; the package carries the take as recorded." / "Record a take of this prompt (R)." | the first in an ⓘ; *drop* the second (the empty state and Record (R) say it) |
| LG8 | Set-aside dialog | "Its takes stay on this machine; it is not packaged or uploaded. …" | visible (the decision's substance) |

## Tests that change

- **Moved taglines.** `test_shell.py::test_every_screen_has_the_spa_title_and_tagline` checks
  them: it will read the ⓘ's text. Checklist row A-03 (`PHASE-4-CHECKLIST.md`) and the rows that
  promise the web view's text get a note: the text is kept, behind an ⓘ.
- **Split model strings.** These tests assert texts whose explanation moves:
  - `test_journey_range_river_layers`, `test_journey_education_cards`;
  - `test_corpus_detail_listen`, `test_the_player_bar_drives_the_one_player`;
  - `test_practice_due_and_frontier`, `test_practice_empty_due_and_frontier`;
  - `test_referee_gate_notice`, `test_referee_results`;
  - `test_without_a_umik_nothing_records`;
  - `test_uploads_say_when_this_machine_cannot_upload` and the upload rows' `detail` tests.
- **The core loop.** `test_the_core_loop_rapid_fire` stays unchanged.
