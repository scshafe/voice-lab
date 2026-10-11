# First run

What you need: the voice-lab server reachable on the tailnet, Pocket ID, and for
measurement sessions the miniDSP UMIK-1 and its per-serial calibration file. Install first
(README "Install").

## 1. A keychain (Linux only)

The sign-in is kept only in the OS keychain. macOS always has one. On Linux, check:

```sh
voice-lab-desktop --check-config
```

If it says `keychain: NONE`, start a Secret Service provider (GNOME Keyring:
`sudo pacman -S gnome-keyring libsecret`; it starts on demand through D-Bus; or KeePassXC
with Secret Service integration on), then check again.

## 2. The server and Pocket ID

Open the app (`voice-lab-desktop`). It opens on **Status**, which asks you to set it up:
**Settings…**, then the server (`https://voice-lab.<tailnet>.ts.net`) and the Pocket ID
issuer (`https://id.<tailnet>.ts.net`); the client stays `voice-lab-desktop`. Or write them
into `config.toml` (README "Settings"). `voice-lab-desktop --check-online` then confirms
that Pocket ID answers and that the server's door asks for a sign-in.

## 3. Sign in

Press **Sign in**. Your browser opens Pocket ID; sign in with your passkey and come back
to the app. If the browser says the redirect is not allowed, quit and start with
`--fixed-port` (it listens on `127.0.0.1:53683`). **Status** then shows the server's build,
its API versions and its gates.

## 4. Coming from voice-journey-capture

The old capture client kept its level pin, its cal file and its sessions in its own data
dir. Copy them once (nothing is moved or deleted; nothing is overwritten):

```sh
voice-lab-desktop --import-capture-data
```

Then `voice-lab-desktop --sessions` lists them, and **Uploads** sends what is still
waiting. A level pinned in the old client stays the same pin (not a new chain era).

## 5. A measurement session

`voice-lab-desktop --measure` opens the UMIK-1 window (phase 4b moves it into the main
window). The steps are voice-journey-capture's, unchanged:

1. Plug in the UMIK-1. Any other microphone is refused. On macOS set its format to 48,000 Hz
   in Audio MIDI Setup; on Linux the app opens it through ALSA `hw:` when PortAudio lists it,
   else through PipeWire, whose graph must run at 48 kHz (`pw-metadata -n settings 0
   clock.force-rate 48000`).
2. Set the input level deliberately and **Pin level** (`voice-lab-desktop --pin-level`).
   From then on a moved, muted or unreadable level blocks recording; **Re-pin…** records a
   chain era event, so do that only on purpose.
3. Import the cal file from miniDSP (the 0° file for its serial): **Calibration file** →
   **Import**, or `voice-lab-desktop --import-cal PATH`.
4. **Start session**, then Space for each segment (the timed ones stop by themselves); R redoes
   the last one; **Finish session**.

The session waits in the data dir and uploads when you are signed in; **Uploads** shows it
until the server accepts it.
