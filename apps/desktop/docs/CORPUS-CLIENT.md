# The corpus client

`voice_lab_desktop/corpus/` is how the app records into the PC's corpus (D-051, D-052;
[RECORDING-V1.md](../../../docs/design/RECORDING-V1.md) step 5). It has no window of its own:
the prompt studio ([PROMPT-STUDIO.md](PROMPT-STUDIO.md), step 6) is built on it. It is offline-first. A take, a decision or a trim
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
| `recording/rec-….json` | Beside it: the prompt and catalog version the take is bound to (the worker needs its directory empty). |
| `takes/<take_id>/` | A saved take. `take_id` is `take-<SHA-256 of original.wav>`. |
| `takes/<take_id>/original.wav`, `continuity.json` | The worker's audio and its continuity evidence (D-039). |
| `takes/<take_id>/package/` | The take package (`cole-voice-lab.corpus-take/v1`). It is built once by `corpus_takes.build_take_package` and sent as built: a rebuilt manifest would be another package under the same take id. A local-only take has none. |
| `takes/<take_id>/take.json` | `LocalTake`: the exact prompt and catalog version, the times, the device, the audio. It is never rewritten. |
| `takes/<take_id>/state.json` | `TakeSync`: the decision and the trim here, the revisions the PC last confirmed, the upload state and receipt, and the **outbox**. |
| `library/library.json` | `CachedLibrary`: the PC's library (each prompt's status and take count, the coverage) as last read, with its `ETag`. No audio, no prompt text. The prompt studio counts coverage from it and the takes here. |
| `notices.json` | Notices for the UI that have not been dismissed yet. |
| `sync.json` | When the client last reached the PC, and its last error. |

A save writes `take.json` and `state.json` inside the recording directory, then renames the
directory into `takes/`, under the spool's lock (a listing never sees half a save). At the
next start:

- a save a crash interrupted after its records were written is finished;
- a take the worker finished but that was never saved is saved from its `rec-….json`;
- any other recording an earlier run left is counted in `status().unsaved_recordings`;
- a take directory whose records cannot be read is listed in `status().damaged`.

Nothing is ever hidden or removed.

## The outbox

Each take's `state.json` holds its outbox: the operations still to send, in order.

- `upload`: the take package. While it is queued, it is always first.
- `decision`: `kept`, `rejected` (with a reason), `removed`, or a restore (`kept` or `draft`,
  the last one before the rejection or removal).
- `trim`: `[start_frame, end_frame)`, or both `null` for the whole take.

Each change has its own `request_id` (`req-` and 32 hex digits). The id is made once and kept
for every retry. Each change also carries the revision it expects. A change expects the
revision the PC last confirmed for its kind (decision or trim). When an earlier change of the
same kind is still queued, it expects that change's revision plus 1.

A new change is folded into the same kind's last queued change while the PC can know nothing
of that one: it was never tried and is not being sent. The folded change keeps its id and
expected revision, and takes the new target. So the outbox holds at most a few changes,
however long the PC is away. A change that was tried, or is being sent, is never altered: the
PC may already know its id. Every operation counts
its tries, its last error, and when it may next be tried. When an operation leaves the
outbox, the take's `sent`, `refused` or `dropped` count goes up, and its `journal` keeps the
last 20.

## Sync

`SyncEngine.run_pass` runs off the GUI thread, on the app's task pool. `SyncScheduler` runs a
pass about 1 s after a local change. It retries after 5 s while the PC cannot be reached,
doubling up to 5 minutes. While operations wait on the PC it retries after 10 s, also
doubling up to 5 minutes. Otherwise it checks the catalog every 10 minutes. A pass:

1. Asks the PC for its catalog with `If-None-Match`. If the PC does not answer, or refuses,
   the pass stops there and every outbox stays as it is.
2. Sends nothing while `corpus_sync` is off (read before every operation).
3. Takes the takes in the order they were saved. For each take, it sends the operations at
   the front of the outbox, one at a time, until one cannot finish now:
   - **upload**: `intake_client.upload_session`, class `capture`, session id = take id. It
     waits for the intake receipt.
     - If the PC defers the take (its catalog version is not there yet), the upload stays
       first, with its changes queued behind it. A later pass collects the receipt.
     - If the intake refuses the package, nothing more of it is ever sent, and a notice says
       why (see the verdicts below).
   - **decision or trim**: submit the request, then poll `GET /api/v1/corpus/requests/{id}`
     until the PC's writer decides it.
     - `applied`: the revision here becomes the PC's.
     - A verdict on the take (below): the PC is canonical. The change and those queued
       behind it are dropped, the PC's state is adopted, and a *changed elsewhere* notice is
       kept.
     - `take_not_found`: the PC refused the take or never saw it. The upload is checked again,
       once, and its answer decides.

**Only the PC's own verdicts on a take end its operations.** These are:

- the intake's refusal of the package (`refused_by_intake`), or another package under the
  take's id (`session_conflict`): the take is never sent again;
- a change refused for the take's own reason (`revision_conflict`, `trim_out_of_bounds`,
  `request_conflict`): that change and every change queued behind it are dropped;
- `take_not_found` once the upload was checked again.

The PC is canonical after a refusal, but its library is rebuilt after the refusal. So its
state for the take is installed only when the library shows at least the revisions the PC is
known to have reached. Until then, `state.json` holds a pending refresh:

- the state here stays as it was;
- each later pass reads the prompt's history again;
- the operator's new changes are queued, even one equal to the (possibly stale) state here,
  but nothing is sent for the take;
- once the PC's state is installed, those changes are chained again on its revisions.

**Everything else is transient.** That covers no answer, the door or a proxy (any other
4xx), a lost sign-in (401/403, with or without a body), a busy session, a 5xx, the tailnet
check, the sign-in provider, and a local package that fails its checks. The queue is kept,
the operation counts the try and backs off, and `SyncStatus` says what happened: `offline`,
`sign_in_required`, `last_error`. A change the PC cannot read (`invalid_request`: version
skew) waits the same way, with one notice. A request the PC set aside unread goes again
under a new id.

A pass the server stops answering mid-way ends there; nothing is lost, and the next pass
resumes. A retried request reuses its id, so the PC never applies a change twice.

Last, a pass that reached the PC reads its library (`GET /api/v1/corpus/library`,
`If-None-Match`: a `304` rewrites nothing), also while sending is held: like the catalog, it
sends nothing of the corpus. A PC that has no library yet leaves the last one read; a
library that is not Cole's is not kept; neither stops the pass (`PassResult.library_error`).

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
- `library`, the PC's library as last read (`LibraryCache.latest`);
- `notices()` and `dismiss_notice()`;
- `status()`, a `SyncStatus` value;
- `add_listener(fn)`, which is called on the GUI thread whenever the status may have changed.

None of it is a QObject: the studio's view model wraps it, with `@Slot` methods (#41).
`save_take` copies and hashes the WAV, so call it off the GUI thread. Every method may be
called from any thread: the scheduler's timer and the listeners are always driven on the GUI
thread (through the task pool's bridge).
