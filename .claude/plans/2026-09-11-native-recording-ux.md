# Plan: Native, responsive recording experience

**Goal:** Deliver a native Mac recording workspace with a real-time waveform,
stable controls, fast Record → Stop → Save & Next, independent one-off takes,
and complete preservation of recording/evidence integrity and existing workflows.

**Out of scope:** Model/training changes, corpus expansion, hosted transfer,
audio enhancement, automatic trimming/transcription, iOS, and physical deletion
of human recordings. Planning does not authorize opening a microphone or
replacing the installed app.

## Context

- [Design and acceptance contract](../../docs/NATIVE_RECORDING_UX.md) is the
  normative UX/architecture specification; this file owns execution status.
- `capture.py:1236`, `recording_library.py:610`/`:902`, and
  `desktop_window.py:929` repeatedly inspect WAVs on interactive paths. A
  synthetic Save & Next after five ten-second takes took 31.7 seconds/95 inspections.
- `capture_native.py:520` emits counts only; the current waveform has no live
  feed. `desktop_window.py:1330` changes layout and uses global busy interlocks.
- Preserve the existing uncommitted work, all historical formats, and regressions
  from earlier navigation, timer, continuity, trimming, and Stop-color fixes.
- Target SwiftUI/AppKit + native Core Audio capture, with Python lab contracts,
  metadata/control service, and isolated analysis. No Rust/C++ dependency.

## Execution rules

Steps are ordered and initially unchecked. Newly named modules, test suites,
scripts, and command flags below are deliverables, not claims that they already
exist. Create each verification entry point in the step that first needs it.
All automated tests and benchmarks generate synthetic audio in resolved private
temporary directories outside Git; they never use the operator's asset root.

Use one canonical implementation per capability. Extend existing audio
inspection, edits, raw validation, and intake rather than porting their rules to
Swift. Native contracts exchange explicit errors and revisioned results.
Do not call legacy `DesktopRecordingSession.current()` or `_scan()` from the new
interactive service. Do not optimize by weakening publication/intake validation.

Run the focused Verify command for each step, then `scripts/verify` before
declaring an implementation increment complete. From step 3 onward that script
must also exercise the available native build/tests. Record actual evidence
before checking a box. Do not wait until the last step to test the integrated
recording path. Keep code changes isolated from unrelated user edits.

Temporary old/new routing belongs in matching `*_migration` modules/tests with
`@migration` start/target dates and
`migrations/active/native_recording_ux.md` once rollout code begins. Keep durable
historical-format readers separate from temporary migration routing. Existing
raw/consent assets are never transformed or dual-written. Reversible index
migrations and data backfill are separate changes. No delegation is required.

## Milestone A — Measured foundation and native capture

- [x] **Step 1: Establish reproducible recording latency measurements.**
  - Deliver: `scripts/benchmark-recording`, generated synthetic fixtures in
    `tests/recording_fixtures.py`, and `tests/test_recording_latency.py`.
    Support `--synthetic --backend legacy|native --profile smoke|acceptance`;
    implement legacy measurement in this step and native measurement when the
    native runtime exists. Reports go outside Git.
  - Instrument: command acknowledgment, first confirmed frame, Stop delivery,
    file seal, durable Save, next prompt render, WAV inspection/open counts,
    background queue latency, frame age, and process/build identities. Use
    structured private traces; never log audio/transcripts.
  - Verify: `.venv/bin/pytest tests/test_recording_latency.py`; then
    `scripts/benchmark-recording --synthetic --backend legacy --profile smoke`.
    Reproduce history-dependent scans; capture a new baseline and exact host
    configuration rather than asserting the old wall times must repeat.

- [x] **Step 2: Define strict take and process contracts.**
  - Deliver: `src/cole_voice_lab/recording_contracts.py`, schema-generator
    registration, and `tests/test_recording_contracts.py`. Specify take/group
    evidence, capture reservation/seal receipt, save operation, analysis result,
    package mapping, recorder/service commands, reliable events, and telemetry.
  - Include: independent transport/decision/analysis/export states; stable take
    and operation IDs; exact prompt/catalog/subject/purpose bindings; UTC and
    sample times; byte/frame/digest evidence; revisions and generation tokens;
    bounded messages; explicit error codes; canonical/recovery serialization.
    Start explicitly carries a Python-computed reservation digest, so the native
    helper can bind its receipt without duplicating canonical serialization.
  - Verify: `.venv/bin/pytest tests/test_recording_contracts.py tests/test_schema.py`;
    `.venv/bin/python -m cole_voice_lab.schema --check`.
    Cover invalid versions, unknown fields, invalid paths, nonfinite metrics,
    mismatched identities, and malformed state transitions. Fixtures are
    generated in temporary storage, not committed private-looking JSON instances.

- [x] **Step 3: Create the native package and test harness.**
  - Deliver: `apps/macos/Package.swift`, a domain README, shared
    `RecordingContracts`/`RecordingCore`/`RecordingUI` targets, the app and recorder
    helper executables, and a minimal C11 callback-buffer target. Use system
    frameworks, Swift 6 strict concurrency, and an explicit macOS deployment floor.
  - Add: `scripts/verify-native-recording`, synthetic AppKit/SwiftUI hosting and
    geometry/snapshot harness, and cross-language contract conformance tests.
    Keep build outputs under `build/`; update ignore/asset guard handling only
    for actual build outputs. Record compiler/SDK identity without downloading tools.
  - Verify: `scripts/verify-native-recording --suite contracts` and
    `scripts/verify-native-recording --suite shell`.
    Build on the installed Command Line Tools, render an inert window in both
    appearances, validate protocol fixtures both directions, and inspect the
    resulting executable deployment target. Add this harness to `scripts/verify`.
  - Gate: Resolve build/deployment incompatibility here. Full Xcode/XCUI tests
    cannot be claimed when only Command Line Tools are installed.

- [x] **Step 4: Implement continuous native capture and sealing.**
  - Deliver: the canonical Audio Queue recorder in `RecordingCore` and the
    preallocated callback/ring target; port current stable device UID, format,
    sample timestamp continuity, failure, and independent maximum-duration behavior.
    Keep a persistent helper process without recording between takes.
  - Implement: allocation-free bounded callback; ordered writer; separate
    bounded envelope handoff; exact written-frame clock; prompt Stop/cancel;
    final header/fsync/digest/receipt; safe queue shutdown; recoverable partials.
    Preserve raw mono 48 kHz 24-bit PCM without added processing.
  - Verify: `scripts/verify-native-recording --suite capture` and
    `scripts/verify-native-recording --suite capture-sanitizers`.
    Synthetic buffers must round-trip exact PCM and counts. Inject timestamp
    gaps/overlaps, writer overflow, device/format change, late callbacks, disk
    failure, Stop during startup, and controller teardown. Check captured audio
    remains correct when telemetry is blocked. Record sanitizer/tool limitations.

- [x] **Step 5: Connect reliable local control and telemetry.**
  - Deliver: `src/cole_voice_lab/recording_service.py`, CLI service entry point,
    Swift `RecorderClient`/`RecordingServiceClient`, and
    `tests/test_recording_service.py`. Keep native Stop directly reachable.
  - Implement: bounded framed local IPC, version/build handshake, separate
    coalescing visual channel, reliable transitions, request IDs, revision guards,
    cancellation, resnapshot on reconnect, and parent-loss shutdown. Share the
    asset-root recording lock with Qt/console through the common ownership boundary.
  - Verify: `.venv/bin/pytest tests/test_recording_service.py` and
    `scripts/verify-native-recording --suite bridge`.
    Exercise split/truncated/oversized frames, duplicate Start/Stop, stale events,
    blocked telemetry, helper/service death, version mismatch, and two clients.
    A synthetic start/stop must already traverse the real process bridge here.
    Durable service registration is connected in step 6; before that backend
    exists, a valid seal submission must report an explicit not-ready error.

## Milestone B — Independent takes and responsive library

- [x] **Step 6: Persist takes independently of sessions.**
  - Deliver: `src/cole_voice_lab/recording_takes.py` and
    `tests/test_recording_takes.py`, reusing existing private path and canonical
    write primitives. Implement reservation, sealed draft registration, immutable
    save-operation records, decision revisions, and automatic context groups.
  - Define: filesystem commit ordering, authoritative evidence versus index,
    operation-ID replay, group changes, pilot/holdout gate binding, timestamps,
    prepared corpus-gate readiness, and separate saved/checking/verified/invalid
    states. Stop preserves a draft; only explicit Save records the keep decision.
    No session form blocks a take.
  - Verify: `.venv/bin/pytest tests/test_recording_takes.py tests/test_paths.py tests/test_canonical.py`.
    Interrupt each write/fsync/rename/ack boundary; re-open and retry. Saved
    decisions must survive, unsaved drafts must not become kept, and retries
    must not create duplicate takes or decisions. No successful Save before sync.
    Also prove helper-process capture/seal followed by durable service draft
    registration, completing the staged process integration from step 5.
  - Wiring refinement: the service returns a strict reserved-take event with
    Python's canonical reservation digest. Reserve names the recorder generation
    separately from the service request generation, so the returned reservation
    can actually authorize Start on the intended helper without Swift hashing.

- [x] **Step 7: Build one indexed library projection.**
  - Deliver: `src/cole_voice_lab/recording_index.py`, reversible timestamped
    SQLite schema migrations, separate legacy metadata backfill, and
    `tests/test_recording_index.py`. Extend `recording_library.py` as the public
    history capability rather than making an unrelated second library API.
  - Import: old desktop drafts, rejected/removed takes, finalized tmp packages,
    accepted raw, session-only dates, and new take evidence. Preserve identities,
    exact prompt matches, deduplication, index revision, and completeness status.
    Update affected rows incrementally; rows/groups/counts share one snapshot.
  - Verify: `.venv/bin/pytest tests/test_recording_index.py tests/test_recording_library.py`.
    Assert zero WAV opens for repeated unchanged metadata reads, bounded paging,
    correct invalidation/rebuild, Unknown versus Missing, and identical coverage
    after restart/backfill. Keep old strict integrity tests at their proper boundary.

- [x] **Step 8: Run verification independently of controls.**
  - Deliver: `src/cole_voice_lab/recording_analysis.py` and
    `tests/test_recording_analysis.py`; persistent resumable jobs with one initial
    worker process and a bounded in-memory scheduler. Reuse `audio.py` inspection.
  - Key results by source digest, analysis version, and applicable edit/context
    revision. Verify new/changed originals; never rescan history on status polls.
    Expose pending, needs-review, verified, and invalid results. New take jobs
    get priority while historical reconciliation progresses fairly.
  - Verify: `.venv/bin/pytest tests/test_recording_analysis.py tests/test_audio.py tests/test_recording_service.py`.
    Block QC deliberately while proving start/stop/save metadata still completes;
    test process crashes, restart, stale results, tampering with preserved mtime,
    independent retries, and bounded resources. Keep strict raw acceptance checks.

- [x] **Step 9: Make Save and navigation one durable operation.**
  - Deliver: queue behavior in the existing recording library/service capability
    and `tests/test_recording_navigation.py`. Implement Save, Save & Next,
    one-off recording, Continue Missing, and explicit selected-prompt retakes.
  - Resolve the next target from indexed exact-prompt coverage including the
    proposed keep decision; persist both before acknowledgment. Return one
    complete response, never a provisional batch
    target. Preserve queue cursor/operation IDs; an unrelated QC revision does
    not invalidate a correct Save. Missing historical metadata blocks only
    coverage-dependent navigation, with an explicit explanation.
  - Verify: `.venv/bin/pytest tests/test_recording_navigation.py tests/test_recording_service.py`.
    Cover duplicate room tones, cross-group history, wrapping, sort/filter
    independence, changed prompt text/style, saved-pending-QC, rejected/removed/
    invalid takes, drafts, all-complete, unknown history, and lost Save replies.
    Assert no historical WAV opens and no automatic microphone start.
  - Wiring refinement: bootstrap the automatic context through a typed service
    command, and start bounded background discovery without a test-only index
    completion setter. Reconcile native heads incrementally; separate the legacy
    discovery checkpoint so new saves never trigger historical audio rescans.

## Milestone C — Native recorder and review experience

- [x] **Step 10: Draw the actual live and stopped waveform.**
  - Deliver: sample-bucket envelope generation, bounded multiresolution cache,
    and a native `RecordingWaveformView` that shares coordinates between live
    capture, stopped overview, seeking, and trim handles.
  - Render: rolling recent audio, signed extrema, frame-based time, true silence,
    fixed recording scale, explicit display-only fitting, and the stopped
    overview without rereading the whole library. Coalesce visuals independently
    of reliable capture state. Ignore stale take/generation snapshots.
  - Verify: `scripts/verify-native-recording --suite waveform`.
    Feed deterministic impulses/silence/sine/clipping and varied buffer sizes;
    assert preserved peaks, frame mapping, bounds, generation handling, bounded
    memory, final count agreement, slow-render behavior, and light/dark snapshots.

- [x] **Step 11: Implement the fixed recorder workspace.**
  - Deliver: native library sidebar, optional user-selected Focus mode, fixed
    script/guidance/take/waveform/timer/transport/footer regions, and explicit
    transport view state bound to real clients. Use native typography and icons.
  - Replace all phase-dependent region collapse with persistent controls and
    dependency-specific disabling. Connect Record/Stop/Save to one coherent
    state transition. Hold the old script/waveform through Save; apply only the
    final next prompt. Stop remains active during recording and startup.
  - Verify: `scripts/verify-native-recording --suite workspace` and
    `scripts/verify-native-recording --suite transport`.
    Assert region bounds within one point across every phase at both target
    window sizes/appearances. Exercise delayed start/seal/save/QC, prompt text
    event history, silence, long scripts, filtering, current-target alignment,
    no focus jumps, and the previous red-Stop flicker regression.

- [x] **Step 12: Preserve complete review and keyboard behavior.**
  - Deliver: frame-range playback via AVAudioEngine/AVAudioPlayerNode,
    dated/current take selection,
    per-take cursor memory, precise seeking/skips, trim handles and numeric
    fields, Save trim/Reset, recoverable Retake/Remove/Restore, and shortcuts.
    Extend the canonical `recording_edits.py` for new identities and async
    derivative generation; never implement alternate edit rules in Swift.
  - Retain: original PCM, explicit edit decisions, current-draft Save guards,
    no autoplay, playback/capture exclusion, typing/dialog/key-repeat guards,
    accessible names, Reduce Motion, contrast, and non-color state indicators.
  - Verify: `scripts/verify-native-recording --suite review` and
    `scripts/verify-native-recording --suite accessibility`;
    `.venv/bin/pytest tests/test_recording_edits.py tests/test_recording_takes.py`.
    Verify exact output frames/digests, stale trim jobs, historical selection,
    unsaved edit decisions, keyboard focus, playback failures, and source bytes
    unchanged. Automated tests never call audible playback.

- [x] **Step 13: Prepare compatible lab packages from takes.**
  - Deliver: `src/cole_voice_lab/recording_packages.py`, explicit native Prepare
    for lab flow, package-to-take maps, and `tests/test_recording_packages.py`.
    Reuse current finalization/intake instead of relaxing v1 formats.
  - Require: saved and independently verified takes, exact suite/purpose/subject,
    compatible device/host/group context, and explicit room-tone bindings. Split
    incompatible selections. Explain missing prerequisites in this flow, never
    by redirecting Save & Next. Keep READY last and original bytes immutable.
  - Preserve: dates and deduplication locally, edit/removal identity across
    staging/finalized/raw aliases, raw-only derivative semantics, and truthful
    session-only history when a transferred v1 package lacks rich sidecars.
  - Verify: `.venv/bin/pytest tests/test_recording_packages.py tests/test_recording_package_jobs.py tests/test_intake.py tests/test_references.py tests/test_datasets.py tests/test_recording_library.py`;
    `scripts/verify-native-recording --suite package-preparation`.
    Test one-off saved without room tone, blocked preparation, valid full pilot,
    split contexts, tampering/undeclared files, interruption before READY,
    idempotent intake, alias removal enforcement, and unchanged legacy packages.

## Milestone D — Recovery, measured acceptance, and cutover

- [x] **Step 14: Complete recovery across failures and old drafts.**
  - Deliver: `tests/test_recording_recovery.py` and native recovery scenarios;
    finish process supervision and storage/resource diagnostics from the design's
    failure matrix. Resume sealed drafts, interrupted saves, and queued analysis
    without reopening the microphone or inventing successful verification.
  - Support: legacy unfinished groups through an explicit importer/continuation
    path that preserves original provenance and never requires rewriting old
    raw. Preserve incompatible older device configurations as readable history;
    new takes use the supported native recorder and a new context group.
  - Verify: `.venv/bin/pytest tests/test_recording_recovery.py tests/test_recording_index.py`;
    `scripts/verify-native-recording --suite recovery`.
    Kill each process at meaningful commit boundaries, inject disk/fsync faults,
    delay jobs, emulate unplug/sleep/configuration change, test second-app
    ownership, and verify selected prompt/decisions after restart. No lost takes
    or silently successful partials; no global freeze from a per-take QC failure.

- [x] **Step 15: Package a reversible native preview release.**
  - Deliver: `scripts/build-native-recording-app`, exact build/service identity
    metadata, application icon/Info.plist/permissions, helper signing, and
    `tests/test_native_recording_application.py`. Separate build from installation.
    Use an explicitly named preview app and preserve the installed Qt launcher.
  - Keep: private asset-root configuration, checked interpreter/protocol,
    supported deployment target, no bundled assets/models, no automatic tool
    downloads, and no restart of the user's app. Document rollback's limit:
    the older app does not edit native-format drafts, which remain preserved.
    Derive the effective bundle minimum from app/helper/service executable
    probes; the installed Python requires macOS 26.0 even though native code
    retains a macOS 13 deployment floor.
  - Freeze the pre-cutover code and working Python/Qt environment as an isolated
    rollback release. The current `.app` points into the mutable checkout, so
    copying only its launcher is insufficient. Resolve editable imports to the
    frozen code and verify retained dependency identities without downloading
    replacements. Include relevant uncommitted desktop code in this snapshot.
  - Verify: `.venv/bin/pytest tests/test_native_recording_application.py`;
    `scripts/verify-native-recording --suite packaged-app`.
    Launch only a synthetic fixture app, verify bundle/child identity and
    permission metadata, test spaces in paths/missing service/version mismatch,
    preview coexistence/locking, and rollback against a disposable fixture root
    after its simulated primary checkout no longer has Qt code/dependencies.

- [ ] **Step 16: Prove performance and physical recording quality.**
  - **Continuation 2026-09-14 (preview readiness only):** Read the current handoff,
    archived physical context, manual recording report and current evidence matrix
    before observing the existing diagnostic preview. The preview remains idle
    and available; no recording, playback, permission reset, shutdown or install
    was initiated. Fresh read-only host observations found 83.6–99.4% helper CPU
    while idle; a three-second sample attributes this to repeated completed-take
    waveform serialization/schema validation on the telemetry lane. Correct this
    bounded terminal-publication defect and verify final delivery, blocked-pipe
    recovery and fresh subsequent capture without reopening the completed memory
    or reconciliation investigations.
    Duration evidence now establishes intact synthetic PCM, a 40 ms shortfall
    relative to the declared 600 seconds, and a pre-Stop telemetry snapshot 100 ms
    behind the sealed/final waveform. The wall deadline includes startup; precise
    historical startup versus timer-coalescing attribution remains unavailable.
    Thirteen focused native capture tests pass. A separate synthetic timer probe
    reproduces skipped scheduled chunks when callbacks coalesce; correct that
    fixture behavior and test bounded catchup without changing real capture bounds.
    Keep the old failed report and all raw observations. The operator delegated
    the simplest reliable policy: retain the hard 600-second elapsed limit.
    Section 12 explicitly amends only that maximum-duration case, reports nominal
    shortfalls separately, and defines a versioned initial-review timer/source
    agreement observation while retaining every pre-Stop value. Implement and
    verify these measurement corrections before the full run; no current
    acceptance pass is claimed and no real capture safety bound changes.
    Add a supported external Swift scratch override, then run the full native
    acceptance with two repetitions to power the history-memory comparison;
    default one-repetition sampling has only two observations where three are
    required. Preserve actual plain Save versus advancing Save samples and report
    every row, including misses/unmeasured/underpowered rows. Regenerate the matrix
    and run scripts/verify unlocked. Only then coordinate each physical step with
    operator-controlled timing, approved speakers and no short chat-timed guard.
    The operator confirmed all 36 configured pilot prompts as the intended set.
    Installation destination
    confirmation, a fresh configured-library count/headroom report and one real
    configured-root take remain later gates. All original app/runtime pairs,
    human takes, provenance and rollback stay in place; Step 17 is excluded.
    Evidence: `readiness-20260914-ihozooyb/` under the private defect-b root;
    duration proof: `Verification/20260914-duration-boundary-pbljss51/`.
  - **Continuation authorized 2026-09-12:** The operator accepts current measured
    Stop-to-review speed provisionally and authorizes the executor to choose and
    implement the large-library reconciliation fix after evidence and a written
    design. Complete characterization, document the options/operator consequences,
    implement and verify, then continue through performance and physical gates
    toward a separately named preview. Preserve the original numerical performance
    targets and report any misses explicitly. Step 17/live-app cutover remains a
    separate authorization; presence and preview-destination confirmations remain.
    Current continuation: boundary 2 was reported on 2026-09-13 after the Mac
    was unlocked. The observed native transition was 29–31 takes (first current
    breach 30; earlier first breach 25), with 74.476 s normal-module phase
    reconciliation at 1,000 and all 1,000 jobs repeated on reopen. The operator
    authorized the executor to choose and implement the written design.
    Characterization, the chosen proposal and excluded trials are recorded in
    `~/Library/Application Support/Cole Voice Lab/Verification/20260912-defect-b-vch9e1h7/continuation-status.md`.
  - **Boundary 1, authorized 2026-09-12:** Fix the native client's unbounded
    per-cycle memory and take-descriptor retention. Playback source-release
    semantics may change, including Step 12 behavior. Do not raise a bound.
    Verify: lifecycle regression tests with failing mutants, measured synthetic
    release cycles, all existing native suites, and `scripts/verify`. Stop and
    report before investigating the separate large-library opening defect.
    Result: **verified 2026-09-12; stopped at boundary 1** (evidence below).
  - Fix (b), chosen and reported 2026-09-13; keep these unchecked until verified:
    - [x] Service-owned discovery/package index publication and historical analysis
      preparation outside interactive locks. Verify focused discovery, index,
      package and analysis suites; held-worker and stale-publication mutations.
    - [x] Background recovery inventory and explicit plain Save during incomplete
      coverage, preserving recovery actions and current capture protection. Verify
      recovery/service/contracts and native workspace/recovery suites, including
      Reserve-before-Start and replay/pagination races.
    - Measured follow-up, 2026-09-13: the first atomic legacy publication held the
      index lock 99–155 ms; later identical-source clean trials were 44–54 ms.
      Profiling identified per-entry SQL and redundant validation rather than commit
      or audit. Batch prepared publication within the existing atomic/conflict guards,
      then remeasure without changing the 50 ms target. The physical-gate review also
      found a device-loss UI dead end; expose explicit reconnect after a matching
      terminal error while preserving drafts/trim and forbidding automatic Record.
      Source freeze reopens for these two bounded corrections before final timing.
    - Measured follow-up, 2026-09-13 (release 02): native 1,000-take open
      enabled Record in 1.570 s with coverage unknown and closed cleanly. Same-build
      restart completed background bootstrap in 118.758 s, re-enumerating all 1,000
      takes; foreground responses remained below five seconds. A separate capture
      sealed an exact one-second Verified Draft but did not finish Save within its
      390 s diagnostic bound. Its harness forces SaveNext and hides Draft Review
      errors; add bounded error evidence and prove actual plain Save during incomplete
      coverage. DiscoveryBegin also needs immediate history notification so the
      visible Save label follows a new Building state before the scan finishes.
      Release 03 then proved actual native Record/Stop/plain Save during Building:
      48,000 frames, kept decision, immutable nonadvancing Save and matching audio
      digest. Its cleanup verdict remains failed because a child identity was not
      recorded; a separate probe establishes that caffeinate can be an inherited
      child. A fresh forced-Next run identified HISTORY_UNKNOWN while the visible
      action was already Save. Adapt the benchmark to the displayed action, count
      plain Save separately, preserve actual advancing samples and expose missing
      power per required design point. Record child identities and distinguish
      inherited diagnostic processes in the closure check. These are corrections
      to verification within (b); no latency target is changed.
    - Measured follow-up, 2026-09-13 (release 04): the default acceptance run
      crashed in the Python fixture builder's SQLite WAL-index read before the
      1,000-take case launched. Three capture phases completed, but the first
      subsequently failed navigation; six later cases were unrun and no aggregate
      report exists. Preserve all partial evidence and latency/inspection misses.
      A bounded external-lock probe proved that the path guard's auxiliary closes
      release SQLite locks, and an instance's retained database guard can release
      another instance's lock on close. Replace those guards with no-follow stat
      and fixed inode identity, serialize close, and preserve all path checks.
      Focused index tests (60), four failing mutations, independent review and
      the restored lock probe pass; full verification remains required. Also make
      fixture drain failures explicit and correct Continue Missing measurement
      availability/no-op guards. These findings do not establish the exact crash
      trigger or justify any changed performance target. Evidence is retained in
      `native-acceptance-04`, `sqlite-guard-fix-01`, `sqlite-guard-mutations-01` and
      `service-trace/sqlite-lock-retention-01`/`02` under the private defect-b root.
    - Verification follow-up, 2026-09-13: actual normal-module startup evidence
      showed the old WAV audit labels the service as a worker before Python updates
      argv[0]. Earlier zero-service-read reports cannot establish that gate. Native
      descriptor differences also used a changing selection exemption. Require
      process startup/open controls and actual source inspection spans from both
      client reader branches, with immutable command-issue exemptions and explicit
      overflow/missing-control rejection. Correlate metadata timing to a real request
      and its applied response. Strict combined Swift checks pass 78 tests across
      13 suites; Python integration, hook mutation and fresh release measurements
      remain required. Section 12 visibly records the evidence correction without
      changing a target. Preserve the old reports as originally produced.
      Release 05 follow-up: combined Swift checks pass 78 tests in 13 suites;
      removal of the actual retained-file open hook fails the new source tests,
      and the restored hook passes. Focused Python integration passes 150 tests;
      exact fixture-preflight process classification separately passes 37 tests.
      Two fresh 20-cycle runs pass the current client/process audit controls.
      The unlocked control's Stop-to-review p95 is 192.021 ms and advancing Save
      p95 is 78.278 ms at 4–24 takes with 106 catalog prompts. The instrumented
      run started and ended locked, so it is descriptive diagnostic evidence,
      not an overhead comparison or unlocked acceptance. Full acceptance awaits
      another unlocked window; these results do not check off the gates below.
      Release 05 full-run follow-up: the fresh unlocked run completed two
      100-cycle capture/Save phases, but both failed source-control validation.
      The harness standardized `/private/tmp` exemption paths to `/tmp` while
      actual inspection events preserved `/private/tmp`. All 100 sources in the
      first case had matching take/hash/device/inode identities and complete
      reader controls; no events were dropped. Stop the owned synthetic run,
      preserve its invalid reports, correct the harness to retain the authorized
      path, prove the regression with a failing mutation, and rerun all cases.
      The 20-cycle diagnostic used a `/Users` root, so this is a path-alias
      difference, not evidence of a 20-versus-100-cycle resource limit. No target
      or source-inspection requirement changes.
    - Measured follow-up, 2026-09-13 (full release 06): all ten declared cases
      ran in 3,847.714 seconds, with nine completed and the idle 1,000-take case
      excluded after a dispatched Continue Missing received HISTORY_UNKNOWN.
      Preserve this correct fail-closed rejection; no ordinary recording dead end
      is established. The canonical table has three passes, three failures, two
      underpowered rows and seven unmeasured rows. Stop-to-review p95 is
      2,211.148 ms; metadata p95 is 376.555 ms; recorder/waveform growth passes
      at 44.281 MiB. A separate private metadata-only profile will distinguish
      client processing from service/lane waiting before further latency changes.
      The 600-second case also exposes one timer disagreement: expected
      28,800,000 frames, sealed 28,798,080, timer-at-Stop 28,793,280. Its retained
      reached-duration flag says true, so investigate both the maximum-duration
      boundary and the benchmark's terminal-phase predicate before correcting
      anything. Keep original reports and all targets unchanged. Exact aggregate
      and source/closure evidence are in `native-acceptance-06` and
      `full-report-preparation` under the private defect-b root. The final host
      boundary was unlocked with zero swap; all six tracked processes exited.
      The private 1,100-take control/sample profiles then acknowledged all 205
      planned commands each, with no added takes or surviving peers. Client CPU
      sampling identifies repeated schema validation and regex construction.
      Make one bounded prepared-validator/cache correction, preserve strict
      semantics and autorelease pools, mutation-check it, and remeasure once.
      Correct the harness's false duration-reached/terminal Stop accounting;
      its local Stopping publication is not a helper receipt and must become
      a diagnostic, leaving the unchanged controller target unmeasured. Preserve
      the original before-Stop versus final-frame discrepancy and safety limit.
      Optional stale navigation-status cleanup remains unapplied; it is separate
      from the correct fail-closed rejection. Then complete required verification
      and report remaining gates without further open-ended optimization.
      The bounded validator correction is implemented: 24 focused tests, six
      killed mutants and 281 Python/Swift conformance cases pass. Its sole fresh
      1,100-take remeasurement acknowledged all 205 commands and closed cleanly;
      history p95 is 197.501 ms and Next Missing p95 is 84.958 ms. First navigation
      availability waited 61.609 seconds outside the command for Complete.
      Both latency targets remain missed. Preserve this private diagnostic as
      changed-build evidence separately from full release 06; do not restart an
      open-ended optimization cycle. Exact sources, report and closure are in
      `metadata-profile-remeasure-07/report.md` under the private defect-b root.
      Benchmark boundary correction is also implemented: 118 focused Python and
      25 strict native harness tests pass; seven isolated mutations fail and
      restored checks pass. Actual sealed frames determine duration completion,
      terminal/no-op Stop produces no latency sample, and local Stopping is a
      named diagnostic rather than controller receipt. Preserve failed-case raw
      frames and the original release 06 table. Run one current-source native
      smoke for the affected path, then regenerate the current matrix from that
      smoke and complete required verification. Label full release 06 performance
      separately as historical; neither smoke nor new parity checks reruns it.
      The current three-cycle smoke passed with exact 24,000-frame takes and
      three advancing Saves; independent audit found zero historical inspections
      across 55 command windows. Full native verification then stopped at package
      preparation: all four takes were kept/verified, but the harness submitted
      room tone plus one utterance and waited for a blocked result. The service
      correctly prepared that two-entry package. The harness waits for analysis
      verification without the kept projection, then blindly toggles candidates;
      the retained command proves a selection mismatch, while the exact projection
      timing was not recorded. Correct only this synchronization: wait for actual
      package eligibility, assert exact selections, and test a held kept projection.
      Preserve timeouts and the failed `verification-release07` root. Then create
      fresh source/build evidence and rerun the affected and required checks.
      That correction now passes nine strict tests, an eligibility-removal mutant
      fails, and the one real synthetic package integration passes in 5.480 seconds.
      Four takes are kept/verified; selected counts are four, then one (blocked),
      then four (prepared), with idempotent resubmission. Evidence is retained in
      `package-harness-synchronization-08/result.json`. Full verification remains
      required; no product behavior or numerical bound changed.
      **Verified 2026-09-13 (release 08):** Fresh current-source smoke passes three
      exact 24,000-frame captures and three advancing Saves, with zero historical
      inspections across 55 audited command windows. All native suites pass and
      regenerate the matrix in 237.434 seconds. `scripts/verify` passes in
      571.694 seconds: 2,358 Python tests, one explicitly opt-in rollback replay
      skipped, 90.16% branch coverage, native suites, lint/types, package build,
      schema consistency and private-asset guard. Both stage boundary observations
      show the Mac unlocked. All 399 frozen files match after verification and
      no fixture peers remain. Evidence: `verification-release08/results.json`,
      `closure-check-01.json` and `current-evidence-08-02/current-evidence-matrix.md`
      under the private defect-b root. The matrix verifies 17 functional rows;
      current smoke is not a new full acceptance run. Historical full release 06
      failures and the later controller-endpoint correction remain open. Step 16
      is still unchecked: physical gates, numerical measurement gaps and separately
      confirmed preview installation/handoff remain. Step 17 is not authorized.
    - Readiness findings, 2026-09-14 (freeze 09). The delta review, smoke and
      2-repetition full acceptance ran (`readiness-20260914-ihozooyb/native-acceptance-09`,
      table `acceptance-09-report/full-15-row-table.md`; all 15 rows reported, 4 of 20
      cases failed). The physical diagnostic session `physical-20260914T064801Z-c65af236`
      (P01–P09) exposed further defects. Each fix below has a written, adversarially
      reviewed design in the readiness root. Keep every failed report and all
      diagnostic takes; change no target.
      - [x] Discovery Complete→Building flap at large libraries:
        `discovery-flap-design-01.md`. Complete is published only from a scan whose
        native evidence stayed stable; otherwise retry as deferred. Verify: new
        discovery tests plus mutation; existing discovery, navigation and service
        suites.
      - [x] Seal timestamp consistency across clocks: `seal-timestamp-design-01.md`.
        lastAt and sealed_at are anchored at close, and ordering is checked before
        publication. Verify: injected-clock NativeCaptureTests plus mutations;
        strict Swift suites.
      - [x] Harness observer retention in the memory row:
        `harness-memory-design-01.md`. Intern the reservation group/catalog in
        captureBoundaries. Verify: RecordingHarnessTests interning/exactness tests
        plus mutations; frame-boundary Python tests unchanged.
      - [x] Unsealed capture terminal dead end (physical P09):
        `unsealed-terminal-dead-end-design-01.md`. Explicit recheck, idempotent return
        to Ready, reachable recovery, and a benign zero-frame presentation. Verify:
        Swift model tests via an injectable recover seam, Python incident-state
        recheck test, e2e start-barrier regression, and mutations.
      - [x] Waveform unreadable at real UMIK-1 speech levels (P05, peaks −28 to
        −33 dBFS). Use a labelled display-only dB amplitude scale for live and stopped
        waveforms, while extrema still come from real written frames. Verify:
        waveform model tests (mapping, monotonic, full scale, silence floor);
        geometry/accessibility harness.
      - [x] Returning to a Recorded prompt shows "No recorded take" with Play
        disabled (P06). Explicit prompt selection resolves the latest retained take
        atomically in the service cursor, and review is prepared without autoplay.
        Amend spec §5. Verify: navigation/takes Python tests, workspace tests, and
        adjusted harness assertions.
      Evidence for these six ticks (2026-09-14/15), all in the readiness root: `fix-<key>-0N/result.json`
      and `review-<key>-0N`. Each fix passed its focused suites and killed mutants, and an
      independent reviewer re-ran them and approved. Discovery needed three repair rounds and
      fails closed on a malformed completion. The unsealed-terminal fix needed one. Take
      auto-select adds an optional `selected_take_id` to the navigate cursor contract.
      Disclosed residuals: a one-time interim Complete during the first legacy backfill; the
      window between the final fingerprint and the Complete commit; a superseded auto-review
      can delay the next reservation for long uncached takes. The full rerun below remains
      required.
      - [x] Product features requested 2026-09-14 are executed via
        `.claude/plans/2026-09-14-prompt-labels-instructions-test-suite.md` before
        the next verification run. (Done 2026-09-15: Steps 1–9 of that plan are
        ticked with implementer and adversarial-review evidence in
        `readiness-20260914-ihozooyb/feature-step-*` and `review-feature-step-*`;
        Step 9 = `scripts/verify` plus native `all`, screen unlocked, no source drift.)
    - [ ] Repeat the frozen release 1,000-take open/capture measurements, measure
      atomic 1,000-row legacy publication, regenerate schemas and acceptance matrix,
      and pass full native acceptance plus scripts/verify with the screen unlocked.
      Do not weaken deadlines or targets; preserve all prior evidence.
  - Deliver: maintained native benchmark mode, release-build acceptance reports,
    full state screenshots, and a parity/failure/performance matrix against
    `docs/NATIVE_RECORDING_UX.md` sections 4–13.
  - Verify: `scripts/benchmark-recording --synthetic --backend native --profile acceptance`;
    `scripts/verify-native-recording --suite acceptance --benchmark-report <new-report> --output <new-empty-root>`; `scripts/verify`.
    Exercise 0/4/36/1,000 historical takes, varied durations through ten minutes,
    100 rapid cycles, and competing QC/import/export. Report p50/p95/p99 and memory,
    exact frame evidence, stable geometry, and zero historical inspections on
    interactive commands. Compare baseline without redefining metrics.
  - Physical gate: After explicit initiation of a bounded diagnostic, verify
    actual permission/denial, UMIK-1 identity, startup/Stop, quiet/spoken waveform,
    continuous capture, headphone playback, rapid use, device/sleep recovery,
    safe reopen, and disposable full-package intake. Record real outcomes under
    the private root. A synthetic pass cannot check this gate off.
    **Operator direction 2026-09-13 (manual preview):** After missed timed cues,
    the operator requested independent use of the recording experience. The signed
    physical preview is open on the spoken diagnostic prompt, using only its private
    diagnostic asset root. Microphone access is on; the operator selected speakers.
    No external short-duration watchdog is armed for this manual exploration;
    production recording bounds are unchanged. This is not completed physical
    acceptance or the separately confirmed configured-root installation/handoff.
    Preserve the open preview for operator use; do not resume timed physical steps
    without new coordination. Evidence: `physical-20260914T013126Z-i649qel_/manual-preview-ready-01.md`
    under the private defect-b root. Step 16 remains unchecked; Step 17 is excluded.
    **Manual recording review 2026-09-13:** The operator made three takes and
    reported that playback sounded alright. Read-only WAV, digest, provenance,
    device/helper identity and continuity checks passed for all three, with no
    clipping or detected dropouts. The 2.624s draft and 6.037s saved spoken take
    have only low-peak warnings; the latter is 0.196dB below the unchanged -35dBFS
    cutoff. The 45.888s saved continuous take passes QC. Both saves are durable.
    Evidence: `physical-20260914T013126Z-i649qel_/manual-recording-check-01/report.md`
    and its hash-linked JSON reports under the private defect-b root. This adds
    basic capture/Stop/Save evidence, not the formal 60s, controlled playback,
    latency, recovery or intake gates. No audio or implementation was changed;
    the preview remains available and Step 16 remains unchecked.
  - If any target fails: profile, fix, and rerun the affected checks. Amend the
    design visibly if a real platform limit requires a changed target or device
    strategy. Do not hide missed latency by displaying fabricated sample progress.

- [ ] **Step 17: Cut over and remove temporary desktop paths.**
  - Deliver: native app as the primary documented desktop workflow; safe
    installation/relaunch procedure; explicit retirement of Qt-only desktop
    modules/tests/dependency and temporary rollout routing once parity passes.
    Preserve the frozen previous release as a rollback artifact, not a second
    actively evolving UI. Keep supported historical readers and CLI contracts.
  - Migrate supported capture callers to the canonical native helper before
    deleting duplicated Python ctypes capture code. Preserve test intent for
    continuity, interruption, exact media clock, and process control. If a CLI
    caller still needs an adapter, finish that adapter before retiring its engine.
  - Update: `docs/DESKTOP.md`, `docs/RECORDING_EDITOR_UX.md`, `CURRENT.md`,
    `README.md`, `TESTING.md`, dependencies, build/bootstrap scripts, and affected
    decision records. Record exactly which prior UX decisions are superseded.
  - Verify: `scripts/verify`; `git diff --check`; package smoke and disposable
    rollback replay. Audit references/imports and migration manifest cleanup.
    Final action: deliver the accepted app artifact and installation result,
    preserving recordings and the old app backup. Do not close an active recording
    or overwrite the live installation to manufacture a completed checklist.

## Done criteria

- Every step has recorded verification evidence; native physical acceptance is
  explicitly completed, not inferred from mocks or an earlier recorder's success.
- Record, Stop, and Save & Next meet the published latency gates; the waveform
  follows real frames, layout stays fixed, and background jobs do not block Stop.
- One-off recording works without session management; Save & Next skips existing
  room tones and other retained exact prompts, with correct completion/recovery.
- Editing/history/accessibility parity and all package/raw/consent invariants
  hold. No old audio, timestamps, removal state, or accepted references are lost.
- Full `scripts/verify` and native release acceptance pass. The native app is
  delivered with build identity, concise operator instructions, and rollback.
- Only completed behavior is described as shipped. No unrelated changes are
  reverted, no private asset enters Git, and no implicit transfer/model action
  occurs. Keep unfinished work unchecked with a concrete next action.

## Evidence log

Execution started 2026-09-11. Append concise test/report/build references under
the completed step or here as execution proceeds; do not paste private audio,
waveform samples, or recording metadata into this file.

- Preservation preflight: froze the 192 pre-execution source files (including
  uncommitted desktop work), working-tree patch, dependency inventory, and a
  copy of the working Python/Qt environment under
  `~/Library/Application Support/Cole Voice Lab/Releases/20260911-pre-native/`.
  Editable source imports point into the frozen source. This is preparation;
  the independent rollback launcher/cutover replay in step 15 remains pending.
- Toolchain preflight: installed Swift 6.3.1 and SDK 26.4.1 compile a strict
  Swift 6 AppKit/SwiftUI executable with macOS 13.0 deployment metadata. An
  inert offscreen fixture rendered light/dark appearances on macOS 26.6.1.
  Actual macOS 13 execution is untested; no full-Xcode/XCUI claim is made.
  Scratch evidence: `/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-preflight-bgccsb08/`.
  Step 3 still requires its maintained package and conformance harness.
- Step 1 focused evidence: 20 benchmark tests pass; benchmark source Ruff
  format/lint checks pass. `scripts/benchmark-recording --synthetic --backend
  legacy --profile smoke` passed using 0/4 historical ten-second synthetic takes.
  Four-history seconds/inspection counts: current 2.309/8; rows+sessions
  6.846/24; Record return 7.706/24; poll 2.412/8; Stop review 4.885/17;
  Save+refresh+next selection 22.280/95 (950 aggregate audio-seconds inspected).
  Report and per-command traces:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-recording-benchmark-1ftr6mif/`.
  These single-run controller/model measurements overlapped verification;
  they are not isolated tail-latency, GUI, or hardware acceptance claims.
  Pre-change full verification passed 1,173 tests/91.45% in 391.44 seconds:
  `/tmp/cole-native-execution/baseline-verify.log`. Full increment verification
  passed 1,193 tests, strict types/lint, build, schemas and private-asset guard
  in 233.47 seconds: `/tmp/cole-native-execution/step01-verify.log`.
  Its reported 90.19% coverage included a concurrent scratch contract run;
  that mixed coverage number is not accepted as increment evidence. An isolated
  rerun passed all 1,193 tests with 91.45% branch coverage, strict types/lint,
  package build, schemas, and private-asset guard in 238.56 seconds:
  `/tmp/cole-native-execution/step01-isolated-verify.log` with dedicated
  `/tmp/cole-native-execution/step01.coverage`. Step 1 is complete.

**Amended 2026-09-11 (verification isolation):** Concurrent scratch coverage used
the repository's default `.coverage` file. All execution checks now use distinct
`COVERAGE_FILE` paths and scratch checks run from their own directories. This
changes measurement isolation only; acceptance thresholds and scope are unchanged.

- Step 2 focused evidence: strict evidence/process models and 14 generated
  schemas integrated. Contracts plus existing schema tests pass 160 cases;
  schema drift, Ruff, and strict mypy checks pass. Fixtures are generated only
  in resolved private directories outside Git. Python/Swift conformance exposed
  and resolved a revision-domain mismatch: both use signed 64-bit integers,
  rejecting boolean/float tokens and overflow. Full increment verification
  passed all 1,345 tests with 91.54% branch coverage, strict types/lint, package
  build, schemas and private-asset guard in 360.11 seconds:
  `/tmp/cole-native-execution/step02-verify.log` with isolated
  `/tmp/cole-native-execution/step02.coverage`. Step 2 is complete.

**Amended 2026-09-11 (receipt binding):** The native controller needs the exact
reservation digest when sealing, but the initial Start DTO only carried the
reservation object. Add its required Python-computed/validated digest to the
unreleased command contract; Swift treats it as an opaque bound identity.
This preserves Python's canonical-serialization ownership. Clarify the existing
step 5/6 dependency: real helper capture is exercised in step 5, and successful
durable service registration is proved when its storage backend arrives in step 6.
No provisional successful registration substitutes for durable evidence.

- Step 3 focused evidence: the installed CLT builds the native SwiftPM package
  with Swift 6 strict concurrency. Product executables declare macOS 13.0.
  The CLT lacks XCTest; its installed Swift Testing framework is used in a
  separate test build and requires macOS 14 (documented test-runtime limit).
  `--suite contracts` passes 11 Swift tests, 88 shared fixtures, 34 additional
  nested-field rejection cases, and 124 Swift encodings checked by Python.
  Three canonical-digest cases remain explicitly Python-owned and are checked
  there; oversized integer parsing is rejected before re-encoding.
  Artifacts: `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-e659um2a/`.
  `--suite shell` passes its C ABI test and 448 actual hosted geometry cases,
  rendering 32 private synthetic light/dark snapshots at both sizes/scales.
  Representative snapshots inspected; measured transport-region movement is
  0.0 points. Artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-cfvpbj9j/`.
  The shell remains inert. Native verification is now part of `scripts/verify`;
  the integrated run passed 1,348 Python tests with 91.55% branch coverage,
  both native suites, strict types/lint, package build, schemas, and asset guard:
  `/tmp/cole-native-execution/step03-verify.log` (Python tests 379.27 seconds).
  This run also verifies the amended Start reservation digest contract. Step 3
  is complete; actual macOS 13 execution and microphone behavior remain untested.

**Amended 2026-09-11 (effective deployment floor):** Read-only `xcrun vtool
-show-build .venv/bin/python` reports minimum macOS 26.0 and SDK 26.5; Python
sysconfig also reports deployment target 26. The only discovered local Python
3.12 installation is that Homebrew 3.12.14 build. Native executables retain
their proved 13.0 target, but the local preview must truthfully declare 26.0
while using this service environment. Step 15 must derive bundle metadata from
all executable requirements rather than copying the old launcher's 13.0 claim.
No dependency download, installed-app change, or actual older-OS test occurred.

Planning validation on 2026-09-11: all 17 steps are ordered, unchecked, and have
Verify commands; local document links, Markdown fences, and whitespace checks
pass. The existing repository's `scripts/verify` passed all 1,173 tests with
91.45% branch coverage, strict types/lint, package build, schemas, and private
asset guard. Log: `/tmp/cole-voice-lab-native-recording-plan-verify.log`.
This verifies the unchanged application baseline and planning increment; no
native implementation or hardware acceptance step has been completed.

- Step 4 focused evidence: canonical C callback/ring and native controller/writer
  integrated. `--suite capture` passes 12 C suites and 11 Swift lifecycle suites;
  Python independently validates two actual native-written synthetic WAVs and
  recovery receipts for exact frames, format, bytes and SHA-256. Artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-sgbx528k/`.
  `--suite capture-sanitizers` passes the same C suites with address/undefined
  and thread sanitizers, and the Swift lifecycle suites with address and thread
  sanitizers. Artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-7t6dmyk_/`.
  Coverage includes exact PCM/drain, timestamp gaps/overlaps, overflow, maximum
  duration/startup deadlines, independent Stop with blocked writer/telemetry,
  device failure, parent loss, publication/fsync faults, recoverable partials,
  and failing-initializer descriptor lifetime. Review found that Apple's older
  ISO formatter truncates Python microseconds; the shared timestamp boundary
  now preserves microseconds and seals retain the exact requested timestamp.
  Full verification passes native contracts/shell/capture/sanitizers, strict
  Python types/lint, 1,348 Python tests with 91.55% branch coverage (458.69 s),
  package build, schemas and asset guard:
  `/tmp/cole-native-execution/step04-verify.log`, isolated coverage at
  `/tmp/cole-native-execution/step04.coverage`. Step 4 is complete. These
  synthetic checks do not establish microphone behavior or release latency.
  Preservation audit: all 192 pre-execution source files remain present; only
  the execution plan, design notes, verification script and schema registration
  have changed among them. The frozen rollback source remains separate.

- Step 5 focused evidence: 60 Python service tests and strict mypy/Ruff pass.
  `--suite bridge` passes five Swift boundary tests and actual native-client,
  helper and Python-service subprocess scenarios. A 4,800-frame capture retains
  exact PCM, digest and reservation microseconds; duplicate Start/Stop coalesce.
  Killing the service leaves direct recorder Stop operational; restarted service
  snapshots use a new generation. An unread telemetry pipe preserves 144,000
  exact frames. Python/native capture ownership rejects competition in both
  directions at the established Qt lock inode. Parent EOF preserves partial
  evidence and exits (18.89 ms in this focused run). Split, oversized, truncated,
  mismatched-version/build and stale-generation messages are covered. Discovery
  now rejects an empty device list before accessing an empty native buffer.
  Integrated artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-89rtx6xs/`.
  Full verification passes the native suites, 1,408 Python tests with 91.58%
  branch coverage (400.89 s), strict types/lint, package build, schemas and
  private-asset guard:
  `/tmp/cole-native-execution/step05-verify.log`, isolated coverage at
  `/tmp/cole-native-execution/step05.coverage`. Step 5 is complete. The service
  foundation explicitly rejects seal registration until step 6 supplies durable
  storage; this is not a saved-take or hardware/performance acceptance claim.

- Step 6 focused evidence: 135 take/path/canonical tests pass, including 103
  take-store cases. Immutable intent/operation/head ordering, synced replay,
  projection failure after the store head advances, fsync/rename faults,
  prepared pilot gates, independent groups and preserved drafts are covered.
  The recovery marker is cleared and synced only after projection succeeds.
  Python/Swift conformance passes 93 shared fixtures plus 35 nested unknown-field
  cases; 131 Swift encodings are revalidated by Python (four digest cases remain
  explicitly Python-owned). Conformance artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-8imsmzoe/`.
  The integrated real-process bridge obtains its reservation and canonical
  digest from the live service, captures 4,800 exact synthetic frames through
  the native helper, survives service death while retaining direct Stop, and
  registers the seal durably after restart. Reopening the store proves an
  unkept draft with pending analysis; a Stop never becomes a keep decision.
  Existing process/ownership/blocked-telemetry failures also pass. Artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-rewiqxtv/`.
  Full increment verification passes native suites, 1,517 Python tests with
  91.66% branch coverage (497.20 s), strict types/lint, package build, schemas
  and asset guard:
  `/tmp/cole-native-execution/step06-verify.log`, isolated coverage at
  `/tmp/cole-native-execution/step06.coverage`. Step 6 is complete. Indexed
  navigation, independent analysis and the active native workspace remain
  subsequent steps; no new-format draft has been promoted to accepted raw.

- Step 7 focused evidence: 131 indexed/legacy-library tests and 183
  contract/schema tests pass. Bounded take/prompt/group pages, coherent counts,
  exact-prompt coverage, Unknown history, source changes with preserved mtime,
  private SQLite paths/sidecars, reversible schema, native incremental updates,
  and legacy drafts/rejections/removals/interrupted/finalized/raw aliases are
  covered. Historical audio validation is spooled before projection transactions.
  Shared Swift/Python conformance passes 107 fixtures plus 38 nested unknown-field
  cases and 148 Swift encodings revalidated by Python; four canonical-digest
  cases remain explicitly Python-owned. Artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-2xwdlgb9/`.
  Installed-wheel verification proves the packaged SQL cannot be shadowed by a
  neighboring checkout; missing resources fail closed and SQL changes alter the
  service source fingerprint. Evidence:
  `/tmp/cole-native-index-step7/installed-wheel-evidence.json`.
  Isolated component measurements at 1,000 retakes/fixed 36 prompts: p95 history
  19.0 ms, next-missing 1.31 ms, postcommit projection 2.25 ms, warm index open
  83.0 ms; zero historical WAV opens; paired resident increase 5,488,640 bytes
  including fixture/import overhead. These are component measurements, not
  end-to-end Save/display or hardware acceptance. Detailed report:
  `/tmp/cole-native-index-step7/index-performance.json`.
  Full verification passes 1,584 Python tests with 91.68% coverage with branch
  tracking (527.86 s), all available native suites, strict lint/types, package
  build, schemas and asset guard:
  `/tmp/cole-native-execution/step07-verify.log`, isolated coverage at
  `/tmp/cole-native-execution/step07.coverage`. Step 7 is complete.
  Preservation recheck: all 192 pre-execution source files remain present; the
  frozen source hashes and 6,198 frozen environment entries match their records:
  `/tmp/cole-native-execution/step07-preservation.json`.

**Amended 2026-09-11 (service bootstrap integration):** Exercising the actual
native workspace exposed two missing connections, rather than a change in UX:
there must be a typed automatic-context opening command, and history discovery
must run without tests manually declaring the index complete. Step 9 now
explicitly wires both. Discovery runs outside the control process; native
metadata churn does not invalidate the separate legacy checkpoint. Unknown
history remains explicit until authoritative reconciliation succeeds.

- Step 8 focused evidence: 324 integrated analysis/audio/service/index/take tests
  pass (50.41 s); strict mypy and Ruff pass. The worker runs independently with
  one in-flight request, a bounded framed protocol, durable resumable tickets,
  three-new-to-one-historical fairness, and bounded retry attempts. Results bind
  original/seal/build/edit/context identity and stable file metadata including
  ctime; stale results never alter another take. The seal-to-job crash gap is
  reconciled on worker startup. Actual worker/process failure, preserved-mtime
  tampering, blocked QC, queue rebuild, fairness and retry cases pass.
  Component evidence: `/tmp/cole-native-analysis-step8/actual-service-worker-evidence.json`
  records an 88.816 ms durable Save while the owned worker was blocked (one
  functional sample, not an acceptance percentile). The 250-take paged backlog
  retained 399,239 traced bytes and opened zero historical WAVs:
  `/tmp/cole-native-analysis-step8/queue-memory-evidence.json`. Installed-wheel
  SQL/resource/source-identity checks pass in that scratch directory.
  The bridge's earlier requirement that QC remain pending was obsolete once
  analysis became independent. It now requires an unkept draft and a matching
  durable running/completed publication if QC has advanced; completed results
  must retain their independently bound source identity. Integrated real bridge
  and failure scenarios pass, including direct Stop with service death.
  Full verification passes 1,652 Python tests with 91.78% coverage with branch
  tracking (318.67 s), native contracts/shell/capture/sanitizers/bridge, strict
  lint/types, package build, schemas and asset guard:
  `/tmp/cole-native-execution/step08-verify.log`, isolated coverage at
  `/tmp/cole-native-execution/step08.coverage`. Native artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-2p62yvj6/`.
  Step 8 is complete. Full release latency and physical recording gates remain.

**Amended 2026-09-11 (background metadata contention):** Step 9 integrated
restart verification exposed a second-reader audit racing the analysis commit.
The final audit now waits for orderly shutdown of its owned service; the earlier
SIGKILL/lost-reply case remains unchanged. Review also found that a background
page reader could hold the metadata flock against Save, and that ordinary lock
contention could be reported as corrupt history. Step 9 now requires lock-free
atomic head reads for projection, before/after pending-commit guards, bounded
background retries with explicit deferred state, and index-aware interrupted
commit repair. Add deterministic contention checks before accepting this step.
This refines the planned separation between background history and controls;
no latency target or integrity boundary is weakened.

- Step 9 focused evidence: 281 navigation/service/discovery/contention/take/index
  tests pass (87.17 s), with strict types and Ruff. Durable Save and its final
  cursor share one operation; exact replay survives a lost acknowledgment and
  SIGKILL without rewinding selection. Actual two-launch discovery reaches
  complete coverage and opens zero historical WAVs after restart. Projection
  readers use immutable metadata without holding the Save lock, reject pending
  transactions, and defer bounded background contention. A deliberately paused
  page read permits a real Save to complete. Interrupted commits repair their
  SQLite projection before clearing the pending marker.
  Full verification passes 1,730 Python tests with 91.62% coverage with branch
  tracking (613.02 s), all native suites, lint/types, package build, schemas and
  asset guard: `/tmp/cole-native-execution/step09-verify.log`; isolated coverage
  `/tmp/cole-native-execution/step09.coverage`. Focused log:
  `/tmp/cole-native-execution/step09-focused-fixed.log`. Native artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-2_szfp1i/`.
  These are functional and integrity checks; release latency acceptance remains
  Step 16. Step 9 is complete.

- Step 10 focused evidence: 208 Python contract cases pass (1.13 s). The native
  waveform suite passes strict Swift compilation, 21 capture/waveform tests in
  debug/AddressSanitizer/ThreadSanitizer, 12 C cases under both sanitizer variants,
  independent WAV/seal validation, and 40 actual Canvas PNGs. Two owned helper
  processes each turn a 9-byte synthetic fixture into exactly 4,801 frames; both
  stored-cache and injected-cache-failure events preserve the same valid seal.
  Current-frame clocks, partial-bucket peaks, stale generation guards, final
  written counts, 256 KiB frames and the two-job archive lease cap are checked.
  Focused artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-750n5ido/`;
  log `/tmp/cole-native-execution/step10-waveform.log`.
  The accelerated 28,800,000-frame cache run took 7.618 s, append p95 1.599 ms;
  extrema allocation 7,200,032 bytes, resident growth 5,029,888 bytes, Canvas draw
  p95 2.698 ms. These component measurements under observed host contention do
  not measure physical display cadence or captured-sample-to-visible age.
  Full `scripts/verify` passes 1,750 Python tests / 91.64% coverage with branch
  tracking (631.76 s), all native suites, strict lint/types, build, schemas and
  asset guard: `/tmp/cole-native-execution/step10-verify.log`, isolated coverage
  `/tmp/cole-native-execution/step10.coverage`. Native full-gate artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-wnh5p0zu/`.
  Step 10 is complete. The separately discovered derived-cache payload checksum
  hardening is being implemented with selected-source review in Step 12.

- Step 11 focused evidence (2026-09-12): fixed workspace passes five Swift tests,
  448 hosted geometry cases within one logical point, and 32 light/dark PNGs at
  both target sizes/scales/reading sizes/Focus choices. Artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-u3xcerd4/`.
  The small dark recording/silence snapshot was visually inspected; fixed regions
  and the persistent red Stop are clear. Actual ordinary and delayed transport
  both pass with 4,801 synthetic frames, 208 paged prompts, startup cancellation,
  direct Stop, coherent draft history, one duplicate-click Save operation, final
  cursor-only advance, preserved focus/scroll and stale QC guards. Artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-o1iq7nzm/`;
  log `/tmp/cole-native-execution/step11-transport-fixed.log`.
  The first delayed fixture omitted packaged SQL when copying the source service
  outside its editable checkout. The fixture now materializes the exact canonical
  migration resources before fingerprinting its isolated runtime; no production
  fallback or integrity check was weakened.
  Full verification passes 1,750 Python tests / 91.63% coverage under branch
  tracking (527.99 s), all native suites, lint/types, build, schemas and asset
  guard: `/tmp/cole-native-execution/step11-verify.log`, isolated coverage
  `/tmp/cole-native-execution/step11.coverage`. Full native artifacts:
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-8c4ou0rv/`.
  Step 11 is complete. All captures are synthetic; no microphone, audible output,
  installed-app launch/restart or transfer occurred.

**Amended 2026-09-12 (derived waveform cache integrity):** Selected-source review
found that the original archive checked its source identity and extrema bounds,
but a plausible corrupted extrema value could still render. Step 12 adds a
checksum over the archive header and extrema payload, with an unreleased cache
format revision and an explicit rebuild miss for older caches. Original audio
and successful capture seals are unchanged. Add an exact plausible-byte corruption
regression before accepting review; this does not relax waveform or latency gates.

- Step 12 focused evidence (2026-09-12): the frozen native review patch (42 paths,
  2,961 added lines) was applied onto the already-integrated Step 12 Python
  contracts/service; every one of its 41 archived source identities matched its
  recorded SHA-256 after integration, and generated schemas were already in sync
  before and after. `scripts/verify-native-recording --suite review` passes 41
  Swift tests plus the actual helper C-ring capture and the actual Python service
  review/edit/decision owned-child flows for native, staged-legacy and
  accepted-raw sources: 4,801 original frames, exactly 2,000 offline-rendered
  retained frames, canonical edit revision 2, kept decision revision 5, original
  SHA unchanged. Artifacts
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t/review`.
  `--suite accessibility` passes the same 41 tests plus a genuine light/dark
  accessibility tree of 84 nodes per appearance, with measured supporting-text
  contrast 6.7076:1 light and 8.3673:1 dark against primary 14.9384:1 and
  12.2258:1, Reduce Motion honoured, and both `activated_application` and
  `voiceover_announcements_posted` false: `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t/review` (accessibility shares the review fixture).
  `--suite review-sanitizers` passes 41 address-sanitized and 41 thread-sanitized
  tests with no findings: `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t/capture-address` and `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t/capture-thread`.
  `--suite workspace` keeps 448 geometry cases and 32 snapshots within one logical
  point (`/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t/workspace`) and `--suite transport`
  passes ordinary and deliberately delayed service flows
  (`/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t/transport`).
  `.venv/bin/pytest tests/test_recording_edits.py tests/test_recording_takes.py`
  passes 140 tests. Full `scripts/verify` passes 1,858 Python tests with 90.77%
  branch-tracked coverage (303.32 s), all native suites, strict lint and types,
  package build, schema check and the private-asset guard:
  `/tmp/cole-native-execution/step12-verify.log`, isolated coverage
  `/tmp/cole-native-execution/step12.coverage`, native full-gate artifacts
  `.../cole-native-recording-verification-tkuxresf`. All audio is synthetic; no
  microphone, audible output, transfer, or installed-app launch/restart occurred.
  Step 12 is complete.
  Recorded for later steps rather than claimed as acceptance: total coverage fell
  from 91.63% at Step 11 to 90.77% because the Step 12 Python modules are the
  least covered in the tree (`recording_review.py` 75%, `recording_edit_jobs.py`
  76%, `recording_edits.py` 86%), and their uncovered lines are predominantly
  fail-closed `SecurityError`/`ContractError` branches. The gate floor is 90%, so
  this passes with a thin margin that later steps should widen, not consume.

**Amended 2026-09-12 (Step 12 integration corrections):** Integrating the frozen
native patch required corrections beyond the archived scope. Each is verified, and
each defect was demonstrated by mutation before the fix was accepted.
1. `apps/macos/Tests/RecordingCoreTests/NativeCaptureTests.swift` counted
   process-wide file descriptors, which races with the newly added parallel
   review/playback suites in the same test process; the same filter produced
   deltas of +1 and -2 across runs. It now counts only descriptors resolving to
   the fixture asset root or below, which is exactly the set `NativeCaptureWriter`
   opens. Dropping one `close` in the writer's directory walk makes it fail with
   30/50/53 leaked descriptors against a 0 baseline.
2. `scripts/verify_native_waveform.py` still asserted the v1 derived cache
   (`CVLWAV01`, `<source>-v1.cvlwave`), so the repository waveform suite failed
   against the CVLWAV02 amendment. It now asserts the v2 magic, the
   3,600,084-byte bound, and recomputes the trailing SHA-256 over the header and
   extrema payload, so the Python gate checks the amendment rather than a name.
3. The four delivered scripts were formatted at the scratch line length and are
   reformatted to the repository's 100-column Ruff configuration, with one
   over-long docstring line rewrapped. No behaviour changed; strict mypy passes.
4. The playback `.failed` event had only negative coverage: the one injected event
   carried a superseded generation and was asserted to be ignored, so deleting the
   handler kept every suite green while the step advertises playback failures as
   verified. A positive regression now delivers a current-generation failure and
   requires `RECORDING.PLAYBACK_FAILED`, a refused transport and refused capture
   until explicit recovery; removing the handler fails it with six expectations.
5. `RecordingPlaybackController.shutdown()` published no gate, so `canPlay`
   remained true for the whole output shutdown and a key or menu action delivered
   during Close or Quit could start audio that outlived them. Operations that
   start output now refuse while a close is in flight; operations that only stop
   output stay available, because the review controller legitimately drains a trim
   preview while `saveTrim` shuts the output down. Removing the gate fails the new
   regression on all three refusals.
6. The single assertion on the real AVFoundation player clock was vacuous: it was
   skipped entirely when `position()` returned nil and otherwise asserted exactly
   the bounds the implementation clamps to. It now probes the playhead at every
   render boundary and requires 635, 1147, 1659 and 2123 source frames; the three
   interior probes cannot be satisfied by a clamp. A frozen clock yields
   123, 123, 123, 123 and fails.
Two further review candidates were adjudicated and rejected: the CVLWAV02 decoder
does mix content-relative header validation with absolute `Data` offsets and would
trap on a non-zero-`startIndex` slice, but no caller in the tree can produce one,
so it is recorded as a latent robustness gap rather than changed here; and
`RecordingSemanticValidator` validates correlated transport-DTO invariants at the
trust boundary rather than duplicating Python's canonical edit rules.

- Step 13 focused evidence (2026-09-12): the frozen Step 13 Python work integrated
  cleanly — four new modules/tests copied at their recorded identities and ten
  patches applied over the current root — and generated thirteen package schemas
  that `python -m cole_voice_lab.schema --check` accepts. The explicit native
  Prepare for lab flow and the `package-preparation` verifier suite did not exist
  and were implemented in this step:
  `RecordingServiceClient.preparePackage` (submit-only), `RecordingPackagePreparation`
  candidate/status model with idempotent `applyPackageEvent`, `RecordingPreparePackageView`,
  a mounted always-visible footer control, `RecordingPackageTests`,
  `RecordingPackagePreparationHarness`, `scripts/verify_native_packages.py`, and the
  fixture/verifier wiring.
  `scripts/verify-native-recording --suite package-preparation` passes six Swift
  model tests plus the actual four-process flow: four real helper captures, four
  independent analysis verifications, and one package worker. Observed service
  states are `queued → running → blocked → queued → running → prepared` over two
  attempts. The refused selection is explained by the service itself
  (`RECORDING.GATE_NOT_READY`, "select at least one utterance as well as room
  tone") while Save availability, prompt selection, the cursor and Record are
  byte-identical before and after, and the fixed regions still match their
  reserved geometry. A one-off saved before any room tone exists stays saved and
  reviewable and is given its missing-prerequisite explanation in the Prepare flow.
  Resubmitting the same durable operation returns the identical session and
  manifest rather than a second package. Artifacts
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t/package-preparation`.
  The independent Python post-check re-derives everything from durable evidence:
  the package validates through `validate_capture_package` with `qc_status` pass,
  the take map re-reads through `load_recording_package_map` against its own
  immutable registration operation, member paths are re-derived from the selected
  suite with `expected_recording_paths`, every member's bytes equal the
  recomputed fixture signal, originals and members have distinct inodes and one
  link each, READY is newer than every member, no staging directory survived, all
  four package aliases resolve back to their exact kept takes, and a foreign
  session id truthfully returns no map.
  `.venv/bin/pytest` over the step's six named modules passes 204 tests.
  Full `scripts/verify` passes 1,936 Python tests with 90.62% branch-tracked
  coverage (567.39 s), all native suites including the new one, strict lint and
  types, package build, schema check and the private-asset guard:
  `/tmp/cole-native-execution/step13-verify.log`, isolated coverage
  `/tmp/cole-native-execution/step13.coverage`, native full-gate artifacts
  `.../cole-native-recording-verification-mn7_33v4`. All audio is synthetic; no
  microphone, audible output, transfer, or installed-app launch/restart occurred.
  Step 13 is complete.

**Amended 2026-09-12 (Step 13 verification scope and fixture signal):** Three
corrections, each proven before it was accepted.
1. The step's Verify line named four pytest modules, but the only test of a
   genuine pre-existing non-native v1 package's session-only history lives in
   `tests/test_recording_library.py`, and the new queue/worker contract lives in
   `tests/test_recording_package_jobs.py`. Both are now named, so "unchanged
   legacy packages" is actually run rather than nominally covered.
2. The native fixture's three-sample synthetic signal is two thirds full scale,
   so the canonical inspector counted 3,200 clipped samples and every fixture
   take stopped at `needs-review`. No native take could reach independent
   verification, which the package gate requires, and no existing suite noticed
   because none waited for verification. The package suite records one exact
   480-frame sine period instead, measured at peak -12.45 dBFS with zero clipped
   samples, and its harness now waits for actual verification. The transport and
   review suites keep their original pattern and their exact-byte evidence.
3. `PreparePackageCommand` requires at least one declared room tone, so a package
   cannot be requested without one and an empty declaration fails as an invalid
   message instead of an explanation. The flow therefore requires a declared room
   tone as a message-shape check and explains a missing or unrecorded room tone in
   the Prepare flow itself, while any selection the service refuses is reported in
   the service's own words. Nothing about this weakens Save: a context-incomplete
   one-off still saves, reviews and plays.
Also corrected during integration: the native semantic validator accepted a
`package-preparation` event that claimed `queued` while carrying an error, which
the newly generated conformance fixtures caught as an accepted invalid case. The
validator now binds `blocked`/`failed` to a named prerequisite. A second candidate
rule mirroring the selected-suite purpose and content binding was written, tested,
and then removed: the suite still passed without it because the existing package
entry/catalog rule already rejects both negatives, so it would have duplicated a
Python-owned rule without adding verification.

**Amended 2026-09-12 (retained evidence directories):** While clearing space during
Step 14 the older per-suite verification roots were pruned by age, which removed
directories the Step 12 and Step 13 entries cited. Every recorded count and JSON
field from those runs remains in this log, and the suites are deterministic
synthetic fixtures, so nothing became unverifiable — but the citations no longer
resolved. Those entries now cite the per-suite subdirectories of a single full-gate
artifact root, which every `scripts/verify` run regenerates and which does not
depend on standalone runs being kept. No repository content, recording, or
provenance was involved; only external synthetic fixture roots were removed.

- Step 14 partial evidence (2026-09-12): the frozen Step 14 Python work is merged
  surgically. The clean ownership patch applied, three reviewed modules were copied
  at their recorded identities, and the Step-10-based wire patch plus the reviewed
  integration patch were applied with `--reject` so that all twelve rejected hunks
  could be hand-merged onto the later Step 12/13 branches: the response union keeps
  its package members beside `RecoveryEvent`, `_event_take_id` and the Swift
  response-event switch keep their `derivative` case, the preparation lane keeps
  `ReviewTakeCommand` while gaining `ContinueLegacyRecordingCommand`/`RecoverCommand`,
  and the contracts fixture generators keep their review and package cases. No
  `.rej` file remains.
  Built in this step: the native recovery surface (`RecordingRecovery.swift` rows and
  status, an explicit bounded page sweep at the end of `connect()`, a single-take
  re-check, the incomplete-record sheet, and the reserved status line),
  `RecordingServiceClient.recover`, five `RecordingRecoveryTests` model tests, the
  `recovery` verifier suite with its `recovery-crash`, `recovery-restart` and
  `recovery-busy` harness subcommands, `scripts/verify_native_packages.py`'s
  counterpart `scripts/verify_native_recovery.py`, a live-owner holder process, and
  `voice-lab recording continue-legacy`.
  `scripts/verify-native-recording --suite recovery` passes on the real four-process
  chain. A genuine capture fails at the writer's own final-header commit
  (`RECORDING.WRITE_FAILED`) and is never savable. A restarted session in a changed
  acoustic setup surfaces that capture as an explicit `partial` carrying the
  service's own message, opens its own group while the previous group keeps its
  recorded device, re-checks idempotently, and then records, seals and saves a new
  take that retains its exact prompt. Sleep ends an open capture without sealing it
  and nothing resumes on its own. While a second process genuinely holds capture
  ownership the page reports `busy`, and once that owner exits the same take resolves
  to `partial`, so `busy` describes the owner rather than the recording.
  The independent post-check re-derives all of it: no seal, no final original, the
  native receipt re-validated through the same bounded private reader recovery uses,
  an independently constructed `RecordingRecovery` returning the same
  `partial`/`RECORDING.WRITE_FAILED` with no claimed recovered seal, unique page
  identities across three takes, and a post-recovery original equal to the recomputed
  fixture signal at exactly 4,801 frames.
  `.venv/bin/pytest tests/test_recording_recovery.py tests/test_recording_index.py`
  passes 82 tests. Full `scripts/verify` passes 2,011 Python tests with 90.74%
  branch-tracked coverage (307.01 s), every native suite, strict lint and types,
  package build, schema check and the private-asset guard:
  `/tmp/cole-native-execution/step14-verify2.log`, isolated coverage
  `/tmp/cole-native-execution/step14b.coverage`, artifacts
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-oismv69t`.
  All audio is synthetic; no microphone, audible output, transfer, or installed-app
  launch/restart occurred.
  **Step 14 is deliberately left unchecked.** Its Deliver bullet asks for the design's
  storage/resource diagnostics, and the pending-derivative budget from section 10 of
  the design is not implemented: `NativeCaptureStorage.requireAvailable` still counts
  only the maximum take plus a 256 MiB reserve, with no term for queued derivative
  work. Implementing it honestly means choosing between a new wire field the app
  consumes and Python-side enforcement where the derivative queue already lives, and
  that is a contract decision rather than an execution detail. The available-space
  refusal now names its actual and required amounts, which satisfies the "explain
  available space" half of the same design row.
  Also outstanding, with what does cover them today: a real microphone unplug during
  capture has no synthetic route because the fixture has no device, and is proven only
  by unit tests plus the physical gate in Step 16; and a service process dying between
  a durable Save commit and its reply is covered by two adjacent proofs — Python kills
  a real process across four recovery boundaries and injects faults at seven Save
  boundaries and nine fsync ordinals, and the bridge suite proves the native client
  observes subprocess death and reconnects — rather than by one end-to-end scenario.
  Delayed jobs are covered by the existing delayed transport suite, which blocks seal,
  Save and analysis behind private FIFOs.

**Amended 2026-09-12 (Step 14 integration corrections):** Two defects were found by
running the gate rather than by inspection, and both are fixed.
1. The archived wire patch started the recovery supervisor for every service. That
   broke the bridge suite: after the bridge kills its service and reconnects, startup
   recovery resumed the unsealed take and submitted its analysis before the caller's
   `register-seal` returned, so the registration came back as something other than a
   fresh unkept draft. The supervisor is now scoped to the interactive native
   workspace session. That is also where it belongs: the app performs an explicit
   bounded sweep after bootstrap, which is authoritative because pre-handshake
   supervisor pages are dropped, while a group-scoped takes service owns its own
   registration lifecycle.
2. The recovery fixture's two runs raced on teardown. The first run's service still
   held the analysis-queue owner lock for a moment after its process exited, so the
   second service failed to start and the app reported `RECORDING.PARENT_LOST`
   intermittently. The fixture now waits for both the capture-ownership and
   analysis-queue locks to be released by actually attempting to acquire them, which
   is deterministic where a sleep would have been a guess.
Two further corrections were to this step's own new checks: a `busy` recovery status
carries no error by contract, so asserting one was wrong — the post-check now proves
that the same take resolves to `partial` once the owner exits; and the sleep scenario
originally slept as soon as Stop became available, which caught the capture still
starting up and exercised startup cancellation instead, so it now waits for frames to
be flowing and runs after the ordinary save. The helper's parent-loss exit time was
previously recorded but never bounded; it is now asserted against a named 2.0-second
watchdog deadline and reported with its measurement.

**Amended 2026-09-12 (pending-derivative reserve; supersedes the Step 14 deferral):**
The operator chose Python-side enforcement over a new wire field. `RecordingEditCoordinator`
now refuses to start a render unless free space covers every queued render plus
`DERIVATIVE_RENDER_RESERVE_BYTES`, which is derived as `MAX_RECORDING_FRAMES * 3 +
256 MiB` from the same constant the native writer uses, so a permitted derivative can
never drop free space below what an active capture needs and the two sides cannot drift.
Free space is read with `shutil.disk_usage`, matching the native side and the three
existing Python call sites. Sizes are exact rather than estimated: both job kinds carry
their own frame range, and a full-range legacy edit contributes zero because it publishes
no derivative at all — budgeting it would refuse pure metadata remove/restore/reject on a
full disk. The queued set is summed once, since the job under test is already in it.
A refusal is transient by construction: no `.failure.json` and no legacy `.result.json` is
written, because durable failure evidence would refuse that exact recipe forever and the
app cannot resend an unchanged trim. The refusal defers the take, re-arms discovery after
`LOW_SPACE_RETRY_SECONDS` so it is retried rather than dropped, and announces itself once
per low-space episode as a retryable `RECORDING.DISK_SPACE` naming actual and required
bytes. Two companion corrections were required for the invariant to hold at all: the render
child classified `ENOSPC`/`EDQUOT` as a generic edit failure, which the parent turned into
permanent failure evidence — exactly when the disk was full — so the child now names
`RECORDING.DISK_SPACE` and `_failure` refuses to write durable evidence for that code.
Evidence: seven new cases in `tests/test_recording_edit_disk_budget.py` pass, and each of
the three mechanisms is mutation-verified — removing the discovery re-arm, the queued-work
term, or the once-per-episode limit each fails a distinct test. All six existing coordinator
constructions now inject their free-space probe, following `accept_capture_session`'s
`available_bytes` precedent, so no existing test depends on the machine's free space.
**This does not implement the design's "disable Record under low disk" row.** That gate is
`NativeCaptureStorage.preflight` inside the helper, which still has no pending-derivative
term; closing it needs the wire field that was declined. What is delivered is that a render
can never itself evict capture's space, and that a full disk never burns a recipe.

- Step 14 closing evidence (2026-09-12): every `scripts/verify` component passes except the
  two suites that require a realized window. Strict Ruff format/lint and strict mypy over 54
  source files pass. Ten native suites pass: contracts, shell, capture, bridge, waveform,
  workspace, transport, package-preparation, recovery and review-sanitizers. The Python
  suite passes 2,018 tests with 90.71% branch-tracked coverage (401.96 s);
  `recording_edit_jobs.py` rises to 77% while gaining roughly sixty statements, so the new
  fail-closed branches are exercised rather than merely added. Package build, schema check
  and the private-asset guard pass. Logs under `/tmp/cole-native-execution/g-*.log`, isolated
  coverage `/tmp/cole-native-execution/step14d.coverage`.
  The `review` and `accessibility` suites could not run: this host's screen is locked, so the
  window server will not realize a window and the accessibility tree returns thirty nodes
  with no identifiers at all. That is environmental, not a regression — it reproduces with
  the recovery sheet unmounted, with the status-line change reverted, and with the startup
  recovery sweep removed, and the immediately preceding full gate passed both suites with
  every one of these Swift changes already present (2,011 tests, 90.74%,
  `/tmp/cole-native-execution/step14-verify2.log`). The only change since is Python-side and
  cannot alter a SwiftUI accessibility tree. Re-run `scripts/verify` with the screen unlocked
  to close that loop. Step 14 is complete.

- Step 15 evidence (2026-09-12): the frozen packaging work is integrated — four new
  files copied at their recorded identities (`native_recording_application.py`,
  `scripts/build-native-recording-app`, and two test modules), the three surgical
  runtime/service/schema patches applied over the current Step 13/14 root, and the
  separate native verification-entry patch applied onto the Step 12 Swift source.
  Three application schemas generate and `python -m cole_voice_lab.schema --check`
  is clean.
  `scripts/verify-native-recording --suite packaged-app` passes 32 tests in 88.37 s,
  including an actual signed preview build, its frozen no-Qt service, hardened
  audio-input entitlements on both app and helper, an equal-size preserved-mtime
  dependency replacement rejected by the runtime receipt, and the rollback replay
  run against the real frozen release. The suite passes
  `COLE_VOICE_ROLLBACK_RELEASE` explicitly and records
  `packaged_app_rollback_release_present`, so a skipped rollback can never read as a
  verified one. Artifacts
  `/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-wi8qbbh7`.
  `.venv/bin/pytest tests/test_native_recording_application.py` passes the same 32
  cases. The complete Python gate passes 2,049 tests with 90.35% branch-tracked
  coverage (495.46 s) measuring 19,368 statements and zero foreign files:
  `/tmp/cole-native-execution/h-pytest-final2.log`, isolated coverage
  `/tmp/cole-native-execution/step15f.coverage`. Eleven native suites pass
  (contracts, shell, capture, bridge, waveform, workspace, transport,
  package-preparation, recovery, review-sanitizers, packaged-app), as do strict Ruff
  format/lint, strict mypy over 55 source files, the package build, the schema check
  and the private-asset guard.
  Newly built in this step rather than inherited: the `packaged-app` suite itself, the
  configured-app launch test, the negative launch cases, and the packaging/rollback
  documentation. The launch test is the step's only permitted launch — a synthetic
  fixture app, headless and self-exiting — and it asserts a genuinely useful state
  (context ready, prompt selected, Record enabled, channels closed) while
  `shown_window`, `activated_application`, `microphone_opened`, `playback_started`,
  `recording_started` and `installed_app_restarted` are all false. Negative cases cover
  incomplete flags, a relative report path, an existing report left byte-identical, a
  non-private report directory, a missing service executable, and a changed peer build
  identity; none of them produces a report claiming success. No microphone, audible
  output, transfer, installation or installed-app restart occurred, and the installed
  Qt launcher is untouched.
  The `review` and `accessibility` suites could not run: this host's screen is locked,
  so the window server will not realize a window and the accessibility tree returns
  thirty nodes with no identifiers. Re-run `scripts/verify` with the screen unlocked to
  close that loop. Step 15 is complete.

**Amended 2026-09-12 (Step 15 integration corrections):** Four defects were found by
running the integration rather than by reading it.
1. The builder failed closed on `src/cole_voice_lab.egg-info`, which `python -m build
   --no-isolation` leaves in the checkout and which `scripts/verify` itself creates, so
   packaging would break for anyone who had run the gate. Build metadata is now excluded
   from the frozen source copy; every other unexpected entry still fails closed.
2. The configured service argv omitted `--subject-id`, so the service defaulted to
   `cole-self` and rejected the configured subject with `RECORDING.STALE_REVISION:
   workspace subject changed`. This would have broken any subject but the default.
   `docs/NATIVE_RECORDING_APPLICATION.md` already documented `--subject-id SUBJECT` as
   the service's base arguments, so the builder simply was not implementing its own
   stated contract.
3. `tests/test_recording_navigation.py` audited every `.wav` open anywhere outside
   `tmp/recording-takes`, as a proxy for "no historical recording inspections". The
   Step 15 service identity deliberately hashes the whole stdlib tree as trusted-base
   evidence, and CPython ships its own `sndhdr.wav` test data, so the proxy failed on a
   Python installation file. The audit is now scoped to the asset root, which is where
   historical recordings actually live; the same test still asserts that the audit does
   fire during legacy backfill, so nothing was weakened.
4. `tests/native_recording_rollback_fixture.py` leaked `COV_CORE_*`/`COVERAGE_*` into the
   frozen pre-native children. With the rollback release named, that enrolled a separate
   program in this repository's coverage denominator: 38 foreign files and about 14,000
   statements at 0%, which dropped the reported total to 55.92% and would have failed the
   gate for an unrelated reason. All four frozen-source child launches now share one
   uninstrumented-environment helper, and the packaging suite reports zero foreign files.
   The release's own frozen venv never leaked because it has no `coverage` installed.
Recorded for Step 16 rather than claimed here: the Step 15 service identity adds roughly
0.33 to 0.42 seconds of environment hashing to each service process startup in
development, matching the archive's own samples. A release avoids that cost through its
runtime receipt, but the figure is directly relevant to the one-second warm useful launch
gate. Separately, `test_actual_service_prepares_in_owned_process_while_save_remains_reachable`
failed once under severe memory pressure with `RECORDING.ANALYSIS_FAILED` and then passed
six consecutive times on a quiet machine; it is recorded as an observed flake, not a fix.

**Amended 2026-09-12 (release integrity closure, now fixed):** An adversarial
review of the Step 15 integration refuted five of six candidate findings; one survived and
its factual core is independently confirmed. `recording_service_build_identity()` hashes
only `*.py` under the service package plus the packaged migration SQL
(`recording_service.py:1203-1213`), and `RECORDING_PYTHON_DISTRIBUTIONS`
(`runtime.py:881`) names only the five Pydantic-family distributions, so the release's own
installed `cole_voice_lab` tree contributes no non-source bytes to any identity. Python
resolves `.cpython-312-darwin.so` before `.py`, so an extension module placed beside a
service module inside a release would shadow it while `service_build`, the runtime receipt
and `verify_native_recording_application` all still match. `release.service_wheel_sha256`
is recorded at `native_recording_application.py:613` and never compared to anything.
This is an incomplete integrity closure rather than a privilege boundary: the expected
identity files are mode-0600 inside the same operator-owned 0700 release tree, and the
bundle is ad-hoc signed, so whoever could add that file could equally regenerate the
manifests. It does not make the current preview unsafe in ordinary use, and it is outside
Step 15's Verify line, which is why Step 15 remains checked. The smallest safe closure is
to add the installed package tree's directory and file stat entries to the runtime receipt
in `_recording_runtime_entries`, guarded to the release layout so development — which has
no receipt — is untouched. Two tempting variants are wrong and should not be used:
widening `recording_service.py:1203` to `rglob("*")` makes a development checkout's live
`__pycache__` race the parent-written manifest and produce spurious build mismatches, and
adding `cole-voice-lab` to `RECORDING_PYTHON_DISTRIBUTIONS` fails for the editable install
because the imported module does not live under the distribution's `locate_file("")`.

The closure is now implemented and mutation-verified. `_recording_runtime_entries` binds
the running service package's directory and file identities into the runtime receipt when
that package sits inside the environment prefix, which is true of an installed release and
false of an editable development checkout, so development keeps hashing current bytes and
is untouched. A new packaging case plants `audio.cpython-312-darwin.so` beside `audio.py`
inside a cloned signed release — changing no `.py` file the build identity digests — and
requires the runtime identity to fail closed with "runtime changed; rebuild". It asserts
against the receipt path rather than an import, because a genuine malicious extension
would load cleanly and only the identity closure can be relied on to reject it. Removing
the binding makes that case fail: the shadowing extension is accepted as verified, which
is exactly the gap. `scripts/verify-native-recording --suite packaged-app` now passes 33
tests in 88.60 s, including a fresh real signed build whose receipt covers the new entries;
strict lint, strict mypy over 55 source files, the schema check and the runtime, runtime
integrity and recording-service suites all pass. The recorded-but-unverified
`release.service_wheel_sha256` remains an observation rather than a defect: the wheel is
consumed during the build and the installed bytes it produced are now themselves bound.

Step 16 is under way and cannot be checked off. The maintained native benchmark mode now
exists, measures the real release three-process stack, and has failed three of the
document's own acceptance rows.

`scripts/benchmark-recording --synthetic --backend native --profile acceptance` builds the
release products with strict concurrency, warnings as errors and no sanitizers, records
exactly what it built, and drives the actual configured client, recorder helper and
recording service through the document's design points. Historical audio attribution is
cross-process: a `sitecustomize` audit hook logs every WAV open under the asset root with
its uptime, pid and process role, and Python's monotonic clock and Swift's
`DispatchTime.uptimeNanoseconds` are the same mach uptime epoch, so an open attributes to
the exact command window it fell inside. Analysis workers are separate processes and their
background reads are excluded by role, which is what the design asks for; the client's own
audio descriptors are compared across each command window so a client-side read cannot hide
behind the service being clean. The audit reports its own positive control: 2,016 of 2,096
audited cross-process opens landed inside a command window, so the zero attributed to the
interactive commands is a measured zero and not a clock that failed to line up.

The report is built so that it cannot claim a gate it did not measure.
`release_acceptance_passed` is computed and never assigned: false while any row is
unmeasured, any sample failed, the build is not release, sanitizers are present, or any
take came up short of the length its design point declares. Every row is judged only at its
own condition — the snapshot row only over a library that actually holds a thousand durable
takes, counted rather than declared, and the sealing row only for takes of at most thirty
seconds, with longer takes reported separately rather than averaged in. Rows that are bounds
rather than quantiles are judged from an observation that names its own measured boundary,
never from the mere presence of a timing sample with a matching name. A group with too few
samples can fail a bound but can never establish one. Six rows need a presented frame, a
device or a person; each is declared unmeasurable with its reason, its owner, and whether
this run even produced the substitute metric it names. Every one of these rules is
mutation-checked.

Measured on release across six completed design points — zero, four and thirty-six
historical takes at one hundred rapid cycles, plus silence, ten-second and thirty-second
takes — four rows pass, three fail, and eight are unmeasured, two of them for want of a
design point that would not run:

- `stop_reaches_controller` passes at p95 0.24 ms against 50 ms, over six powered groups.
- `historical_audio_inspections` passes: zero audio reads attributed to any of 2,520
  interactive command windows.
- `timer_frame_agreement` passes exactly across 360 cycles, and `stable_layout` at 0.0
  points of drift across 102 measurements.
- `recorder_memory_growth` fails at 9.31 GiB against a 64 MiB bound.
- `stop_to_sealed_review` fails at p95 708.8 ms against 300 ms.
- `save_next_durable_acknowledgment` fails at p95 229.7 ms against 200 ms; its p99 of
  262 ms is within the 400 ms bound.

The three failures are one defect, not three. The native client's physical footprint grows
without bound across a session: over forty one-second release cycles it went from 42.3 MiB
warmed idle to 1,487 MiB, and over a hundred cycles it reached 9.31 GiB above warmed idle.
The recorder helper, the service and the analysis workers stay flat throughout, so the
growth is confined to the client, which also holds exactly one take WAV file descriptor per
cycle. Within a single hundred-cycle case the latency tracks that growth directly: as the
client went from 812 MiB to 6,122 MiB, the median stop-to-sealed-review went from 306 ms to
645 ms and the median Save and Next from 120 ms to 166 ms. The two latency rows are
therefore reported as failing under a contaminated condition; neither can be treated as an
independent latency defect until the memory growth is fixed and the benchmark re-run. The
one concrete lead is that `RecordingPlaybackController.shutdown()` stops output but never
releases the selected source's verified file lease or its `AVAudioEngine`; only `close()`
does, and the workspace calls `shutdown()` on save and on navigation. A retained
maximum-sized waveform pyramid was considered and disproved: both client call sites size the
cache to the take's real frame count. The remaining per-cycle megabytes are not yet
attributed to a named allocation. No fix is attempted here, because releasing on
`shutdown()` would clear the selected source and change reviewed behavior that Step 12
already verified; that is a design decision for the operator.

The thousand-take design point does not run at all. Both thousand-take cases died before
completing a single cycle with `RECORDING.CANCELLED: Local process request exceeded its
deadline; channel closed for recovery`, the client's five-second local request deadline,
having audited no WAV open at all, so no audio work was involved. Its two rows — the
metadata snapshot and next-missing lookup at one thousand takes, and the added
indexed-history memory at one thousand takes — are reported as unmeasured rather than
filled in from the smaller libraries that did run.

That failure is attributed. The fixture that first produced it also started the service
with a thousand takes that had never been analysed, which was a fault of the benchmark and
is fixed below, so the result was held back until it could be separated from that fault. A
copy of the same library was then genuinely settled — a real drain of all thousand takes
through the production analysis supervisor, which took 271.6 seconds — and run again with
zero takes awaiting analysis. It fails identically, three times out of three, at 6.8 to 8.0
seconds. The library's own metadata is not what is slow: over that same thousand-take
library, opening the index takes 0.06 s and a two-hundred-row history page 0.02 s. What the
durable evidence shows instead is a discovery record reading `completeness: building`,
`deferred: true`, `native_reconciled: false` — the service's deferred reconciliation of a
thousand native takes is still running when a local request breaches the client's
five-second deadline and the channel closes for recovery. Section 10's own requirement is
the one this breaks: a concurrent background reconciliation job may inspect history, but
those reads must not become a dependency of an interactive command.

Four defects in the benchmark itself were found and fixed while measuring. Laying out the
offscreen hosting view on every cycle put the harness's own SwiftUI work inside the numbers
and stalled a hundred-cycle run at cycle twenty-two; layout is now observed over the opening
cycles and again at the end, and the memory growth was re-measured with it off the hot path
and came out higher, not lower. The per-take wait had no bounded barrier, so a short take
hung for the full barrier instead of being recorded; it now uses a take-length barrier,
records whether the configured length was reached, and any short take makes the acceptance
flag false. The "idle" workload was not idle: saving a take enqueues no analysis, so a drain
that watched the queue found it empty and returned at once, and the competing-workload field
reported a backlog of zero in both workloads; the drain now waits on takes lacking a terminal
analysis state, which is the work the service will actually face. And the snapshot row was
scoped on the history level a case declared rather than the library it built, so a fixture
that quietly fabricated fewer takes would still have been judged as the thousand-take row.

The run was stopped after the thirty-second case rather than running the two- and ten-minute
takes. Those two design points feed no judged row — the document itself reports longer takes
separately, and the only row they could influence already fails by two orders of magnitude —
while the client's own memory growth had taken the machine to 87 MiB of free memory. The
report is re-derived from each completed case's retained harness report and audit log through
the same rules the live run uses, and the design points that never ran are recorded as
failures rather than omitted.

Step 16 evidence. The release acceptance report, its per-case harness reports, cross-process
audit logs and retained fixtures are at
`/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-recording-benchmark-hs2ovddk/`;
the report there is re-derived from those cases and records the two that failed and the two
that were never reached. The native verification artifacts and the generated matrix, in both
JSON and Markdown, are at
`~/Library/Application Support/Cole Voice Lab/Verification/20260912-step16/`, where all
seventeen parity rows verify and twenty-one rows remain outstanding. `scripts/verify` passes
in full: strict format, lint and mypy, every native suite including the new `acceptance`
suite, 2,088 Python tests at 90.32% branch coverage, the wheel build, the schema check and
the private-asset guard.

Step 16 remains open. Three rows fail, eight are unmeasured, and the ten physical-gate items
still require an explicitly initiated bounded diagnostic with the real microphone, which has
not been authorized. The parity, failure and performance matrix is generated by
`scripts/recording_acceptance_matrix.py` and by `scripts/verify-native-recording --suite
acceptance`, which also checks that every state the shell harness records at the canonical
configuration has a rendered file; its `acceptance_closed` field is computed from the
outstanding rows, and the physical gate alone keeps it false.


**Amended 2026-09-12 (operator-ordered defect boundaries):** The operator
authorizes changing playback source-release behavior for defect (a), provided
the fix is mutation-verified and all existing native suites remain green.
This increment stops after that fix and its full verification. Later work is
ordered separately: characterize the large-library deadline with measurements
before proposing changes; present a design for the operator to choose before
implementation; rerun acceptance; perform explicitly initiated bounded physical
diagnostics outside the corpus with presence checks; then a separately named
preview installation only after destination confirmation and the real-library
size gate. Step 17 and replacing/restarting the existing installed launcher
remain unauthorized. No acceptance threshold is amended by this authorization.


**Verified 2026-09-12 (operator boundary 1 only):** Defect (a) is fixed.
The actual cause was Foundation JSON/schema/regex temporaries retained until
EOF by the client's reliable and telemetry reader work items and the helper's
control reader. Per-bounded-read autorelease pools in
`RecordingProcessChannel.swift` and `RecordingHelper.swift` release them.
Retained allocation stacks identify both client paths through the wire decoder
and schema validator. Playback behavior and all resource/deadline bounds are
unchanged. The earlier descriptor claim was a diagnostic error: the benchmark
field is a cumulative union of paths; live snapshots and distinct-source tests
show two descriptors for the current source, not cumulative take retention.

A new release zero-history, 100-cycle, one-second synthetic run measured
29.83 MiB combined client/helper growth against the unchanged 64 MiB bound
(979 footprint samples), all 100 exact 48,000-frame takes and zero historical
audio inspections across 700 command windows. Three independent single-pool
mutations fail the same 64 MiB resource budget; restored tests pass. The
maintained bridge verifier runs resource tests separately from the original
bridge suite. Full `scripts/verify` passes with the screen unlocked: all native
suites, 2,088 Python tests (one general-sweep rollback test skipped; explicitly
exercised by the native packaging stage), 90.33% branch coverage, wheel build,
schema check and private-asset guard. `git diff --check` passes.

Retained evidence, allocation traces, exact build identities, mutation logs
and full verification log (`full-verify-3.log`):
`~/Library/Application Support/Cole Voice Lab/Verification/20260912-defect-a-k0h9vho0/`.
Start with `boundary-1-report.md`. All-native artifacts:
`/private/var/folders/6d/pxd7tb_502dclls7fst4brbh0000gn/T/cole-native-recording-verification-0gm49qz3/`.
Earlier interrupted/failed verifier runs and underpowered mutation probes are
retained, not presented as passes.

Step 16 remains unchecked. This is resource proof at one design point, not full
acceptance: targeted Stop-to-sealed-review p95 still measured 469.53 ms against
300 ms; full latency/profile verdicts and matrix regeneration remain boundary
4. Next is boundary 2 characterization of the large-library opening defect,
with measurements reported before any proposal. No reconciliation fix/design,
physical diagnostic, operator preview installation or Step 17 work was begun.

**Amended 2026-09-13 (measured reconciliation design):** Completed and reported
boundary 2 before product edits. Current plain native opens were 29: 3/3 pass,
30: 2/3 pass, 31: 0/3 pass; older first breach 25 and direct-observer margin
variation prevent claiming a universal deterministic capacity. The 1,000-take
normal-module phase observation took 74.476486126 s; all 1,000 analysis jobs
repeated on the next launch despite unchanged completed discovery checkpoints.
The actual startup Recover path waits on the analysis queue mutex; later
200-head reconciliation holds the take-store lock for seconds. Other index
connections also trigger full-history audits inside subsequent service transactions.

The operator delegated choosing/implementing the design while excluding cutover.
Choose one service index owner, prepared worker publications, fair historical
analysis, replayable background recovery inventory, and explicit plain Save while
coverage is incomplete. Preserve existing pending-journal/projection/ack ordering;
no new outbox or persisted incremental scan resumption in this increment. Preserve
atomic legacy replacement initially and measure its final SQL critical section.
Visible UX amendments belong in sections 5–10 and 12. Steps 16 and 17 remain open;
physical presence, preview destination and separate cutover gates are unchanged.

Evidence and short proposal:
`~/Library/Application Support/Cole Voice Lab/Verification/20260912-defect-b-vch9e1h7/boundary-2-report.md`
and `reconciliation-proposal.md` in the same root. Preexisting uncommitted code
is preserved in `before-reconciliation-fix-code.tar` with a hash manifest.


**Amended 2026-09-14 (preview readiness continuation):** New measured idle helper
CPU and synthetic timer-coalescing findings are bounded Step 16 corrections.
Retain previous verified fixes, all physical recordings and all failed reports.
External build scratch and two full repetitions strengthen evidence without
changing numerical thresholds. No physical gate, installation or cutover is
claimed complete; coordinate each operator-dependent step without short Go cues.

**Amended 2026-09-14 (readiness findings and operator feature requests):** Freeze 09's
full acceptance and physical P01–P09 found four product/harness defects and two
operator UX findings, now listed as Step 16 sub-items with written designs. The
operator requested prompt labels, separate instructions, a label OR filter, a
prominent Continue Missing, System/Light/Dark theme and a physical-test prompt
suite. These go in a separate plan and are executed before the next full
verification. Physical checks resume on the fixed build with the test suite; the
remaining sequence (P10–P14, full acceptance, install, real take) is unchanged.
