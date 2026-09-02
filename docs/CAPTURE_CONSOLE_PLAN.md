# Capture console implementation plan

Status: approved 2026-09-02 with the planned defaults below, not started.
Owner: Cole. Target: the MacBook Air capture host that already runs the capture
kit. Development branch: `capture-console`.

## Goal

Give the pilot and corpus recordings an intuitive local interface without
weakening a single capture invariant. The console wraps the existing
`cole_voice_lab.capture` library; it does not replace it. FFmpeg still records,
the library still finalizes, and the browser only presses the buttons.

## What exists today

`capture_prompt_suite` prints one prompt at a time, waits for Return, starts
FFmpeg in the foreground, and expects Cole to press `q` inside FFmpeg to end the
take. The pilot suite is 36 prompts, so that is 36 Return-then-`q` cycles with
no retake, no playback, no per-take feedback, and no resume: `Ctrl-C` aborts
the whole session and QC runs only after the last prompt.

The library is already shaped for a second driver. `capture_prompt_suite`
accepts injectable `read_line`, `emit`, `process_factory`, and `clock`
callables, `_capture_one` isolates the start/inspect/rename path, and
`finalize_capture_session` is the only writer of `manifest.json`, `qc.json`,
and `READY`.

## Invariants the console must preserve

These come from `AGENTS.md`, `SECURITY.md`, D-003, and
`docs/CAPTURE_AND_SYNC.md`; the plan fails review if any is weakened.

1. Audio is captured only by the unchanged `ffmpeg_capture_command`. The
   existing command-pin test stays byte-for-byte identical. The page never
   calls `getUserMedia`, Web Audio capture, or any browser recording API.
2. The console is a foreground process started from a Terminal window so
   macOS attributes the microphone permission exactly as it does for the CLI.
   No LaunchAgent, no background service, no auto-start.
3. The server binds `127.0.0.1` on an ephemeral port. There is no bind-host
   option, no TLS, and no Tailnet exposure.
4. Every `/api/*` request carries a per-run 256-bit token and passes exact
   `Host` and `Origin`/`Sec-Fetch-Site` checks, defeating CSRF from other
   browser tabs and DNS-rebinding attacks against loopback.
5. The transferable package keeps today's exact structure. Only
   `finalize_capture_session` writes evidence; rejected takes live outside the
   session directory; a `.partial.wav` is renamed only by the engine's
   inspect-then-rename path, never by hand and never by the page.
6. No new runtime dependency. Standard library only, so the hash-locked
   capture-kit runtime lock changes only through the lab wheel itself.
7. Raw recordings never leave the capture host through the console. Playback
   previews are built in memory, served only on loopback with the token, and
   never written to disk.

## Architecture

```text
Terminal (foreground)
  voice-lab capture console --asset-root … --session-id … --suite pilot --device …
    prints http://127.0.0.1:PORT/#TOKEN once; --open launches the default browser
        │
        ▼
  CaptureConsoleServer  (stdlib ThreadingHTTPServer, loopback only)
    GET  /                       console page with CSP and no-store headers
    GET  /static/console.js|css  bundled resources
    GET  /api/state              prompt, take, and QC state          (token)
    POST /api/take/start         start FFmpeg for the current prompt (token)
    POST /api/take/stop          write "q" to FFmpeg stdin, inspect, rename
    POST /api/take/accept        advance to the next prompt
    POST /api/take/retake        move the take to the rejected directory
    GET  /api/take/preview       in-memory 16-bit preview WAV         (token)
    POST /api/finalize           finalize_capture_session → READY
    POST /api/shutdown           stop the server after finalization
        │
        ▼
  TakeEngine  (new, in cole_voice_lab.capture)
    state derived from the session directory plus one in-memory recording handle
    start_take → ProcessFactory → Popen(stdin=PIPE)
    stop_take  → "q", wait ≤ 10 s, SIGINT fallback, inspect_canonical_wav,
                 os.replace, chmod 0600, fsync
    reject_take → rename into <tmp>/capture-<session>.rejected/, append log
    metrics    → WavInspection.qc_metrics with SNR against the first room tone
    finalize   → finalize_capture_session (unchanged)
```

Empirical check on 2026-09-02: FFmpeg spawned with a pipe on stdin exits with
code 0 and a closed, readable WAV header when the controller writes `q`. That
is the stop path. `SIGINT` remains the abort path, exactly as today.

## Phase 0: decisions and policy

Small. Land before any code.

- Add D-009 to `DECISIONS.md`: a loopback capture console may drive the native
  recorder; the browser controls and never captures. Include the resume rule:
  completed canonical takes in an unfinalized session may be resumed after
  re-inspection; partial files are rejected forensics and are never promoted.
- Extend `SECURITY.md` with the console threat model: loopback bind, token,
  `Host`/`Origin` checks, CSP, no browser capture, previews in memory only.
- Update `docs/CAPTURE_AND_SYNC.md` section 2 to describe both drivers and to
  replace "re-record the take into a clean session" with the resume rule.

## Phase 1: take engine in the library

Files: `src/cole_voice_lab/capture.py`, `src/cole_voice_lab/schema.py`,
`schemas/`, `tests/test_capture.py`, `tests/test_schema.py`.

Engine design:

- Split `_capture_one` into `start` and `stop` halves. `start` refuses an
  existing canonical or partial file, builds the unchanged command, and spawns
  through the factory. `stop` writes `q`, flushes, waits up to ten seconds,
  falls back to the existing `SIGINT`/terminate/kill ladder, then runs
  `inspect_canonical_wav`, `os.replace`, `chmod 0600`, and directory fsync.
- Extend the process protocol with a `signal_quit()` method. The default
  factory becomes `subprocess.Popen(command, stdin=subprocess.PIPE)`. The
  existing CLI loop keeps working unchanged because it still waits on the
  process while Cole presses `q` in FFmpeg.
- Add a `TakeEngine` class holding the layout, suite, device, session
  directory, recording entries, one optional live process, and the first room
  tone RMS. Public methods: `current()`, `start_take()`, `stop_take()`,
  `accept_take()`, `reject_take(reason)`, `metrics(index)`, `finalize()`,
  `abort()`. State is recomputed from disk on every call so the page can never
  drift from reality.
- Resume. `open_session(layout, session_id, resume=False)` keeps today's
  fresh-directory behavior. With `resume=True` it requires an existing
  mode-0700 real directory without `READY`, validates every canonical take with
  `inspect_canonical_wav`, fails closed on any unexpected file, moves any
  `.partial.wav` to the rejected directory with reason `interrupted`, and
  continues at the first missing entry.
- Rejected takes. Directory `<tmp>/capture-<session-id>.rejected/` (mode 0700)
  beside the session, files `NNN-<label>.take-<k>.wav` (mode 0600), and a
  canonical `rejected-takes.json` rewritten in full on every rejection. Add a
  `RejectedTakeLog` contract model, register it in `SCHEMA_MODELS` as
  `capture-rejected-takes`, and regenerate `schemas/`. Fields: `session_id`,
  entries of `relative`, `take_number`, `sha256`, `size_bytes`, `reason`,
  `rejected_at`.
- Per-take cap. A ten-minute engine timer auto-stops a forgotten take and
  records the finding `auto-stopped` in the state so the page can flag it.
- Metrics. Reuse `WavInspection.qc_metrics` with `noise_rms_dbfs` from the
  first accepted room tone and `allow_quiet` for room-tone entries, so the
  page shows the same findings QC will report at finalization.

Tests, following the existing fake-process pattern in `tests/test_capture.py`:

- A fake controlled process that writes the WAV when `signal_quit()` is called.
- Start, stop, accept, retake, resume, interrupt, and cap paths.
- Invariants: no overwrite of an existing take; an unexpected file still fails
  finalization; the rejected directory is outside the session; a failed stop
  never renames the partial; accepted takes are mode 0600.
- The command-pin test is untouched and still passes.

## Phase 2: console server and page

Files: `src/cole_voice_lab/capture_console.py`,
`src/cole_voice_lab/resources/capture-console.html`, `capture-console.js`,
`capture-console.css`, `src/cole_voice_lab/cli.py`,
`tests/test_capture_console.py`, `tests/test_resources.py`,
`tests/test_cli.py`.

Server:

- `ThreadingHTTPServer(("127.0.0.1", 0))` with daemon threads and one lock
  around the engine, so exactly one recording exists at a time and concurrent
  state changes answer 409.
- Token from `secrets.token_hex(32)`, printed once, carried in the URL
  fragment so it never appears in a request line or server log, sent by the
  page as `X-Capture-Token`, compared with `hmac.compare_digest`.
- Every request must present `Host` equal to `127.0.0.1:<port>`. Every
  `/api/*` request must additionally present `Sec-Fetch-Site: same-origin` or
  an `Origin` equal to `http://127.0.0.1:<port>`. Anything else answers 403.
- POST bodies are JSON only, capped at 64 KiB, with `Content-Type` enforced.
  Unknown paths and methods answer 404 and 405.
- Response headers on every response: `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy: same-origin`,
  `Cross-Origin-Resource-Policy: same-origin`, and
  `Content-Security-Policy: default-src 'none'; script-src 'self';
  style-src 'self'; connect-src 'self'; media-src blob:; img-src 'self';
  base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
- Preview endpoint reads the canonical take through `open_regular_no_follow`,
  converts 24-bit to 16-bit PCM in memory with the `wave` module, and returns
  `audio/wav`. The page fetches it with the token and plays a Blob URL.
  Nothing derived touches disk.
- `Ctrl-C` in the Terminal while recording follows the CLI abort semantics:
  `SIGINT` forwarded to FFmpeg, partial retained, `CaptureInterrupted` raised,
  non-zero exit, no `READY`.

Page, with no framework and no inline script or style so the CSP holds:

- Prompt text at two rem or larger, progress "12 of 36", category and style
  line, a full-width room-tone banner that says to stay silent, and a state
  pill that reads Ready, Recording with elapsed seconds, or Review.
- Buttons and keys: Start and Stop on Space, Play on P, Retake on R, Accept
  and next on Return. Focus stays on the primary action; state changes are
  announced through an `aria-live` region; color is never the only signal.
- Per-take readout after Stop: duration, peak dBFS, clipped samples, SNR
  against room tone, and each QC finding with a plain-language hint, for
  example "peak-too-quiet: move closer or speak up, then retake".
- Summary screen: every take with its QC status, a Finalize button enabled
  only when the set is complete, and after finalization the manifest and QC
  digests, the READY status, the headphone-review reminder, and the exact
  `sync plan` command with the absolute session path and a copy button.
- Elapsed time comes from the server's monotonic clock; the page polls
  `/api/state` every 500 ms only while recording.

CLI: `voice-lab capture console` with the same arguments as `capture run`
plus `--resume` and `--open`. It prints the URL once, runs until finalization
and shutdown, and has no bind-host option.

Tests:

- Server on an ephemeral port in a thread, driven with `http.client` against a
  fake engine: header set, token, `Host`, and `Origin` rejections, method and
  content-type enforcement, state transitions, 409 on invalid transitions,
  preview bytes decoded as 16-bit mono 48 kHz, finalize producing `READY` on a
  synthetic session through the real `finalize_capture_session`, shutdown.
- Resources: console files ship in the wheel, contain no `http://` or
  `https://` reference, no `<script>` without `src`, no `on*=` attribute, and
  no `style=` attribute.
- CLI: loopback-only bind asserted from the bound address, URL printed once
  with the fragment, `--open` calling a monkeypatched `webbrowser.open`,
  `--resume` routed to the engine.
- `scripts/guard-private-assets` stays clean with the new resources.

## Phase 3: docs, kit rebuild, acceptance on the Air

- Update `docs/CAPTURE_KIT.md` post-install commands, `docs/RUNBOOK.md`
  section 3, `README.md`, `CURRENT.md`, and `TESTING.md`.
- Commit, then rebuild the kit with `scripts/build-capture-kit` (offline from
  the reviewed wheelhouse, or `--allow-network` for a deliberate refresh).
  Retain the printed bundle and wheel digests outside the bundle.
- On the Air, verify with the two `shasum` commands, then bootstrap into a
  new capture-host root. Bootstrap refuses to overwrite a changed target, so
  the upgrade is a fresh sibling root; keep the old root until every session
  under its `assets/tmp` has been synced and accepted.
- Physical acceptance, recorded as a checklist in the runbook:
  1. First Start shows the macOS microphone prompt for Terminal.
  2. Only the UMIK-1 is selectable; the built-in microphone is rejected.
  3. Preview plays in Safari and Chrome.
  4. `Ctrl-C` during a take leaves a partial and no `READY`.
  5. `--resume` continues at the first missing prompt.
  6. A full pilot runs end to end to `READY` with QC status shown.
  7. `sync plan --live-plan --json` accepts the package and the mini intake
     accepts it idempotently.

## Phase 4, optional: live level meter

Deferred. A live meter needs a second FFmpeg output with `astats` and
`ametadata`, or a tee, which changes the pinned command shape even though the
recorded stream stays unfiltered. D-003's "no signal-processing flags" wording
would need a reviewed amendment first. Decide only after the post-take readout
proves insufficient in a real session.

## Verification gates

- `scripts/verify` passes under umask 022 and 077 with branch coverage at or
  above 90 percent, strict mypy on the new module, the exact schema set, and a
  clean asset guard.
- The command-pin test is unchanged.
- An independent review of `capture_console.py` against the invariants above
  happens before the kit is rebuilt.

## Sequencing and size

Strictly Phase 0, then 1, then 2, then 3. Phase 1 is roughly 400 to 600 lines
including tests. Phase 2 is roughly 700 to 900 lines including the page and
tests. Phase 3 is documentation plus one physical session on the Air.

## Decisions taken 2026-09-02

Cole accepted the planned defaults; do not reopen these during development.

1. Resume rule: completed, re-inspected canonical takes in an unfinalized
   session may be resumed. Partials are moved to the rejected directory and
   are never promoted. This relaxes the earlier "re-record into a clean
   session" guidance and is recorded in D-009.
2. Rejected takes are kept as forensics under the private `tmp` directory
   with mode 0600. They are never synced and never deleted automatically.
3. The console prints the URL once. `--open` is an opt-in flag that launches
   the default browser.
4. Playback previews are 16-bit PCM built in memory from the canonical take.
   The original 24-bit bytes are never served or rewritten.

## Kickoff

Start development in a fresh session on the `capture-console` branch with the
prompt below. Phases 0 to 2 and the documentation half of Phase 3 are agent
work. The kit rebuild and the physical acceptance session on the Air are
Cole's.

```text
You are working in /Users/cole/Projects/cole-voice-lab on branch
capture-console. Read AGENTS.md, docs/CAPTURE_CONSOLE_PLAN.md, DECISIONS.md,
SECURITY.md, docs/CAPTURE_AND_SYNC.md, src/cole_voice_lab/capture.py, and
tests/test_capture.py before writing code.

Implement the capture console end to end as specified in the plan: Phase 0,
then Phase 1, then Phase 2, then the documentation items of Phase 3. The four
decisions in the plan are final; do not ask about them again. Stop before the
capture-kit rebuild and the physical acceptance checklist, which Cole runs.

Hard constraints: the FFmpeg capture command and its pin test stay byte-for-
byte identical; the browser never captures audio; the server binds 127.0.0.1
only with a per-run token plus Host and Origin checks; only
finalize_capture_session writes manifest, QC, and READY; rejected takes live
outside the session directory; standard library only, no new dependencies;
strict mypy, Ruff, and branch coverage at or above 90 percent; register the
new RejectedTakeLog model in SCHEMA_MODELS and regenerate schemas with
scripts/generate-schemas. Tests must freeze any wall-clock stamp and create
directories with explicit mode 0o700; never rely on the umask or the date.

Process: finish one phase at a time, run scripts/verify until it is green,
then commit that phase with a short imperative subject in the repository's
style. Do not push. Do not run capture against real hardware, do not touch the
private asset root on this machine, and never write audio or model files into
the repository.

Finish with a report that lists the commits, the verify summary, any
deviation from the plan with its reason, and the exact commands Cole runs
next: the kit rebuild, the Air bootstrap into a fresh root, and the physical
acceptance checklist.
```
