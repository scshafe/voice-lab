# First run

What you need: the miniDSP UMIK-1, its per-serial calibration file from miniDSP, and
the voice-journey server reachable on the tailnet. Install first (README "Install").

## 1. A keychain (Linux only)

The sign-in is kept only in the OS keychain. macOS always has one. On Linux, check:

```sh
uv run voice-journey-capture --check-online
```

If it says `keychain: NONE`, start a Secret Service provider (GNOME Keyring:
`sudo pacman -S gnome-keyring libsecret`, plus `exec --no-startup-id
gnome-keyring-daemon --start --components=secrets` in the i3 config; or KeePassXC with
Secret Service integration on), then check again. `--check-online` also confirms that
Pocket ID and the server answer (`intake asks for a sign-in`).

## 2. Sign in

Open the window (`uv run voice-journey-capture`) and press **Sign in**. Your browser
opens Pocket ID; sign in with your passkey, then come back to the window. If the
browser says the redirect is not allowed, quit and start with `--fixed-port` (it
listens on `127.0.0.1:53683`).

## 3. The UMIK-1

Plug it in. **Microphone** shows `Umik-1  Gain: 18dB` (press Refresh if not). Any
other microphone is refused: only the UMIK-1 is supported at launch.

- **macOS:** in Audio MIDI Setup, select the UMIK-1, set its format to 48,000 Hz, and
  set its input volume once to the level you will keep.
- **Linux:** the app opens the UMIK-1 directly (ALSA `hw:`) when PortAudio lists it,
  and then reads the card's capture control with `amixer`. Otherwise it goes through
  PipeWire, reads the source's volume with `pw-dump`, and needs PipeWire's graph at 48
  kHz (`pw-metadata -n settings 0 clock.force-rate 48000`).

## 4. Pin the input level

Set the level deliberately, then press **Pin level** (or run
`voice-journey-capture --pin-level`). From then on, a moved, muted or unreadable level
blocks recording. Moving it back to the pinned value unblocks it. **Re-pin…** records
the new level as a chain era event; do that only on purpose.

## 5. The cal file

Download the UMIK-1's calibration file for its serial number from miniDSP (the 0°
file, e.g. `7012345.txt`). Paste its path into **Calibration file** and press
**Import** (or run `voice-journey-capture --import-cal PATH`). The app keeps a copy
and sends its SHA-256 with each session. Without one, sessions are marked
uncalibrated (`calFileSha256: null`).

## 6. Record a session

Check the distance in Settings (35 cm, mouth to capsule) and stand in the same corner
as always. Turn on **94 dB calibrator** only if you have one. Press **Start session**
(it checks the level, the cal file and that the UMIK-1 delivers real audio). Then
press Space for each segment: the timed ones (room silence, calibrator) stop by
themselves; for the others, Space again stops. R redoes the last segment. When all
are done, press **Finish session**.

The session is saved locally and uploads straight away when you are signed in.
**Upload now** retries; every launch retries what is still pending.
`voice-journey-capture --sessions` lists each session's state. The server accepts the
whole session into `corpus/measurement/<sessionId>/` or nothing, and its watcher
picks it up from there.
