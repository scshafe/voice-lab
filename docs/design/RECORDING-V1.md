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
  renames it into `incoming/.corpus-request-<id>.json`, which fires the path unit. The
  publisher applies the request in order, checks its revision, and records the outcome;
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
  revision.
- **API views:** `CatalogView`, `CorpusLibrary` (rows per prompt, plus coverage), `TakeHistory`.

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
