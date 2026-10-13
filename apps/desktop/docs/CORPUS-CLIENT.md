# The corpus client

`voice_lab_desktop/corpus/` is how the app records into the PC's corpus (D-051, D-052;
[RECORDING-V1.md](../../../docs/design/RECORDING-V1.md) step 5). It has no window of its own:
the prompt studio (step 6) is built on it. It is offline-first. A take, a decision or a trim
is saved on this machine at once, then sent to the PC in order. Once the PC has them, the PC
is canonical.

## The setting

| Setting | Default | What |
| --- | --- | --- |
| `corpus_sync` | `false` | Send takes, decisions and trims to the PC. Leave it off until spike S-1 has passed on this platform (D-040, and D-042's reason). The owner turns it on per machine in `config.toml`. |

While `corpus_sync` is off, nothing of the corpus leaves this machine. The app still reads
the PC's catalog, because it records against it. Takes recorded from the simulated capture
device are never sent: the PC refuses that audio. They stay here as *local-only*.

Uploads need the same standing home-host approval as every other upload (D-021), bound in
this machine's library and covering the class `capture` (FIRST-RUN.md §4). Each upload is
recorded in that library's ledger. Decisions and trims carry no audio, so they need no
transfer approval.

## On disk

Everything lives under `<data dir>/corpus/`. Directories are 0700 and records 0600. Records
are canonical JSON, written atomically.

| Path | What |
| --- | --- |
| `catalog/views/<snapshot_sha256>.json` | The PC's `CatalogView` for one catalog version. Before it is kept, and every time it is read, it is checked against its reference: the suite's digest, and the digest of the snapshot it describes. |
| `catalog/state.json` | The PC's active version as last seen, and when the PC last answered. |
| `recording/rec-…/` | A take being recorded. The capture worker writes `original.wav` and `continuity.json` here. |
| `takes/<take_id>/` | A saved take. `take_id` is `take-<SHA-256 of original.wav>`. |
| `takes/<take_id>/original.wav`, `continuity.json` | The worker's audio and its continuity evidence (D-039). |
| `takes/<take_id>/package/` | The take package (`cole-voice-lab.corpus-take/v1`). It is built once by `corpus_takes.build_take_package` and sent as built: a rebuilt manifest would be another package under the same take id. A local-only take has none. |
| `takes/<take_id>/take.json` | `LocalTake`: the exact prompt and catalog version, the times, the device, the audio. It is never rewritten. |
| `takes/<take_id>/state.json` | `TakeSync`: the decision and the trim here, the revisions the PC last confirmed, the upload state and receipt, and the **outbox**. |
| `notices.json` | Notices for the UI that have not been dismissed yet. |
| `sync.json` | When the client last reached the PC, and its last error. |

A save writes `take.json` and `state.json` inside the recording directory, then renames the
directory into `takes/`. If a crash comes between the two, the next start finishes the
rename.

## The outbox

Each take's `state.json` holds its outbox: the operations still to send, in order.

- `upload`: the take package. While it is queued, it is always first.
- `decision`: `kept`, `rejected` (with a reason), `removed`, or a restore (`kept` or `draft`,
  the last one before the rejection or removal).
- `trim`: `[start_frame, end_frame)`, or both `null` for the whole take.

Each change has its own `request_id` (`req-` and 32 hex digits). The id is made once and kept
for every retry. Each change also carries the revision it expects. A change expects the
revision the PC last confirmed for its kind (decision or trim). When an earlier change of the
same kind is still queued, it expects that change's revision plus 1. Every operation counts
its tries, its last error, and when it may next be tried. When an operation leaves the
outbox, the take's `sent`, `refused` or `dropped` count goes up, and its `journal` keeps the
last 20.

## Sync

`SyncEngine.run_pass` runs off the GUI thread, on the app's task pool. `SyncScheduler` runs a
pass about 1 s after a local change. It retries after 5 s while the PC cannot be reached,
doubling up to 5 minutes. While operations wait on the PC it retries after 10 s, also
doubling up to 5 minutes. Otherwise it checks the catalog every 10 minutes. A pass:

1. Asks the PC for its catalog with `If-None-Match`. If the PC does not answer, the pass
   stops there and every outbox stays as it is.
2. Sends nothing while `corpus_sync` is off.
3. Takes the takes in the order they were saved. For each take, it sends the operations at
   the front of the outbox, one at a time, until one cannot finish now:
   - **upload**: `intake_client.upload_session`, class `capture`, session id = take id. It
     waits for the intake receipt.
     - If the PC defers the take (its catalog version is not there yet), the upload stays
       first, with its changes queued behind it. A later pass collects the receipt.
     - If the PC refuses the take, nothing more of it is ever sent, and a notice says why.
   - **decision or trim**: submit the request, then poll `GET /api/v1/corpus/requests/{id}`
     until the PC's writer decides it.
     - `applied`: the revision here becomes the PC's.
     - Any refusal (`revision_conflict` means another client changed the take): the PC is
       canonical. The client adopts the PC's decision and trim for the take (from the
       prompt's history) and drops every change still queued for it. It keeps a
       *changed elsewhere* notice.
     - `take_not_found`: the PC refused the take or never saw it. The upload is checked again,
       once, and its answer decides.

If the server stops answering mid-pass, the pass ends there, nothing is lost, and the next
pass resumes. A retried request reuses its id, so the PC never applies a change twice.

## The catalog and sessions

Making a `CorpusClient` starts a session. The session pins the PC's active catalog version
as last seen: from the cache, so this works offline. `begin_take` binds the next take to a
prompt of the pinned version before the worker starts, so a take never mixes versions.

When the PC activates a new version, `status().switch_pending` says so. The next session
switches: a new client, or `start_new_session()` between takes. With no cached version,
nothing can be recorded until the PC has answered once (`status().record_blocked`).

## What the studio uses

`CorpusClient.from_context(context)` builds the client from the app's settings, sign-in and
task pool. It offers:

- `start()`, `shutdown()`, `sync_now()`;
- `pinned` and `start_new_session()`;
- `begin_take(prompt_id)`, then `save_take(pending, captured)` (or `discard_take`);
- `decide`, `restore`, `set_trim`, `takes()`, `takes_for_prompt()`;
- `notices()` and `dismiss_notice()`;
- `status()`, a `SyncStatus` value;
- `add_listener(fn)`, which is called on the GUI thread whenever the status may have changed.

None of it is a QObject: the studio's view model wraps it, with `@Slot` methods (#41).
`save_take` copies and hashes the WAV, so call it off the GUI thread for long takes.
