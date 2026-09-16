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
Swift) so a configuration can never name the catalog. The normal helper has no
base arguments. Normal service base arguments are
`-m cole_voice_lab.recording_service --native-workspace --subject-id SUBJECT`.
Omit `--recording-group-id`; the typed context response owns the current group.

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

The maintained synthetic verification creates private PCM, identity, schema and
configuration instances. It seeds a corpus store in each fixture root, publishes
the fixture catalog and activates it for the synthetic subject
(`scripts/verify_native_workspace.py`, `seed_active_catalog`), and writes the
resulting reference to `catalog-reference.json` so the transport harness can
check that the app received exactly that catalog. Its explicit helper seal FIFO
and Python Save/QC barriers delay the real writer/service/analysis worker; they do
not synthesize successful receipts or decisions. Hidden hosted views are never
ordered onscreen.

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
