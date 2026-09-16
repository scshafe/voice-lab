# Plan: Distinguish silent recording instructions from spoken scripts

**Goal:** Room-tone tasks unmistakably request silence; only spoken prompts appear
as read-aloud scripts, with delivery guidance visibly separate.
**Out of scope:** Prompt/schema migrations, audio edits, gain changes, automated
capture or timers, old-take deletion, dependencies, or restarting the operator app.

## Context

- The current room-tone prompt is rendered under Script in the same 24pt editor
  as speech. Cole's screenshot confirms this is ambiguous.
- Extend the canonical DesktopWindow presentation, keyed by category `room-tone`.
  Preserve exact prompt text, IDs, catalog digests, and saved history.
- Use a separate instruction card, explicit Do not speak label, muted-speaker
  icon, duration, and task-aware transport labels. Spoken text stays unchanged.

## Steps

- [x] **Step 1: Separate recording-task presentation.**
  - Verify: room tone has no visible spoken-script editor; speech restores exact
    text; library previews and transport identify silent recording unambiguously.
- [x] **Step 2: Verify UI transitions and presentation.**
  - Verify: synthetic Qt tests cover both room tones, spoken/custom prompt switch,
    selection/resume/record/review/Keep, accessibility, and compact light/dark views.
  - Notes: root owns desktop_window.py; delegated tests own test_desktop_window.py.
  - Evidence: 96 native GUI tests pass. Offscreen synthetic screenshots checked
    at 1320x860 and 1060x700 in light/dark palettes. Explicit card-label palettes
    and fonts preserve contrast/13pt instructions across appearance changes;
    screen readers receive full instruction text. No microphone or user audio used.
- [x] **Step 3: Document and run the full gate.**
  - Verify: `scripts/verify`, `git diff --check`, screenshot inspection, docs updated.
  - Notes: operator safely reopens; never interrupt real audio or alter old takes.
  - Evidence: final `scripts/verify` passed 872 tests in 254.73 seconds, 90.86%
    branch-aware coverage; lint/format, strict types, build, schema check, and
    private-asset guard passed. `git diff --check` is clean. Desktop guide,
    CURRENT, TESTING, and D-013 document the change. Installed launcher imports
    this checkout; no app restart/reinstall or saved-recording change performed.

## Done criteria

Clear silent-vs-spoken task presentation, verified behavior and unchanged corpus
identity; user receives safe reopen instructions with no recording changes.
