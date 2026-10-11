# Installing on macOS

On macOS the app is an app bundle, `voice-lab.app`. Microphone permission (TCC) attaches to
an app bundle that declares why it records (`NSMicrophoneUsageDescription`), not to a Python
started from a terminal (design [VOICE-PLATFORM.md](../../../docs/design/VOICE-PLATFORM.md)
§6.3, D-041).

The bundle is **ad-hoc signed** (`codesign --sign -`). That costs nothing, but it is not
notarized and has no paid Developer ID (decision 13, D-028), so macOS asks you once to allow
it (see "First run" below).

**Prompted capture:** until spike S-1 passes on macOS, the Swift app stays the macOS path for
prompted recordings (D-025, design §6.2). This app's **Record: prompts** screen then serves the
S-1 trial only. The measurement ritual, the corpus, the analysis, the referee and the uploads
are the app's on macOS too.

## Build it on the Air

The build runs on macOS only. It needs:

- Homebrew's Python 3.12: `brew install python@3.12`.
- uv: `brew install uv`.
- The Xcode command line tools, for `codesign`, `plutil` and `ditto`: `xcode-select --install`.

From a checkout of this repository with its `.venv` (the repository README, "Development
bootstrap", macOS; the script itself needs only the standard library):

```sh
git pull --ff-only
scripts/build-desktop-bundle                          # the plan: every command, nothing built
scripts/build-desktop-bundle --execute --allow-network
```

Every wheel is hash-checked. The build tool comes from `requirements/desktop-bundle-macos.txt`
(PyInstaller) and `requirements/development-build.txt`. The app's runtime is exported from
`apps/desktop/uv.lock`. `--allow-network` downloads these from PyPI. To build offline instead,
use `--wheelhouse DIR`, a directory that already holds those wheels.

The build writes into `apps/desktop/dist/macos/`, which Git ignores:

- `dist/voice-lab.app`, the app;
- `voice-lab-desktop-<version>-macos.zip`, a zip of the app to copy to another Mac. Its SHA-256
  is printed at the end;
- `build-desktop-bundle.json`, the build's record: the commit, the requirements files' digests,
  and the zip's digest.

To rebuild into the same place, add `--replace`. It replaces only a previous build of this
script.

What the build checks:

- It signs the app ad-hoc with the hardened runtime and the entitlements a Python bundle needs:
  - the microphone;
  - library validation off, because ad-hoc-signed Qt, PortAudio and FFmpeg libraries carry no
    team ID;
  - executable memory, for the audio callback.
- `codesign --verify --strict --deep` must pass.
- The Info.plist must pass `plutil -lint` and carry every key the build asked for.

If the hardened build does not start, rebuild with `--no-hardened-runtime --replace`. The
hardened runtime is not required, because nothing here is notarized.

Install it:

```sh
ditto apps/desktop/dist/macos/dist/voice-lab.app /Applications/voice-lab.app
open /Applications/voice-lab.app
```

The data dir and the settings are the same as the uv tool's (README, "Settings"), so the two
ways of running the app share them.

## First run

**A build made on this Mac** is not quarantined, so it opens without the Gatekeeper prompt.
The same is true of the uv tool.

**A build copied from elsewhere** carries the quarantine attribute when it arrives through a
browser, AirDrop or Mail. Gatekeeper then blocks the ad-hoc-signed app until you allow it once:

1. Open the app. macOS says it could not verify that the app is free of malware. Choose **Done**.
2. Open **System Settings → Privacy & Security**. Under **Security**, choose **Open Anyway**
   next to `voice-lab`.
3. Open the app again, choose **Open Anyway**, and authenticate.

macOS remembers this choice for that build. On macOS 15 and later, Control-click → **Open** no
longer bypasses the check.

For a build you have checked (its zip's SHA-256 against the one printed by the build), you can
remove the quarantine attribute before the first open, and then no prompt appears:

```sh
shasum -a 256 voice-lab-desktop-0.1.0-macos.zip       # compare with the build's
ditto -x -k voice-lab-desktop-0.1.0-macos.zip /Applications
xattr -dr com.apple.quarantine /Applications/voice-lab.app
```

## The microphone

The first recording asks for the microphone: "voice-lab would like to access the microphone.
Record your voice prompts and measurement sessions with the UMIK-1." Choose **Allow**. To
change your answer later, use **System Settings → Privacy & Security → Microphone**.

TCC binds the grant to the build's code hash, and an ad-hoc signature changes with every build.
So **each new build asks for the microphone again**. This is expected, not a fault. If a new
build is refused silently, reset its grant and open it again:

```sh
tccutil reset Microphone private.cole.voice-lab-desktop
```

## Without the bundle: the uv tool

`uv tool install` (README, "Install") also works on macOS. Microphone access then attaches to
the app that starts it: Terminal, iTerm, or whichever app you start it from. Allow the
microphone for that app in **System Settings → Privacy & Security → Microphone**.
