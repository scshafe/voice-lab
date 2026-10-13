# Recording v1: the corpus on the PC, the prompt studio, singing held generically

Written 2026-10-12, from the owner's decisions of that day (D-051). The architecture is D-052.
This is the plan: what v1 is, how it is built, in which order, and what only the owner can
do. Progress is logged at the end, as each step lands.

## 1. The owner's decisions (D-051)

- **The reference interface is the Swift app** (Cole Voice Recorder, `apps/macos`,
  docs/NATIVE_RECORDING_UX.md). voice-lab-desktop's prompt studio is rebuilt to its
  layout and behaviour.
- **The speech corpus lives on the PC** and is served over `/api/v1`. That covers the catalog
  and its versions, every take, every decision and trim, and coverage.
- **voice-lab-desktop replaces the Swift app** on macOS. The Swift app stays until spike S-1
  passes on the Mac and the studio reaches parity. Then it is frozen with a tag (D-025).
- **Singing is held generically on the backend.** A singing recording is a recording, whatever
  made it. For now its one source is Voice Memos. After v1 ships, the app gains a **singing
  studio**, similar to the prompt studio but distinct from it.
- **Training:** fine-tuning stays on the Mac and trains on the takes already there. Moving
  fine-tuning to the PC (phase 5's ROCm work) follows v1. New takes recorded into the PC's
  corpus are not trained on until then. That was the owner's default ("go ahead"); the
  alternative was a sync of PC takes back to the Mac.

## 2. What v1 is

1. **The corpus on the PC.**
   - **Catalog:** the PC's corpus store holds `cole-corpus`. Its version 2 is pilot-v2 without
     room tones, then Harvard, then ARCTIC: 1,886 prompts, `catalog_sha256 1ebd48cb…`, the
     same digest as the Mac's (docs/CORPUS_STORE.md). It is active for `cole-self`.
   - **Takes:** every take recorded by voice-lab-desktop is identified by its audio's
     SHA-256 and published into `raw/`. Its record names the exact prompt and catalog version,
     when it was recorded, the device, and its PortAudio continuity evidence (D-039).
   - **State:** each take's decision (kept, rejected, removed, restored) and trim recipe,
     with revisions.
   - **Reads:** coverage over the active catalog, and each prompt's take history.
2. **`/api/v1` serves it** to any recording client.
3. **voice-lab-desktop's prompt studio**, corpus-first, at parity with the Swift app (§5).
   It records through the PortAudio capture worker, works offline (Save is durable on the
   machine at once), and syncs to the PC in the background.
4. **Singing recordings, source-neutral.**
   - The journey index gets a source port: Voice Memos (phone) is its first adapter, and the
     UMIK-1 measurement sessions are its second, as they already are in practice.
   - Every row says its `source`, in the index and in `/api/v1/recordings`, so a later
     source (the singing studio) plugs in without reworking the pipeline.
5. **The Swift app retired:**
   - the Mac library's existing takes exported to the PC first;
   - S-1 passed on macOS (and Linux);
   - the studio at parity;
   - then the Swift app is frozen with a tag.

**Not in v1:**
- fine-tuning on the PC (phase 5);
- the singing studio;
- a catalog editor in the app: catalog edits stay `voice-lab catalog …` on the PC;
- the pilot gate for `capture-corpus` catalogs: `cole-corpus` is `capture-pilot`.

## 3. Architecture (D-052)

**Writers.** The network-facing API never writes the lab. Its lab mount stays read-only
except `incoming/` (D-021's split). The **intake publisher**, a no-network host unit
(`deploy/host/voice-lab-intake.service`, path-triggered on `incoming/`), is already the only
process that writes `raw/` and `provenance/intake/`. In v1 it also becomes the only writer of
the corpus on the PC, so the corpus needs no new host unit and no new mount. It writes:

- **take packages** (§4), uploaded through the existing resumable `/api/v1/intake`, with the
  D-021 ledger and the standing home-host approval (class `capture`). The publisher tells a
  take package from a capture package by its manifest's schema;
- **corpus requests**, for decisions and trims. The API writes each as a small JSON file and
  renames it into `incoming/.corpus-request-<received time>-<id>.json`, which fires the path
  unit. The publisher applies the requests in that order, checks each one's revision, and
  records the outcome;
- **the read model** (`derived/corpus/`): the active catalog's reference, coverage, and each
  prompt's take history, which the API serves. The publisher rewrites it on every pass, and
  `voice-lab catalog publish|activate` rewrites it too.

**Catalog edits** stay the owner's CLI on the PC, run as the `voice-lab` user:
`voice-lab catalog …` with `--asset-root /data/voice-lab/lab`. Only the owner changes what
Cole is asked to read.

**Reads.** The API reads the read model and the catalog snapshots
(`provenance/corpus/snapshots/<sha>.json`, digest-checked by the existing file-only readers).
It never opens the corpus database, which cannot be opened on a read-only mount.

**Clients.** voice-lab-desktop keeps a local spool. Takes, decisions and trims are made locally
first, then sent in order; the PC is canonical once it has them. A client pins the catalog
version it recorded against. When the PC's active version changes, the app says so and
switches on the next session; it never mixes versions in one take. This replaces the
recording service's "activate takes effect at relaunch" rule for HTTP clients, and supersedes
docs/LIBRARY_VAULT.md's note that serving prompts from a remote store is out of scope.

**The vault.** Every new path (raw take packages, the corpus records, `derived/corpus/`) is
classified in `library_classification.py` before it exists: an unclassified path stops
every vault pass.

## 4. Contracts (new, versioned)

- **`cole-voice-lab.corpus-take/v1`**: a take's immutable record. It names:
  - the take id (`take-<sha256 of the original>`), the subject, and the exact prompt with its
    digest;
  - the catalog reference (id, version, `catalog_sha256`);
  - the recording client and capture host, and the device (`CaptureDevice`, backend
    `portaudio`);
  - the audio (`AudioSpec`, SHA-256, size, frames);
  - the requested and first-frame times;
  - a summary of the continuity evidence, plus its digest.
- **The take package** (`manifest.json` holding the take record above, `qc.json`, `READY`,
  `original.wav`, `continuity.json`). It is validated as strictly as a capture package:
  exact file set, canonical JSON, digests, a canonical mono 48 kHz s24le WAV, and
  continuity evidence bound to that WAV. It uses a capture package's control files, so the
  upload and the publisher carry it unchanged into `raw/<manifest_sha256>/`.
- **`cole-voice-lab.corpus-take-state/v1`**: a take's current decision (with revision, reason
  and time) and its trim (`start_frame`, `end_frame`, revision).
- **`cole-voice-lab.corpus-request/v1`**: a decision or trim request, with its expected
  revision: the client's `request_id` (`req-` and 32 hex digits, stable across retries), the
  take, the API's `received_at`, and one change (a decision with its reason, or a trim's
  `start_frame`/`end_frame`, both `null` to reset).
- **`cole-voice-lab.corpus-request-outcome/v1`**: what the publisher did with a request
  (`provenance/corpus-requests/<request_id>.json`): `applied` with the new revision, or
  `refused` with a code (`revision_conflict`, `take_not_found`, `trim_out_of_bounds`,
  `invalid_request`) and the take's current revision; the publisher's time.
- **API views:** `CatalogView`, `CorpusLibrary` (rows per prompt, plus coverage), `TakeHistory`;
  the bodies `TakeDecisionRequest` and `TakeTrimRequest`, and `CorpusRequestStatus` (`queued`,
  `applied` or `refused`, with the outcome).

The Swift app's take records (`RecordingTake`, Core Audio seals) are not reused. They are
bound to Core Audio and the Swift helper. Mac takes reach the PC through an exporter (step 9)
that maps them onto these contracts.

## 5. The prompt studio (the Swift app's interface, in Qt)

These are the Swift app's regions and behaviour (NATIVE_RECORDING_UX.md), to be matched:

- **Fixed workspace.**
  - A library sidebar showing coverage (`n / 1,886`), a prominent **Continue Missing**, search,
    a Show filter (All / Missing / Needs attention), labels, and per-prompt status.
  - A header with label chips and a text-size choice.
  - The script card: PROMPT · READ ALOUD, or INSTRUCTIONS for silent prompts.
  - Take picker, waveform, trim row, timer and playback row; then a 56 pt Record/Stop footer and
    a status line.
  - Transport state never moves the layout.
- **Transport and review.**
  - The live waveform while recording.
  - Take actions: Retake, Remove, Reject (with a reason), Restore.
  - Save, or Save & Next in context. Return = Save & Next, ⌘S = Save.
  - Trims with In/Out, Save trim and Reset.
  - Zoom and Fit; playback with −0.1 s and +0.1 s steps.
  - Take history across every session, with dates and status.
- **Keys.** R, Space, Return and ⌘S, plus ←/→/Home/End in the waveform. Keys are ignored
  while typing, in dialogs and on auto-repeat.
- **Corpus-first (D-018).** No sessions, groups or packages are visible. Continue Missing walks
  the whole catalog in order.

voice-lab-desktop's current Record → Prompts (4c, D-042) is session-based. It becomes this
studio. The session store, Finish and the per-session package give way to take-by-take sync.

## 6. Steps

Each step is one or more PRs, each verified (`scripts/verify`, `dev check .`, the guard),
merged with `Merge #N: <title>`, with the deploy watched to green.

| # | What | Done when |
| --- | --- | --- |
| 1 | This plan; D-051, D-052 | merged |
| 2 | **Catalog on the PC.** The read model's active catalog; `GET /api/v1/catalog` (ETag = the snapshot's digest); classification of `derived/corpus/`; the owner's seeding steps | a synthetic store served over the API in tests; the owner seeds the PC |
| 3 | **Takes in.** The take contracts and the strict validator; the publisher's dispatch into `raw/`; the take records; `GET /api/v1/corpus/library`; `GET /api/v1/corpus/prompts/{id}/takes`; take audio behind the playback approval | a synthetic take uploaded end to end appears in the library |
| 4 | **Decisions and trims.** `POST /api/v1/corpus/takes/{id}/decision`, `PUT …/trim`; the corpus requests; revisions | conflicting revisions refused; the read model follows |
| 5 | **The app's corpus client.** A local spool and sync (offline-first); the catalog from the PC; take upload, decisions and trims queued | an offline session syncs when back online, in tests |
| 6 | **The prompt studio UI**, in several PRs: library, transport and waveform, review and history, keys, polish | the parity list (§5) checked off |
| 7 | **Singing, source-neutral.** The journey index's source port; Voice Memos and measurement adapters; `source` in rows and the API | the index and API carry `source`; nothing else changes |
| 8 | **S-1 on real hardware** (owner, or the Mac agent with the UMIK-1) | D-040's verdict recorded |
| 9 | **The Mac's takes to the PC.** An exporter the Mac agent runs, under a transfer approval naming the payload digest; the publisher imports | the Mac library's takes counted on the PC |
| 10 | **Retire the Swift app.** Freeze it with a tag; the docs | the owner's sign-off |

## 7. The owner's steps

1. **Seed the PC's catalog** (step 2), once. On the PC, as you, from your checkout:

   ```sh
   cd ~/src/voice-lab && git pull --ff-only
   scripts/seed-pc-catalog
   ```

   It asks for your sudo password and runs the lab's steps as the `voice-lab` user:
   - it downloads the Harvard and ARCTIC lists and stops, writing nothing, unless both match
     `standard_prompt_sets.STANDARD_SET_SHA256`;
   - it runs docs/CORPUS_STORE.md "Seeding a library" with `--asset-root /data/voice-lab/lab`
     and the Mac's `--fetched-at`: init from pilot-v2 without room tones, import Harvard, then
     ARCTIC, publish;
   - it activates version 2 for `cole-self` only when its `catalog_sha256` is `1ebd48cb…`, the
     Mac's. The same files and `--fetched-at` give the same digests, so the Mac's takes name
     versions the PC holds.

   It continues from wherever the store is, so a stopped run is finished by running it again.
   Once seeded, a run re-checks version 2 and activates it again, which rewrites what
   `GET /api/v1/catalog` serves (version 2, 1,886 prompts). It says so when the deployed build
   predates that route.
2. **S-1 on the Mac** (step 8), with the UMIK-1: `scripts/s1-portaudio`, or ask the Mac agent.
3. **The transfer approval for the Mac's takes** (step 9).
4. **Sign off parity and retire the Swift app** (step 10).

## 8. Risks

- **S-1 may not pass on macOS.** Then the Swift app stays the macOS recorder (D-025), and the
  corpus still moves: Swift takes reach the PC through step 9's exporter, repeated as needed.
- **One writer.** The intake publisher is a single serialized pass, by design. Its latency is
  the path unit's, a second or so. The app never waits on it: it is offline-first.
- **Catalog identity.** The Mac's takes name `catalog_sha256` values. The PC must hold the
  same versions, so seeding checks the digests before activating.
- **Vault classification.** Every new path is classified in the same PR that first writes it.

## 9. Progress

- 2026-10-12: the plan (step 1).
- 2026-10-12: step 2, the catalog on the PC. `corpus_read_model` records the subject's active
  catalog in `derived/corpus/` (never vaulted). It is refreshed by `voice-lab catalog
  init|publish|activate` and by every intake pass, which reports a broken store but still
  publishes. `GET /api/v1/catalog` serves it with the snapshot's digest as its `ETag`, and
  never opens the store. Seeding the PC is owner step 1.
- 2026-10-12: `scripts/seed-pc-catalog`, owner step 1 in one command.
- 2026-10-12: step 3, takes into the PC. A take package (`corpus_takes`,
  `cole-voice-lab.corpus-take/v1`: the take, `qc.json`, `READY`, `original.wav` and the
  PortAudio worker's `continuity.json`, whose contract moved into the backend) rides the
  existing upload and publisher, which tell it from a capture package by its manifest's
  schema and publish it into `raw/<manifest_sha256>/` (not `raw/takes/`, so the upload, the
  receipt lookup and the vault's raw source stay one path). The publisher refuses generated
  audio and a prompt the take's catalog snapshot does not count, and records each take in
  `provenance/corpus-takes/` (an undecided draft, rebuildable from `raw/`, copied by the
  vault). The read model gains the library (`derived/corpus/library-<subject>.json`), rebuilt
  by every pass and every `catalog` command; `GET /api/v1/corpus/library`,
  `GET /api/v1/corpus/prompts/{id}/takes` and, behind the playback approval,
  `GET /api/v1/corpus/takes/{id}/audio` serve it. After review: the publisher checks the
  take's whole catalog reference against the version its store published, and defers (never
  refuses) a take whose version is not on the PC yet or whose snapshot it cannot read; a
  named pipe in `incoming/` no longer blocks a pass; the worker's device must be a UMIK-1.
- 2026-10-12: step 4, decisions and trims. `POST /api/v1/corpus/takes/{id}/decision` and
  `PUT /api/v1/corpus/takes/{id}/trim` (`api/corpus_request_routes.py`) check the take, a
  trim's frames and the body read-only, never the revision, and queue a
  `cole-voice-lab.corpus-request/v1` in `incoming/` under a name that sorts in arrival order;
  `GET /api/v1/corpus/requests/{id}` serves its status. Each intake pass applies the queue in
  order (`corpus_requests`): a change applies only at the revision its client expected, the
  outcome (`provenance/corpus-requests/`, copied by the vault) is written before the take's
  record so an interrupted pass is finished by the next, and the library follows in the same
  pass. A request id answers a retry as before and refuses another change (409); a request
  for a take still uploading or deferred waits for it, with the take's later requests; an
  unsafe request file is set aside in `incoming/.refused/`. The queue holds at most 10,000
  requests (503). A restore sends the decision the take had before (`kept` or `draft`): the
  PC keeps no restore target.
- 2026-10-12: step 5, the app's corpus client (`apps/desktop/src/voice_lab_desktop/corpus/`,
  apps/desktop/docs/CORPUS-CLIENT.md). The PC's `CatalogView` is cached in
  `<data dir>/corpus/catalog/` by snapshot digest, checked against its reference (the
  snapshot it describes hashes to `snapshot_sha256`), refreshed with `If-None-Match`, and
  pinned per session: a new active version on the PC is recorded for the next session, and
  each take is bound to a prompt of the pinned version when it starts. With no cached
  catalog and no PC nothing is recorded; with one, recording works offline. A take is saved
  in `<data dir>/corpus/takes/<take_id>/` (the worker's WAV and evidence, its package built
  once and kept, `take.json`, and `state.json` with the decision, trim, confirmed revisions
  and the take's ordered outbox). Changes get a request id made once and a chained expected
  revision. The sync engine runs off the GUI thread and sends each take's operations one at
  a time: the upload (`upload_session`, class `capture`, the D-021 approval and ledger; a
  deferred take keeps its changes queued), then each change, polled until applied. Any
  refusal adopts the PC's state for the take, drops what is queued behind it and leaves a
  notice. A `take_not_found` re-checks the upload once. Network failures leave the outbox
  as it is. Nothing is sent until `corpus_sync = true` (off until S-1 passes, D-042's
  reason), and takes of the simulated device stay local-only. The tests run an offline
  session that syncs after a restart against the real API and publisher.
  After review, only the PC's explicit take-level verdicts end a take's operations
  (`refused_by_intake` or `session_conflict` for the upload; `revision_conflict`,
  `trim_out_of_bounds`, `request_conflict` or `take_not_found` after its re-check for a
  change). Everything else is transient: the queue is kept, the try backs off, and
  `SyncStatus` says why. Version skew waits with a notice. The PC's state is adopted only
  once its library is at least at the refusal's revision; until then the take waits for a
  refresh, and the operator's changes are held and never treated as no-ops. Unsent changes
  fold into one per kind. Interrupted saves are finished or reported at start. Pokes and
  listeners run on the GUI thread.
- 2026-10-12: step 6a, the prompt studio's fixed workspace and its corpus-first loop
  (apps/desktop/docs/PROMPT-STUDIO.md). Record → Prompts is now the studio
  (`screens/studio.py`, `studio/`, `StudioScreen.qml`) over the step-5 client. Phase 4c's
  session flow stays, behind `prompts_mode = "sessions"` in `config.toml`.
  - **The workspace.** The library shows coverage (`n / N`), Continue Missing, search, Show,
    Labels, the pinned catalog version and every prompt's status. Then come the header with
    its label chips, the script card (PROMPT · READ ALOUD, or a banner and INSTRUCTIONS), the
    timer, the 56-point Record/Stop footer and the status line. The take picker, waveform,
    trim and playback regions are held at their final size for 6b. Every region's geometry
    depends on the window alone; a test compares them across idle, recording, review,
    saving, saved and failed.
  - **The loop.** Record binds a take to the selected prompt of the pinned version
    (`begin_take`) and starts the worker; Stop ends it. Save (`save_take`, then `kept`, off
    the GUI thread) keeps it here at once. Save & Next then selects the next prompt without
    a kept take, in catalog order, wrapping once; filters never change it. Retake, or
    leaving a take under review, saves that take as an undecided draft rather than losing
    it. Discard removes a failed take.
  - **Coverage.** It counts the PC's library and this machine's takes, the better of the two
    per prompt. A PC row counts only for the same prompt (id and digest), so a pending
    version switch still counts the shared prompts. To know what the PC has while offline,
    the client now keeps the PC's library (`corpus/library.py`, `library/library.json`),
    read last in every pass that reached the PC, with `If-None-Match`, also while sending
    is held.
  - **Catalog and status line.** A new active version on the PC is announced and switches at
    the next session, or at Switch now between takes. With no cached catalog the studio says
    so and records nothing. The status line gives the take's state, then the `SyncStatus`:
    held until S-1 (a plain statement), offline, queued and sent counts, takes waiting for
    the PC's state, damaged takes, unsaved recordings, and notices until they are dismissed.
  - **§5 ticked by 6a:** the fixed workspace without the text-size choice, Continue Missing,
    search, Show, Labels, per-prompt status, label chips, the script card, the Record/Stop
    footer and the status line; Save and Save & Next; Retake; corpus-first.
  - **Left to 6b and 6c:** 6b adds the take picker and history, the live waveform, trims,
    Zoom and Fit, playback, and Reject, Remove and Restore. 6c adds the keys: `toggleRecord`,
    `saveNext` and `save` are what they call, and no shortcut is installed yet.
  - **After review:** the prompt is always shown whole. The card sets it at 24 points, or at
    the largest smaller size that fits, down to 16. Below the base height the waveform gives
    way first (down to 64 points), then the card (down to 160). The longest catalog prompt
    fits at 960×600 and 1440×900, idle and recording, and a test measures it. Every
    sidebar and status line fits in two lines on the smallest window: the catalog line
    reads `cole-corpus · v1`, Show and Labels stack, and a status names its cause in a few
    words. The whole message goes to the take's place (a failure, a take that did not
    start, no microphone) or to the status line's Details (a notice with Dismiss, why
    uploads cannot start).
- 2026-10-12: step 6b, transport and review (apps/desktop/docs/PROMPT-STUDIO.md), in the
  regions 6a held: `TakeReviewModel` (`screens/studio_takes.py`, `app.studioModel.takes`),
  `studio/takes.py` and `studio/audio.py`.
  - **The take picker and History** list every take of the prompt, from this machine and
    from the PC, with when, length, status and where it lives.
    `CorpusClient.prompt_history` and `take_audio` read the PC's, the audio in memory and
    under the PC's playback approval. *Changed elsewhere* marks a take whose state the
    client adopted from the PC.
  - **Decisions:** Remove, Reject (a reason is required) and Restore go through
    `decide`/`restore`, and Save keeps a selected draft. A take recorded elsewhere is shown
    and played, not decided: the client holds this machine's takes only. Deciding it from
    here needs a client change, left open.
  - **The waveform** uses the Swift app's fixed dB display scale, with Zoom (down to 50 ms)
    and Fit. While recording it is live from the worker's partial WAV, the only level the
    worker exposes, with the peak level.
  - **Trims:** In and Out from the playhead or the handles, then Save trim (`set_trim`) or
    Reset. The take under review keeps its trim until Save, or until Retake leaves it as a
    draft.
  - **Playback** goes through the one player. The preview is exactly the kept frames
    (tests compare them to the original), with ±0.1 s steps inside them.
  - **§5 ticked by 6b:** the live waveform; Retake, Remove, Reject, Restore; Save and Save &
    Next; trims with In/Out, Save trim and Reset; Zoom and Fit; playback with ±0.1 s; take
    history across every session.
  - **Left to 6c:** the keys, through the slots the model already has.
