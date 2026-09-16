# Native recording application launch configuration

`RecordingApplicationConfiguration` is the canonical Swift loader for the native
workspace. It reads a bounded, private, no-follow JSON file and validates nested
catalog/device values with the Python-owned `recording-service-request` schema.
It never computes a canonical catalog or reservation digest.

The signed app contains only `Resources/RecordingConfigurationPath.json`, with
exact keys `schema_version` (`cole-voice-lab.recording-configuration-path/v1`)
and `configuration_path` (normalized absolute path to the external private file).
An explicit `--configuration ABSOLUTE_PATH` selects the same loader for synthetic
harnesses. The locator contains no executable digest: signing the app first and
then writing external manifests avoids a self-referential signature.

The external application file has exactly these keys:

| Key | Value |
| --- | --- |
| schema_version | `cole-voice-lab.recording-application/v1` |
| asset_root | Resolved absolute private asset root |
| schema_directory | Absolute directory of generated recording schemas |
| client_build | Exact `RecordingBuildIdentity` for the signed app executable |
| client_build_path | Absolute private 0600 file containing the identical identity |
| catalog | Exact `PromptSuite`, including all prompt text |
| catalog_sha256 | Python canonical SHA-256 of that exact catalog |
| subject_id | Configured subject identity |
| capture_device | Exact stable `CaptureDevice` |
| capture_host | Capture host identity |
| pilot_approval_path | Approved evidence path or JSON null |
| pilot_approval_sha256 | Bound digest paired with the path, or JSON null |
| recorder | Peer configuration below |
| service | Peer configuration below |

Each peer has exactly `executable`, `arguments`, `environment`, `build`, and
`build_identity_path`. `arguments` is a base argument array (at most 32 strings,
4096 characters each). `environment` is null or an explicit map with at most 64
bounded string entries. `build` and its private file must be identical strict
`RecordingBuildIdentity` objects. The actual executable hash must agree with the
handshake; the application also checks its own executable against `client_build`.

The loader generates a fresh process UUID for each peer on every launch and adds
`--asset-root`, `--process-generation`, `--build-identity`, and
`--expected-client-build`. It adds `--schema-directory` to the helper and
`--catalog-sha256` to the service. Base arguments cannot override these flags.
The normal helper has no base arguments. Normal service base arguments are
`-m cole_voice_lab.recording_service --native-workspace --subject-id SUBJECT`.
Omit `--recording-group-id`; the typed context response owns the current group.

Preserve the lexical virtual-environment launcher path in `service.executable`.
Hash its resolved interpreter for the identity, but launch the venv path: resolving
that path before launch bypasses `pyvenv.cfg` and loses the packaged dependencies.
Asset roots and private manifests require resolved, no-follow paths. No microphone
permission is requested while loading configuration, discovering devices, checking
storage, starting the service, or opening the context. Only explicit Record invokes
the AVFoundation permission request and then the native helper Start operation.

`RecordingWorkspaceModel` owns the low-frequency transport state. Its separate
`RecordingWaveformModel` owns media telemetry and sample-derived time. Stop routes
directly to the native helper. Save keeps the current script and waveform until
one matching durable operation and selection revision supplies the next cursor.
The optional `beforeCapture` and `afterCaptureStopped` hooks are the playback
exclusion integration point; playback and review behavior is delivered in Step12.

The maintained synthetic verification creates private external catalog, PCM,
identity, and schema instances. Its explicit helper seal FIFO and Python Save/QC
barriers delay the real writer/service/analysis worker; they do not synthesize
successful receipts or decisions. Hidden hosted views are never ordered onscreen.

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
