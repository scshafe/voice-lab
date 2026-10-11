# Phase 4 progress log

Phase 4 of [VOICE-PLATFORM.md](VOICE-PLATFORM.md) (§6, §9): the desktop app and the web
view. Written as the work goes, so that a successor can resume it. Newest entries last.

Rules that bind this work: recordings, consent evidence and weights never enter Git
(`scripts/guard-private-assets` before every push); synthetic audio and data only; no
audio device (the measurement capture's `simulated.py`, a stand-in output for playback);
never read `/data/voice-lab`, `/data/backups/voice-lab`, `/srv/voice-journey` or the Air's
asset root; the agent cannot reach the tailnet or the LAN, so nothing here touched the live
server, the Air or a real device; draft #31 and the phase-3 runbooks' owner steps are not
touched; phases 5 and 6 are out of scope.

## Plan

| Step | What | PR | State |
| --- | --- | --- | --- |
| 4a | `voice-journey-capture` becomes `voice-lab-desktop` (Python 3.12, scshafe-qt 0.2); sign-in and status, Uploads, Corpus, Journey, Practice, Referee on `/api/v1`; the checklist | — | in progress |
| view | Tests: every `/view` route refuses non-GET; none serves audio | — | ready on `phase4/view-tests`, after 4a |
| 4b | The UMIK-1 ritual in the app; `/api/v1/intake` with the D-021 ledger | — | not started |
| 4c | Prompted capture; the S-1 harness; Linux packaging; the macOS bundle script and install page | — | not started |
| removal | Remove the SPA's routes and voice-journey's unversioned paths | draft | gated: checklist sign-off, cutover (F) |

## Entries

### 2026-10-11

- Read AGENTS.md, VOICE-PLATFORM.md (§3.4, §6, §9), D-030 to D-036, PHASE-3-LOG.md and its
  open items, the imported capture client and voice-journey's SPA as imported
  (`7f62275:web/src`; D-035 kept it out of the tree).
- The clone's `main` was 33 commits behind `origin/main`; fast-forwarded before starting.
- **scshafe-qt.** 0.2.0 (PyPI, `requires-python >=3.12`, PySide6-Essentials `>=6.11.2,<6.12`)
  has the 0.1 component set and none of §6.4's gaps; no release since. PySide6 6.12.0 is on
  PyPI, but scshafe-qt 0.2 caps at 6.11, so the app stays on 6.11.2.
- **4a checklist** first ([PHASE-4-CHECKLIST.md](PHASE-4-CHECKLIST.md)): 91 items over the
  SPA's views (A, C, J, P, R) and the two screens it never had (S, U), each naming the tests
  that tick it; `apps/desktop/tests/test_checklist.py` keeps the document and the tests in
  step.
- **The distribution** (D-037): `apps/desktop` is `voice-lab-desktop`; the capture client's
  modules moved with their history into `voice_lab_desktop` and
  `voice_lab_desktop.measurement`. Ruff and strict mypy cover it; `scripts/verify` runs its
  suite on its own (pytest-qt; the backend's suite runs with `-p no:pytest-qt`, whose hooks
  break the backend's tests that stand in a fake application) and builds its wheel into
  `dist/`. The app depends on the backend distribution rather than a `contracts/` one
  (D-037). Settings name no server or issuer (the publication screen's §5 item).
- **Playback** (D-038): PyAV decodes in memory, NumPy shapes, PortAudio plays; one player.
- **The capture client's tests** in `apps/desktop/tests/measurement/` (139 passed and 2
  skipped before the move, on Python 3.12 with scshafe-qt 0.2.0 unchanged).

## scshafe-qt asks

What voice-lab-desktop needs from scshafe-qt (design §6.4), as the API its stand-ins in
`apps/desktop/src/voice_lab_desktop/qml/VoiceLab/StandIn/` already have. When a release has
one, the app adopts it and deletes the stand-in. Every component: tokens only, keyboard
focus with the focus ring, an accessible role and name, plain text for caller strings,
instant under reduced motion.

| Component | Stand-in | Used by | State |
| --- | --- | --- | --- |
| `SuiText` (typography) | yes | every screen | waiting |
| `SuiCheckbox` | yes | Journey (include flagged) | waiting |
| `SuiSelect` | yes | Corpus filters, the goal form | waiting |
| `SuiSlider` | yes | the player's position | waiting |
| `SuiProgress` (determinate, cancel) | yes | Uploads | waiting |
| `SuiTable` (sortable, virtualised) | yes | Corpus (about 7,000 rows), data tables | waiting |
| `SuiChart` (line, band, scatter, bars) | yes | Journey, Referee | waiting |
| `SuiLevelMeter` | 4b | Record: measurement | — |
| `SuiWaveform` with trim | 4c | Record: prompts | — |
| `SuiSwitch`, `SuiRadio` | no | not needed by phase 4 | — |

### SuiText

A `Text` over the type scale; always `Text.PlainText`.

- `variant`: `"display"` (fontSize3xl, bold) | `"title"` (fontSize2xl, bold) | `"heading"`
  (fontSizeLg, strong) | `"subheading"` (fontSizeMd, strong) | `"body"` (default,
  fontSizeBase) | `"caption"` (fontSizeSm, muted) | `"label"` (fontSizeSm, strong) |
  `"mono"` (monoFamily, fontSizeSm) | `"number"` (tabular figures).
- `tone`: `"default"` | `"strong"` | `"muted"` | `"info"` | `"ok"` | `"warn"` | `"danger"`.
- Everything else is `Text`'s (`wrapMode`, `elide`, `maximumLineCount`, …). Accessible as
  static text.

### SuiCheckbox

`T.CheckBox`: `checked`, `text`; `toggled()` on a user change (Space, click). 16 px box;
`implicitHeight` controlHeight-ish; the label wraps.

### SuiSelect

A single choice from a short list (a filter, a form field), with a visible caption.

- `options`: `[{ value: string, label: string }]` (a few to a few dozen; the Corpus's year
  filter has about eight, the goal form's scope about a dozen).
- `value`: the chosen value ("" is a normal value, e.g. "All"); `label`: the caption above
  it and the accessible name; read-only `currentIndex`, `currentLabel`, `popupOpen`.
- `valueSelected(value)` on a user choice (mouse, Enter, Space, arrows); `value` follows.
- Implicit width at least 140 px; height controlHeight (+ the caption); the popup at most
  320 px tall, scrolling.

### SuiSlider

`T.Slider`, horizontal: `value`, `from`, `to`, `stepSize`, `label` (accessible name);
`moved()` on a user change (drag, click, arrows, Home/End); `pressed` while dragging.
Implicit 160 × 20 px. Used as a seek bar: `to` up to an hour (3,600 s), updated ten times
a second while playing.

### SuiProgress

A determinate bar with an optional label, detail and cancel.

- `value` in [0, 1] (clamped); `indeterminate` (activity without a value; no animation
  under reduced motion); `tone` (a SuiTheme tone, default `"info"`).
- `label` (left, strong), `detail` (right, muted, tabular: "12.4 MB of 48 MB").
- `cancelable` shows Cancel; `cancelRequested()`.
- Accessible as a progress bar whose description is the percentage. A 6 px track.

### SuiTable

A sortable, virtualised table: only the visible rows have delegates.

- `model`: a list model with one role, `row`: a map from column key to the cell's value
  (already formatted for display), plus `_key` (a stable row id) and `_highlight` (tints
  the row). The Corpus has about 7,000 rows, appended 100 at a time.
- `columns`: `[{ key, title, width, align: "left" | "right", sortable, sortKey, kind }]`
  (eleven columns, 64 to 220 px). `kind` draws the value: `"text"` (default), `"number"`
  (right-aligned, tabular), `"chip"` (`{ text, tone }`), `"stack"` (`{ primary, secondary,
  muted }`, two lines), `"link"` (activates: `cellActivated`), `"actions"` (`[{ id, text,
  enabled, tooltip }]`, buttons: `actionTriggered`).
- `pinnedColumns` (default 1): the first columns stay in view while the rest scroll
  sideways (a horizontal scroll bar) when the columns are wider than the table.
- `sortKey`, `sortDescending`: the sort shown (▲ / ▼); a click on a sortable header emits
  `sortRequested(sortKey)` — the owner sorts (the server does).
- Selection: `currentIndex`; Up/Down/Home/End/PageUp/PageDown move it; Enter and a double
  click emit `activated(index)`; Left/Right scroll sideways.
- `endReached()`: the view came within `endThreshold` (10) rows of the end; once per row
  count (for paging).
- `rowHeight` (40, two-line cells), `headerHeight` (30), `label` (accessible name).

### SuiChart

A layered chart of one or more panels stacked on a shared x axis; data coordinates in, the
chart owns the pixels; themed from the tokens (no Qt Graphs). See the stand-in's header for
the full `spec`:

- `spec`: `{ label, xMin, xMax, xTicks: [{ value, label }], padLeft, padRight, panels: [{
  height, title, yMin, yMax, yScale: "linear" | "log2", yTicks, yTicksRight, grid, layers }] }`.
- Layers: `band` (a filled range: `[[x, lo, hi]…]`), `line` (`[[x, y]…]`, a null y breaks
  it), `points` (`[{ x, y, r, tone, shade, hollow, opacity, id, tooltip }]`), `rects` (bars,
  bands across a panel: `[{ x0, x1, y0, y1, tone, id, tooltip }]`), `rule` and `vrules`
  (reference lines), `zone` (a y range with a label), `regions` (hover-only x ranges with a
  tooltip), `text` (labels in data coordinates).
- `tone` is a SuiTheme tone (`bucket1`…`bucket8`, `info`, `ok`, `warn`, `danger`, `muted`,
  `line`); `shade` in [0, 1] darkens a point's tone (older = darker).
- Hover shows the tooltip of the point, rect or region under the pointer; a click on one
  with an `id` emits `activated(id)`; `tooltipAt(x, y)`, `idAt(x, y)` and
  `pointPosition(panel, x, y)` answer for a position (tests, keyboard).
- Volumes: the Range River has about 30 quarters × 3 bands and a register strip; the
  fingerprint about 2,400 points; Cause & Effect about 90 monthly bars and two panels; the
  threads chart 8 rows. Width follows the pane; heights 100 to 400 px per panel.

## Stand-ins waiting on scshafe-qt

Every file in `apps/desktop/src/voice_lab_desktop/qml/VoiceLab/StandIn/` (see the table
above): `SuiText`, `SuiCheckbox`, `SuiSelect`, `SuiSlider`, `SuiProgress`, `SuiTable`,
`SuiChart`.

## Owner's steps

In order. Each is the owner's because it needs the tailnet, a real device, a person's ears
or the owner's judgement.

1. **Phase 3's real UMIK-1 session (runbook E) still uses voice-journey-capture.** Once 4a is
   merged, `apps/desktop` is `voice-lab-desktop` (Python 3.12); the old client is not in
   `main` any more. Run it from the last tree that has it, which is unchanged against the
   server (the compatibility path stays until the removal PR):

   ```sh
   git -C ~/src/voice-lab fetch origin
   git -C ~/src/voice-lab worktree add ~/src/voice-lab-capture 468c744
   cd ~/src/voice-lab-capture/apps/desktop && uv sync --frozen && uv run voice-journey-capture --check-online
   ```

   (Python 3.14 and uv 0.12.21, as runbook E says; its settings stay in
   `~/.config/voice-journey-capture/`.) Then draft #31 and the cutover (runbook F), as
   phase 3's log lists them.
2. Review and sign off [PHASE-4-CHECKLIST.md](PHASE-4-CHECKLIST.md) (and D-037, D-038). Your
   sign-off and the cutover are the two gates of the removal PR.
3. Install the app on the PC and on the Air, set it up and sign in:

   ```sh
   cd ~/src/voice-lab && git pull --ff-only
   uv tool install --python 3.12 ./apps/desktop
   voice-lab-desktop      # Status → Settings…: https://voice-lab.<tailnet>.ts.net, https://id.<tailnet>.ts.net
   voice-lab-desktop --import-capture-data   # once: the old client's level pin, cal file, sessions
   ```

   Pocket ID's public client `voice-lab-desktop` (phase 2) already allows
   `http://127.0.0.1:*/callback`; nothing changes there.
4. Look through the screens against the real server: the Corpus with the whole corpus (about
   7,000 rows; scroll to the end, sort, filter), the Journey tabs, Practice, the Referee's
   results. The server serves no audio today (no `--playback-approval` in the stack's
   `serve` command): playback and the referee's trials stay off until you decide to record
   that approval. If you do, listen to a take in the Corpus and to a referee pair with
   headphones (the matched gain and the 8 kHz band limit).
5. Open `https://voice-lab.<tailnet>.ts.net/view/` on the phone, through the door: every page
   renders and none plays audio (the phone check of design §9's web-view acceptance).
