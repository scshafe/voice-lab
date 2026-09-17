# Native recording application launch configuration

`RecordingApplicationConfiguration` is the canonical Swift loader for the native
workspace. It reads a bounded, private, no-follow JSON file and checks its exact
key set, its version, the declared purpose, and the peer and build identities.
The file carries no catalog: after the handshake the app receives the subject's
active catalog from the recording service (`CatalogCommand` → `CatalogEvent`,
`docs/CORPUS_STORE.md`). Device, host and pilot-approval values are validated when
the channel checks each outgoing open-context against the Python-owned
`recording-service-request` schema. Swift never computes a canonical catalog or
reservation digest; the catalog digest comes from the service's `CatalogReference`.

The signed app contains only `Resources/RecordingConfigurationPath.json`, with
exact keys `schema_version` (`cole-voice-lab.recording-configuration-path/v1`)
and `configuration_path` (normalized absolute path to the external private file).
An explicit `--configuration ABSOLUTE_PATH` selects the same loader for synthetic
harnesses. The locator contains no executable digest: signing the app first and
then writing external manifests avoids a self-referential signature.

The external application file (`NativeRecordingApplicationConfiguration`) has
exactly these keys:

| Key | Value |
| --- | --- |
| schema_version | `cole-voice-lab.recording-application/v2` |
| asset_root | Resolved absolute private asset root |
| schema_directory | Absolute directory of generated recording schemas |
| client_build | Exact `RecordingBuildIdentity` for the signed app executable |
| client_build_path | Absolute private 0600 file containing the identical identity |
| purpose | The catalog purpose the release was built for: `capture-pilot`, `capture-corpus`, `held-out-evaluation` or `physical-test` |
| subject_id | Configured subject identity |
| capture_device | Exact stable `CaptureDevice` |
| capture_host | Capture host identity |
| pilot_approval_path | Approved evidence path or JSON null |
| pilot_approval_sha256 | Bound digest paired with the path, or JSON null |
| recorder | Peer configuration below |
| service | Peer configuration below |

The loader refuses any other version (a `recording-application/v1` file that
embeds a catalog included), a missing or extra key, and a purpose outside the four
values. `purpose` guards the release's asset root only. At runtime the purpose
that counts is that of the catalog the service pins, which the service checks
against the root. A build configured with a pilot approval refuses a delivered
catalog whose purpose is not `capture-corpus`.

Each peer has exactly `executable`, `arguments`, `environment`, `build`, and
`build_identity_path`. `arguments` is a base argument array (at most 32 strings,
4096 characters each). `environment` is null or an explicit map with at most 64
bounded string entries. `build` and its private file must be identical strict
`RecordingBuildIdentity` objects. The actual executable hash must agree with the
handshake; the application also checks its own executable against `client_build`.

The loader generates a fresh process UUID for each peer on every launch and adds
`--asset-root`, `--process-generation`, `--build-identity`, and
`--expected-client-build`. It adds `--schema-directory` to the helper. It adds
no catalog digest to the service: the service pins its catalog from the corpus
store. Base arguments cannot supply any of these flags or `--catalog-sha256`,
which stays reserved on both sides (`_RESERVED_ARGUMENTS` in Python, `peer()` in
Swift) so a configuration can never name the catalog; a `--native-workspace`
service also refuses `--catalog-sha256` itself (argparse, exit 2). The normal
helper has no base arguments. Normal service base arguments are
`-m cole_voice_lab.recording_service --native-workspace --subject-id SUBJECT`,
followed by `--finetune-runtime-root PATH` when the build settings name a
fine-tune runtime (an ordinary, unreserved flag; the configuration has no key for it).
Omit `--recording-group-id`; the typed context response owns the current group.

The configuration names two peers, the recorder helper and the service; the app starts
both. The service starts one further process of its own when Fine-tune model or Speak is
pressed: the fine-tune worker, `<service python> -m cole_voice_lab.recording_finetune_jobs
--worker --asset-root ROOT --operation-id OP`. The same worker serves both kinds of work: a
fine-tune job (a `FineTuneWorkerRequest`, stage frames, one `FineTuneWorkerReply`) or one
Speak request (one `SpeechWorkerRequest`, one `SpeechWorkerReply`, which carries the clip
or the error). It is not a configured peer and has no handshake or build identity of its
own; it runs the service's frozen interpreter and package, under umask 077. It leads a new
session and process group, and the F5-TTS runtime processes it starts
(`<finetune_runtime_root>/bin/python`) stay in that group. It exchanges length-prefixed
frames with the service over its stdin and stdout. It kills its own group when its stdin
closes, and the service kills the group when the work is cancelled, a Speak request passes
its 120-second deadline, or the service stops. At most one worker runs at a time: Speak and
fine-tuning share that one slot. A Speak request exists only in the service's memory. See
[FINETUNE.md](FINETUNE.md), "From the app" and "Speak page".

Preserve the lexical virtual-environment launcher path in `service.executable`.
Hash its resolved interpreter for the identity, but launch the venv path: resolving
that path before launch bypasses `pyvenv.cfg` and loses the packaged dependencies.
Asset roots and private manifests require resolved, no-follow paths. No microphone
permission is requested while loading configuration, discovering devices, checking
storage, starting the service, receiving the catalog, or opening the context. Only
explicit Record invokes the AVFoundation permission request and then the native
helper Start operation.

`RecordingWorkspaceModel` owns the low-frequency transport state. Its separate
`RecordingWaveformModel` owns media telemetry and sample-derived time. Stop routes
directly to the native helper. Save keeps the current script and waveform until
one matching durable operation and selection revision supplies the next cursor.
The optional `beforeCapture` and `afterCaptureStopped` hooks are the playback
exclusion integration point; playback and review behavior is delivered in Step12.
The workspace model also owns the Speak page's `RecordingSpeakModel`, whose own
`RecordingPlaybackController` joins the same capture interlock as the review player
(`beginCaptureInterlock`, `endCaptureInterlock`). Every `RecordingPlaybackSource` declares
its audio format: `capture` (48 kHz, 24-bit, mono; the default, for takes) or `speech`
(24 kHz, 16-bit, mono, for Speak clips). The playback backend refuses a file whose layout,
frame count or digest differs from the declared source, and its read bound follows the
format's bytes per frame. A synthetic session gives both players the offline engine.
[NATIVE_RECORDING_UX.md](NATIVE_RECORDING_UX.md) §17 specifies the page.

The maintained synthetic verification creates private PCM, identity, schema and
configuration instances. It seeds a corpus store in each fixture root, publishes
the fixture catalog and activates it for the synthetic subject
(`scripts/verify_native_workspace.py`, `seed_active_catalog`), and writes the
resulting reference to `catalog-reference.json` so the transport harness can
check that the app received exactly that catalog. Its explicit helper seal FIFO
and Python Save/QC barriers delay the real writer/service/analysis worker; they do
not synthesize successful receipts or decisions. Hidden hosted views are never
ordered onscreen.

The native suites (`scripts/verify-native-recording --suite …`, default `all`) are
`contracts`, `shell`, `capture`, `capture-sanitizers`, `bridge`, `waveform`, `workspace`,
`transport`, `review`, `accessibility`, `review-sanitizers`, `finetune`, `speak`,
`recovery`, `packaged-app` and `acceptance`.
`finetune` replaced `package-preparation`. Its fixture roots
(`scripts/native_finetune_fixture.py`) add a stub F5 runtime
(`tests/finetune_stub_runtime.py`) and pass `--finetune-runtime-root` to the service. The
shipped service never overrides the pretrained-file pins (the F5-TTS v1 Base weights and
the Vocos vocoder). A configured fixture therefore starts the unchanged service through the
test-only wrapper `tests/native_finetune_service_fixture.py`, which is copied into the
fixture's isolated runtime, never into a build, and hands the stub's pins to the fine-tune
supervisor. The suite trains once to a finished run, saves a take while a second job trains,
waits until the status line shows the second job's loss and time left, and cancels that
job. A second root, whose runtime has no `bin/python`, checks that Fine-tune model
stays unavailable with its reason. `scripts/verify_native_finetune.py` re-derives every
result from the durable records.

`speak` runs the `RecordingSpeakModelTests` and `RecordingSpeakTests` Swift tests, then the
harness's `speak` case against a fixture root from `scripts/native_speak_fixture.py`. That
root is the `finetune` fixture with a second reference-candidate prompt and two finished
runs written directly (`tests/finetune_speech_runs.py`, no training). Its service starts
through the same test-only wrapper, which here also passes `--test-synthesis-deadline 10`,
so the suite can show a deadline failure in seconds; the shipped service has no such flag.
The harness (`RecordingSpeakHarness.swift`) drives the real workspace model, recorder
helper, service and Speak worker. It speaks with Latest, Base and the earlier run, plays a
clip through the offline engine, cancels a request, lets one pass its deadline, checks the
app's own gates between Speak and Fine-tune model in both directions, records and saves a
take while Speak waits, runs Save As, and compares the Record regions before and after the
page switch. It writes `native/speak-report.json`. `scripts/verify_native_speak.py`
re-reads every clip, `speech.json`, job record and output directory, checks that no Speak
marker and no fixture process remains, and writes `python-speak-evidence.json`. The identity report carries both
as `speak` and `speak_python`, and the acceptance matrix cites `speak` for §17. The
`accessibility` suite hosts `RecordingPageContainer` and checks both pages.

## Build settings

`scripts/build-native-recording-app --settings FILE` reads one settings file
(`NativeRecordingApplicationSettings`). The builder requires a regular file with no symlink
in its path, at most 1 MiB. It does not check the file's owner or mode, so keep the file
private yourself. It has these keys:

| Key | Required | Value |
| --- | --- | --- |
| schema_version | no (default) | `cole-voice-lab.native-application-settings/v2` |
| asset_root | yes | The private asset root, 1–4096 characters |
| purpose | yes, no default | The catalog purpose the build is for: `capture-pilot`, `capture-corpus`, `held-out-evaluation` or `physical-test` |
| subject_id | yes | Stable subject identity |
| capture_device | yes | Exact `CaptureDevice`; the backend must be `coreaudio` |
| capture_host | yes | Capture host identity, 1–255 characters |
| pilot_approval_path | no (default null) | Relative approved evidence path |
| pilot_approval_sha256 | no (default null) | Its digest; set exactly when the path is set |
| finetune_runtime_root | no (default null) | The F5-TTS virtual environment fine-tune jobs run in: an absolute, normalized path without `..` (write `~/.local/share/cole-voice-lab/finetune-f5-01/venv` out in full; a path starting with `~` is refused as relative); the build passes it to the service as `--finetune-runtime-root`. The build neither checks nor freezes the runtime: the app offers Fine-tune model only while `<root>/bin/python` exists, and each job checks the runtime's package and pinned weights when it starts. Without it the app cannot fine-tune |

The settings name no catalog; the build records against whatever version the
subject has active in the library's corpus store when the app launches. The
settings must satisfy the same open-context rules the app will send: a Core Audio
device, a bounded host, a paired pilot approval, and a pilot approval only with
purpose `capture-corpus`. A `native-application-settings/v1` file, which embedded a
catalog, is refused (exit 2). A physical-test build must say
`"purpose": "physical-test"` explicitly.

## Preview release packaging and rollback

`create_native_recording_application(...)`, driven by `scripts/build-native-recording-app`,
builds an explicitly named `Cole Voice Recorder Preview.app` plus an adjacent private
`.runtime` directory. Building is separate from installing: the builder never launches an
app, never installs anything, never downloads a tool, and refuses to overwrite an existing
output. It freezes the source, builds a local wheel without isolation, creates a no-pip
virtual environment containing only the five installed Pydantic-family distributions,
generates canonical schemas, release-compiles the Swift app and helper, signs the helper
and then the bundle with a local ad-hoc hardened audio-input entitlement, and writes the
external private configuration and build manifests after signing. Local ad-hoc signing is
neither Developer ID nor notarization, and it does not promise that microphone
authorization persists across rebuilds.

The builder's arguments are `--output`, `--settings`, `--source-root`, `--python` and
`--synthetic-pcm`. Its first check binds the declared purpose to the root
(`_require_release_root`): the root is a marked physical-test root exactly when the
purpose is `physical-test`, and a marked root must belong to the settings' subject.
There is no `--expect-catalog-sha256`; argparse refuses it as an unrecognized
argument. The build does not pin a catalog.

Release verification, `verify_native_recording_application(release.json)`, checks the
release and configuration are private and canonical, re-checks the root against the
configuration's `purpose`, compares the peer identities and their manifests, verifies the
signed executables and `codesign --verify --strict --deep`, re-derives the frozen Python
service identity, and checks the recording schemas are present. It verifies no catalog
digest, because the configuration carries none. The app's `--verification-report` names
the `catalog_sha256` and `catalog_reference` the service delivered at launch.

The service build identity (`recording_service_build_identity`) covers every package
`.py` file and every SQL migration the service applies: the four recording-index
projection migrations, including `20260916110000_create_recording_prompt_alias_projection.sql`,
and the corpus store migration `20260916100000_create_corpus_prompt_catalog_store.sql`.
Changing any of them changes `service_build`, so verification refuses a frozen runtime
built from other migrations.

The effective bundle minimum is derived from the actual Mach-O metadata of the app, helper
and service executables rather than declared. The Swift code keeps a macOS 13 deployment
floor, but the installed Python the service runs on requires macOS 26.0, so the bundle
reports 26.0. Build metadata such as `*.egg-info` is regenerated output and is excluded
from the frozen source copy; every other unexpected entry still fails closed.

The installed Qt launcher is untouched by a preview build, and the two coexist. The
pre-cutover code and its working Python/Qt environment are frozen separately under
`~/Library/Application Support/Cole Voice Lab/Releases/`, with the editable source imports
resolved into that frozen copy so the rollback does not depend on the mutable checkout.

**Rollback has one limit worth stating plainly: the older application cannot edit
native-format drafts.** Those drafts, their originals and their provenance are preserved
exactly as recorded and remain readable by the native app; the older application simply has
no editor for them. Rolling back therefore restores the previous recording workflow without
discarding anything, but any native-format draft has to wait for the native app to be
edited again. The rollback replay is exercised against a disposable fixture root whose
simulated primary checkout no longer has Qt code or dependencies; the retained real-root
launcher is never executed by automation.

A native build older than the corpus store cannot open a version-4 recording index or read
`recording-group/v2` groups. The new build's recording service opens the index when it
starts (`RecordingWorkspaceBackend` constructs `RecordingIndex`), and that open upgrades the
index to version 4 before anything is recorded. Pre-corpus-store code accepts only index
versions 0 to 3. An older native build therefore must not run on a root where the new build
has opened the library, even once. No command rebuilds or downgrades the index:
`RecordingIndex.quarantine_cache` has no production caller. After the new build's first
launch, the older native build is no longer a rollback path. The fallback is the Qt
application, which does not open the recording index. No evidence is lost either way; the
forward build reads everything.
