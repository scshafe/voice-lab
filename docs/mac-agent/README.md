# The Mac agent: building and trying voice-lab's macOS apps on Cole's Mac

The project's own agent lives on the PC (`agent-voice-lab`, scshafe-dev). It cannot build
macOS apps, and by design it cannot reach the Mac. So on the PC `scripts/verify` skips the
Swift app's build and tests, and nothing there can produce a `.app`. The Mac gets its own
agent, which Cole starts in one directory. It runs one command, `scripts/mac-refresh`. That
command verifies a commit on the Mac (the Swift suites included), builds the Swift app and
voice-lab-desktop, and, when asked, installs both. It then reports back on the pull request.
The Mac agent never pushes, opens pull requests or merges: changes still go through the PC
agent.

## One-time setup

1. Tools: `xcode-select --install`, then `brew install python@3.12 uv gh` and `gh auth login`.
2. A workspace of its own, apart from any other checkout and from every recordings folder:

   ```sh
   mkdir -p ~/voice-lab-mac && cd ~/voice-lab-mac
   git clone https://github.com/scshafe/voice-lab
   cp voice-lab/docs/mac-agent/CLAUDE.template.md CLAUDE.md
   ```

   Start the agent in `~/voice-lab-mac` (not inside the clone): `CLAUDE.md` there gives it
   its job. It is a copy on purpose, so a commit under test cannot rewrite the agent's
   rules. When the template changes on `main`, the agent shows you the diff before it
   updates its copy.
3. The Swift app's build settings: a private file at
   `~/.config/voice-lab/native-app-settings.json` (`chmod 600`). Use the settings you built
   the Swift app with before, or write them as
   [NATIVE_RECORDING_APPLICATION.md](../NATIVE_RECORDING_APPLICATION.md) "Build settings"
   says. They name your private asset root, the purpose (`capture-pilot` or
   `capture-corpus`), the subject, the capture host, and the UMIK-1 as a Core Audio
   device. Its stable UID is printed, with the UMIK-1 plugged in, by:

   ```sh
   cd ~/voice-lab-mac/voice-lab && .venv/bin/python -c \
     "from cole_voice_lab.capture_native import list_coreaudio_input_devices as l; print(*l(), sep='\n')"
   ```

   Run that after the first refresh, which creates the `.venv`. Without this file the
   refresh builds only voice-lab-desktop.
4. The stable signing identity (D-043), once:

   ```sh
   cd ~/voice-lab-mac/voice-lab && scripts/mac-signing-identity --execute
   ```

   It creates the certificate `voice-lab local signing` in your login keychain. macOS asks
   for your login password once, to trust it for code signing, and may ask to let
   `codesign` use the key: choose **Always Allow**. From then on every build is signed with
   it, and the microphone permission you grant an app survives rebuilds. Without it, builds
   are signed ad-hoc and macOS asks again after each one.

   That certificate has no Apple team ID, so the sign-in voice-lab.app keeps in the
   keychain is tied to each build's own code: every new build asks for your login password
   ("voice-lab wants to access key 'voice-lab-desktop'") before it can read it. Step 5 ends
   that.
5. A free Apple Development certificate (D-054), recommended. In Xcode, open Settings →
   Accounts, add your Apple ID (Xcode makes a free Personal Team), then **Manage
   Certificates… → + → Apple Development**. `scripts/mac-refresh` prefers it over
   `voice-lab local signing` whenever the keychains hold one; check with
   `security find-identity -v -p codesigning`. The first build signed with it asks once for
   the microphone and once for the keychain sign-in (**Always Allow**). Later builds ask for
   neither, because both are tied to the team rather than to one build. The certificate lasts
   about a year; Xcode renews it from the same screen.

## Day to day

```sh
cd ~/voice-lab-mac && claude "refresh the apps from main and install them"
cd ~/voice-lab-mac && claude "try PR 38"
```

Behind each request the agent runs, from the clone:

```sh
scripts/mac-refresh --execute --allow-network --install     # main, installed
scripts/mac-refresh --execute --allow-network --pr 38       # a pull request, not installed
```

Here is what a refresh does:

- It verifies with `scripts/verify`, which takes about half an hour on the Mac. A failure
  stops it.
- It builds both apps into `~/Library/Application Support/voice-lab/mac-refresh/runs/`.
- With `--install`, it copies them into `/Applications`. The previous copies are kept in
  the run's `replaced/`.

The script refuses two kinds of install unless you say otherwise:

- **A commit that isn't on `main`**: `--allow-non-main-install`.
- **An older Swift app over a newer one**: `--allow-downgrade`. An older native build cannot
  open a library a newer one has opened (NATIVE_RECORDING_APPLICATION.md).

Without a flag, `scripts/mac-refresh` only prints the plan.

The installed Swift app reads its configuration from its run directory, so do not delete
the run of an installed app. `installed.json` in that directory names the run of each
installed app.

## What only you can do

- The first launch of each app, and allowing the microphone. With the stable identity,
  macOS asks once per app.
- Plugging in the UMIK-1 for anything that records: S-1 ([apps/desktop/docs/S-1.md](../../apps/desktop/docs/S-1.md)),
  or a session.
- Approving an install of a pull request's build, or of an older Swift app.

## Copying a build to another Mac

The zip that `scripts/build-desktop-bundle` writes runs on another Mac after the Gatekeeper
steps in [apps/desktop/docs/INSTALL-MACOS.md](../../apps/desktop/docs/INSTALL-MACOS.md).
The stable identity only helps on the Mac that holds its key.
