# Plan: Finder installation and app icon

**Goal:** Install Cole Voice Lab in `/Applications` with Cole's supplied artwork
in Finder and the running Dock, using the existing private capture configuration.
**Out of scope:** Audio capture, recording changes, portable packaging, model
work, Dock-wide resets, unrelated application changes, or killing an operator app.

## Context

- The existing exclusive launcher is in the personal Applications directory;
  `/Applications/Cole Voice Lab.app` is absent and `/Applications` is writable.
- Extend `desktop_application.py` and `desktop.py`, not a second installer.
  Bundle icon metadata serves Finder; Qt application icon serves the running Dock.
- Package one public icon resource so installed wheels and local launchers agree.
  The uploaded square PNG has an opaque checkerboard outside the purple tile.
  A transparent-background edit was attempted but did not produce alpha;
  retain the original art while awaiting permission for precise local masking.
- Existing app targets must still fail closed. Keep the previous launcher
  recoverable; avoid two conflicting visible installations after successful QA.
- What if the app is active? Do not terminate it. What if an icon is missing?
  Fail clearly before publishing a launcher. What if the checkout moves? This
  remains a documented local launcher bound to its interpreter, not a portable app.

## Steps

- [x] **Step 1: Prepare the icon and extend app packaging.**
  - Verify: inspect transparency/artwork; focused tests cover bundle resources,
    icon metadata, Qt identity, unsafe targets, and missing resources.
- [x] **Step 2: Install and inspect the main Applications entry.**
  - Verify: validate plist, launcher quoting, icon resources and digests; check
    Finder/Launch Services recognition and a non-recording launch when safe.
- [x] **Step 3: Verify and document the installed application.**
  - Verify: `scripts/verify`, `git diff --check`, and packaged icon presence.
  - Notes: document exact open path and any remaining operator-only Dock check.

## Done criteria

The main Applications entry opens the existing native app with the supplied icon,
the build retains that icon, checks pass, and no recordings were changed. Keep
the prior launcher recoverable; no commits or unrelated user-app restarts.

- [x] **Follow-up: Remove the visible opaque icon border.**
  - Verify: true alpha outside the purple tile, unchanged interior pixels,
    regenerated ICNS representations, and installed Finder icon inspection.
  - Notes: Cole's subsequent request confirms the previously offered local
    border cleanup. No further generated redraw; keep originals recoverable.

## Evidence and continuation

- Installed `/Applications/Cole Voice Lab.app` using the existing exclusive
  builder. Plist validation, exact ICNS bytes, and identical quoted capture
  command to the old launcher all passed. Launch Services resolves the bundle
  ID to `/Applications/Cole Voice Lab.app`. Finder's exported icon was inspected.
- No existing recorder was running before launch. Opened and revealed the new
  app without starting a take; left it running for Cole. Do not terminate it.
- The previous launcher is recoverable at
  `/Users/coleshaffer/.local/share/cole-voice-lab/desktop-launcher-backup.ENxiyn/Cole Voice Lab.app`.
- Qt explicitly sets the live Dock tile image. `NSRunningApplication.icon`
  returns the Python bundle icon, not reliable evidence of the customized live
  tile. The interpreter-based launcher's process identity remains Python; a
  permanent Dock shortcut should target the app in Applications, not that process.
- Built-in image edit prompt: remove only the checkerboard outside the purple
  tile, preserving all interior artwork and requesting true transparent alpha.
  The generated PNG remained opaque and was rejected. An optional question asks
  whether to perform exact local masking instead; no such edit is authorized yet.
- Installed artwork is currently the exact supplied PNG, SHA-256
  `2d3f8846407427bf1d57c779e097db540a32216ad29893abd86f256796021b82`.
  It lives at `src/cole_voice_lab/resources/desktop-icon.png`, with Apple
  `sips`/`iconutil` size representations in `desktop-icon.icns`. The build bundles
  these public art resources, never human audio or private capture evidence.
- All 16 focused desktop-entry/packaging tests passed. Final `scripts/verify`
  passed 692 tests in 277.88 seconds with 90.90% branch-aware coverage, Ruff,
  strict mypy, package build, schemas, and the private-asset guard. The built
  wheel contains both icon resources; its PNG digest matches the supplied file.
  `git diff --check` passed. QA artifacts: `/tmp/cole-voice-install.6VTuiX/`.

**Amended 2026-09-07:** The image tool returned no alpha; retain the exact original
rather than accepting changed opaque artwork. Local masking is an optional
follow-up requiring Cole's answer, not a blocker for the requested installation.

**Amended 2026-09-07:** Cole reports the visible gray Dock border and asks to fix
it, confirming the offered local masking cleanup. Update only the outer alpha
mask and installed icon resource; do not terminate the recording application.

**Follow-up verified:** The local mask preserves 1,355,149 interior RGB pixels
exactly and makes 217,367 exterior pixels transparent. The rebuilt ICNS is
installed in `/Applications/Cole Voice Lab.app`; Launch Services was refreshed
without restarting the app or Dock. Finder's exported icon has alpha and was
visually inspected. Previous public resources and plist are retained in
`/tmp/cole-voice-editor.KmDy12/original-icons/`; the original upload is unchanged.
The running Dock tile updates on operator-controlled reopen. This supersedes the
earlier exact-opaque-PNG state, not the existing launcher/capture configuration.
