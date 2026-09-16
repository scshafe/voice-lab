# Plan: Clarify spoken-script delivery guidance

**Goal:** Remove the apparent conflict between Read aloud and the footer while keeping script, delivery style, and approximate timing distinct.
**Out of scope:** Prompt/catalog edits, audio processing, capture behavior, layout redesign, or restarting the operator's app.

## Context

`desktop_window.py` renders spoken tasks under Read aloud but labels their footer Delivery (do not read aloud). The user found that confusing in reference-narration-01. Change presentation only; preserve exact script/style values and the separate Do not speak room-tone card.

## Steps

- [x] **Step 1: Clarify the delivery footer and accessibility**
  - Use Delivery guidance and clearly approximate duration; explain in the tooltip/accessibility description that guidance is not script content.
  - Verify: targeted `tests/test_desktop_window.py` script/room-tone, accessibility, and compact-layout cases.
  - Result: 13 focused tests passed, including narration in five transport states, custom timing, and accessibility resets.
- [x] **Step 2: Update documentation and inspect rendering**
  - Update desktop usage and the previous UX decision wording; inspect synthetic narration in light/dark compact and larger windows.
  - Verify: offscreen synthetic screenshots; no private audio or microphone access.
  - Result: synthetic paused narration rendered in light/dark palettes at 1060×700 and 1440×980; footer fits its card without overlap. Desktop usage and UX decision documentation updated.
- [x] **Step 3: Run full project verification**
  - Verify: `scripts/verify`; report wording and safe reopen instructions.
  - Result: full gate passed: 1149 tests in 371.23 seconds, 91.44% branch-aware coverage; formatting, lint, strict types, package build, schemas, and private-asset guard passed. Installed launcher uses this checkout; no restart or reinstall performed.

## Done criteria

Updated wording is verified, canonical recording tasks are unchanged, full gate passes, and the user receives a concise clarification. No commit or automatic app restart.
