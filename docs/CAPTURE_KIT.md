# Capture-kit build and bootstrap

The capture kit is an offline installation bundle for the MacBook capture host.
It contains the lab wheel, its exact runtime dependency wheels, a hash-locked
requirements file, a non-operative site-config example, and the bootstrap
script. It never contains recordings, references, embeddings, model weights, or
experiment evidence.

## Build trust boundary

The builder creates a fresh, temporary Python 3.12 environment for every build,
installs these exact hash-locked wheels, and then invokes PEP 517 with
`--no-isolation`:

- `build==1.5.0`
- `setuptools==84.0.0`
- `wheel==0.46.3`
- `packaging==26.3`
- `pyproject-hooks==1.2.0`

Both that build toolchain and the capture-host runtime dependency set are
authorized by committed, exact wheel SHA-256 locks under `requirements/`.
Even `--allow-network` may fetch only bytes matching those locks; a package
index cannot substitute a different wheel for the same name and version. The
bundle metadata records both committed lock-file digests so the standalone
capture kit retains this build provenance, together with the resolved build
interpreter path, version, and SHA-256.

That interpreter evidence is a closed runtime identity, not only a path string.
The bundle binds the exact Homebrew Python Cellar prefix tree, the resolved
interpreter bytes and identity, the `python@3.12` opt-link target, the bundled
`ensurepip` pip wheel, and the exact Homebrew dependency-keg trees and opt-link
targets used by that interpreter. It also binds the reviewed macOS build and
the interpreter's permitted system-library references. Bootstrap recomputes
that closure before every verification, install, recovery, or idempotent run;
an interpreter upgrade, keg mutation, or opt-link retarget requires rebuilding
and independently reviewing a new capture kit. Every immediate hop of every
symlink inside a reviewed Cellar tree must stay inside that same hashed keg;
bounded no-follow traversal rejects cycles, escape-and-reentry chains, and
external intermediaries even when their current final target is back in the
keg.

An offline build requires a local wheelhouse containing both the exact build
toolchain and capture-runtime dependency wheels:

```sh
scripts/build-capture-kit --wheelhouse /absolute/reviewed/wheelhouse --replace
```

The builder passes `--no-index` and `--require-hashes` to both the temporary
toolchain install and runtime-wheel download. It does not silently fall back to
a package index, and no package from the repository development environment
participates in the wheel build. It also requires an immutable, completely
clean Git `HEAD`, exports that commit into a fresh staging source tree, and
builds with `SOURCE_DATE_EPOCH` set to the commit timestamp. `BUNDLE.json`
records the exact commit, tree, and epoch. Ignored build directories,
untracked files, and stale backend outputs therefore cannot enter the wheel,
and two builds of the same commit on the same reviewed host closure produce
the same application-wheel and bundle-manifest digests.

Network access is opt-in for a deliberate first build:

```sh
scripts/build-capture-kit --allow-network --replace
```

That flag permits only the script's pinned build-tool installation and pinned
runtime-wheel download steps. It does not deploy anything. An existing output
is never overlaid: use `--replace` to replace a directory that validates as a
previous generated capture kit. Construction occurs in a fresh same-filesystem
staging directory, so stale wheels and stale `SHA256SUMS` entries cannot survive
publication.

## Preserve the independent digest

The successful build prints:

- `BUNDLE_MANIFEST_SHA256`, the SHA-256 of `SHA256SUMS`;
- `APPLICATION_WHEEL`, the one lab wheel filename; and
- `APPLICATION_WHEEL_SHA256`, its digest.

Record those values outside the capture-kit directory. Sending a digest in the
same archive or transfer as the files it authenticates does not provide an
independent trust path. Transfer the complete directory only after the operator
has preserved the printed values separately. The transferred
`macbook-bootstrap` is not a trust anchor for its own bytes: do not execute any
file from the bundle until trusted macOS system tools have authenticated the
manifest and every entry.

On the MacBook, first use the independently retained digest with the system
`shasum`, then verify every manifest entry from inside the received directory:

```sh
CAPTURE_KIT=/absolute/capture-kit
APPLICATION_WHEEL='<printed APPLICATION_WHEEL>'
APPLICATION_WHEEL_SHA256='<printed APPLICATION_WHEEL_SHA256>'
BUNDLE_MANIFEST_SHA256='<independently retained BUNDLE_MANIFEST_SHA256>'

/usr/bin/printf '%s  %s\n' \
  "$BUNDLE_MANIFEST_SHA256" "$CAPTURE_KIT/SHA256SUMS" \
  | /usr/bin/shasum -a 256 -c -

(cd "$CAPTURE_KIT" && /usr/bin/shasum -a 256 -c SHA256SUMS)
```

Both commands must report `OK`. They authenticate `macbook-bootstrap` along
with the wheels and metadata without trusting any bundled executable. Only
then run the deeper semantic verifier, still without installing:

```sh
"$CAPTURE_KIT/macbook-bootstrap" \
  --verify-bundle \
  "$CAPTURE_KIT/$APPLICATION_WHEEL" \
  "$APPLICATION_WHEEL_SHA256" \
  "$BUNDLE_MANIFEST_SHA256"
```

The bundled verifier redundantly checks the manifest digest first, then exact bundle membership,
every member digest, the executing bootstrap copy, the selected application
wheel, `BUNDLE.json`, and the one-to-one wheel/hash requirements lock. It opens
**every** wheel as a ZIP before installation. Each archive must contain only
regular members with safe relative POSIX names: absolute paths, `..`, duplicate
names, directories, symlinks, devices, encrypted entries, and unsupported
`.data` layouts are rejected. Each wheel must also have exactly one matching
top-level `.dist-info/METADATA` and `.dist-info/RECORD`; every RECORD row,
SHA-256, size, and archive member must agree. The application wheel's
`Requires-Dist` names may refer only to distributions in the reviewed bundled
wheel set. This inspection also runs inside the builder immediately before
atomic publication.

The verify-only path may use stock macOS Python when the pinned Homebrew Python
is not yet available. That control interpreter runs with an empty environment
and Python's isolated mode (`-I -B`), which excludes the current directory,
`PYTHONPATH`, and user site packages. Installation still requires the exact
reviewed Homebrew Python 3.12 runtime closure recorded in `BUNDLE.json`.

## Install and recover

First create the dedicated real parent. It must already exist when bootstrap
starts, must have a canonical absolute path, and must contain no symlink
component:

```sh
install -d -m 700 $HOME/.local/share/voice-lab
```

Then install into a fresh, dedicated child below Cole's home:

```sh
/absolute/capture-kit/macbook-bootstrap \
  $HOME/.local/share/voice-lab/capture-host \
  /absolute/capture-kit/cole_voice_lab-VERSION-py3-none-any.whl \
  APPLICATION_WHEEL_SHA256 \
  BUNDLE_MANIFEST_SHA256
```

For an existing capture host, treat a console-capable kit as a new install,
not an in-place upgrade. Bootstrap into a fresh sibling root such as
`capture-host-console-01`; it intentionally refuses to overwrite a changed
target. Keep the old root until every session beneath its `assets/tmp` has
been synced and accepted:

```sh
CAPTURE_ROOT=$HOME/.local/share/voice-lab/capture-host-console-01
test ! -e "$CAPTURE_ROOT" && test ! -L "$CAPTURE_ROOT"

"$CAPTURE_KIT/macbook-bootstrap" \
  "$CAPTURE_ROOT" \
  "$CAPTURE_KIT/$APPLICATION_WHEEL" \
  "$APPLICATION_WHEEL_SHA256" \
  "$BUNDLE_MANIFEST_SHA256"
```

Bootstrap creates the venv with `--without-pip`. It invokes the exact pip wheel
inside the reviewed Python closure only transiently, through `PYTHONPATH`, and
uses the exact one-to-one bundled wheel requirements with `pip --no-index
--no-deps --no-compile --require-hashes`; dependency metadata can neither
resolve nor fetch anything. Persistent pip/setuptools packages, `.pyc` files,
`.pth` files, and unrecorded site-package paths are forbidden. Installation
occurs in a newly created mode-`0700` same-filesystem sibling staging directory
named after the target. The staging root contains a mode-`0600` `.INSTALLING`
receipt binding the target path, application wheel, wheel digest,
independently supplied bundle-manifest digest, and reviewed Python-closure
digest.

If installation is interrupted, rerun the same command with the same verified
bundle. Bootstrap never invokes Python or pip from an incomplete stage and
never overlays its runtime. It atomically moves that whole stage to a confined
mode-`0700` sibling such as
`.capture-host.RECOVERY.RANDOM/partial`, then builds from a brand-new stage.
The preserved partial is not automatically deleted.

Before `.INSTALLING` becomes `READY`, bootstrap derives an exact
`site-packages` allowlist from the verified wheel archives. It verifies every
installed wheel file against archive RECORD hashes, sizes, and content;
requires the complete file and directory walk to equal that allowlist plus only
canonical pip-generated `INSTALLER`/`REQUESTED` evidence; and verifies the
installed RECORDs in one exact sorted, LF-terminated canonical byte form. It
replaces pip's staging-path console script with one exact, bundle-code-derived
relocatable wrapper, independently verifies those wrapper bytes, and requires
it to be a single-link regular executable. The only install-tree symlinks
allowed are the venv `bin/python*` launchers; each is sealed with its link text,
canonical resolved path, target size, and target SHA-256 and must resolve to the
reviewed interpreter.

The v2 receipt then provides complete-tree drift evidence. It is not the trust
root for installed executable content: the independently supplied bundle
digest, reviewed wheel archives, Python runtime closure, and canonical derived
wrapper remain the verification anchors. A recovered READY stage is published
only after replaying those content checks and the full-tree seal. An invalid
READY stage is preserved under the same recovery convention and rebuilt
cleanly. Publication is an atomic same-filesystem, no-clobber rename. Bootstrap
then executes `runtime/bin/voice-lab --help` through the final published path
with bytecode writing disabled and revalidates the seal. Rerunning against an
existing matching final root performs all the same validation and final-path
probe and succeeds idempotently; any unrelated or changed target fails closed
and is never overwritten.

The `assets/` subtree is intentionally mutable, non-executable capture data,
not part of the code trust root. Its READY tree digest records the pristine
post-install baseline and detects ordinary drift, but it does not authenticate
a same-owner coherent rewrite and reseal. Captures naturally change that
subtree, after which bootstrap will fail its pristine-tree idempotence check;
asset authenticity instead comes from the capture/provenance and reviewed sync
workflow. The canonical `-I -P` wrapper does not add `assets/` to Python's
import path.

## Absolute post-install commands

Use the installed executable by absolute path. Probe/select the UMIK-1 in a
foreground Terminal, then launch the recommended local capture console. The
browser is only an authenticated controller and preview player; native FFmpeg
in the foreground Terminal remains the sole audio-capture process.

```sh
CAPTURE_ROOT=$HOME/.local/share/voice-lab/capture-host-console-01
VOICE_LAB="$CAPTURE_ROOT/runtime/bin/voice-lab"
ASSET_ROOT="$CAPTURE_ROOT/assets"
SESSION_ID=pilot-YYYYMMDD-01
UMIK1_DEVICE='REVIEWED UMIK-1 DEVICE ID OR DISPLAY NAME'

"$VOICE_LAB" capture devices --ffmpeg /opt/homebrew/bin/ffmpeg

"$VOICE_LAB" capture console \
  --asset-root "$ASSET_ROOT" \
  --session-id "$SESSION_ID" \
  --suite pilot \
  --device "$UMIK1_DEVICE" \
  --open
```

The command prints its tokenized loopback URL exactly once. If the foreground
process is interrupted before finalization, relaunch that same declared
session only with the explicit resume flag:

```sh
"$VOICE_LAB" capture console \
  --asset-root "$ASSET_ROOT" \
  --session-id "$SESSION_ID" \
  --suite pilot \
  --device "$UMIK1_DEVICE" \
  --resume --open
```

Resume re-inspects every completed canonical take and moves interrupted
partials to the private rejected-take directory outside the session. For a
terminal-only capture with no review console, `capture run` remains available
with the same arguments but without `--resume` or `--open`.

After local QC, build the live sync plan with the finalized absolute session
path and explicit restricted-SSH evidence:

```sh
"$VOICE_LAB" sync plan \
  "$ASSET_ROOT/tmp/capture-$SESSION_ID" \
  --remote-user MINI_USER \
  --remote-root /Users/MINI_USER/.local/share/cole-voice-lab/incoming \
  --identity-file $HOME/.ssh/ABSOLUTE_RESTRICTED_INTAKE_KEY \
  --known-hosts-file $HOME/.ssh/ABSOLUTE_REVIEWED_KNOWN_HOSTS \
  --live-plan --json
```

Review and retain the printed plan digest before separately repeating the same
absolute command with `--execute --allow-network --approved-plan-sha256
'REVIEWED_PLAN_SHA256'`. The bootstrap does not authorize that network action.

The capture-kit bundle schema remains `v1`; the stronger standalone install
receipt is `cole-voice-lab.install-receipt/v2`. Rebuild older capture kits:
archives without complete RECORD/content evidence, exact dependency-wheel
evidence, or the closed Homebrew Python runtime evidence intentionally fail the
current verifier. A Homebrew Python/macOS update also requires a rebuilt and
newly reviewed kit.

`site.example.toml` remains documentation only after transfer. No command loads
it; use explicit command-line arguments for every operative path or policy.
