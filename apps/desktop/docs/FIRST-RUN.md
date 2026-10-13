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

## 4. Approve the home host (once per machine)

Sessions go to the PC through `/api/v1/intake` only under a standing approval you author on
this machine (D-021): it names the PC's endpoint, its tailnet node and the classes of what
may be sent, for at most 366 days, and every upload is written into this machine's ledger
before its first byte. **Uploads** says when it is missing. The library that holds it is
`~/.local/share/voice-lab-desktop/library` (Linux) or `~/Library/Application
Support/voice-lab-desktop/library` (macOS); open **Uploads** once and the app creates it.
The node's stable ID comes from `tailscale status --json` (the peer whose `DNSName` is the
endpoint), never from a document:

```sh
LIB=~/.local/share/voice-lab-desktop/library      # macOS: ~/Library/Application Support/voice-lab-desktop/library
voice-lab home-host approve --asset-root "$LIB" \
  --approval-id pc-home-host-2026 \
  --endpoint voice-lab.<tailnet>.ts.net --tailnet-suffix <tailnet>.ts.net \
  --node-stable-id <StableID> --classes capture measurement \
  --approved-at <now, ISO-8601 with offset> --expires-at <at most 366 days later> \
  --approved-by Cole --statement "My recordings, to my own PC." --preview
# then the same arguments, without --preview, plus:
#   --reviewed-approval-sha256 <the printed approval_sha256> --i-approve-this-destination
```

The server in Settings must be `https://` that same endpoint. Before each upload the app
checks, through Tailscale, that the node answering for it is the approved one.

## 5. Coming from voice-journey-capture

The old capture client kept its level pin, its cal file and its sessions in its own data
dir. Copy them once (nothing is moved or deleted; nothing is overwritten):

```sh
voice-lab-desktop --import-capture-data
```

Then `voice-lab-desktop --sessions` lists them, and **Uploads** sends what is still
waiting. A level pinned in the old client stays the same pin (not a new chain era).

## 6. A measurement session

The **Measurement** screen (or `voice-lab-desktop --measure`, which opens on it). The steps
are voice-journey-capture's, unchanged. The mouth distance is in **Status → Settings**:
measure once from your lips to the tip of the UMIK-1 and keep it the same, since levels
change about 6 dB per doubling of distance. Each session records it, with the input level
and every segment's label and levels, and its package carries them to the PC:

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

## 7. Prompted recording

**Record → Prompts** is the prompt studio
([PROMPT-STUDIO.md](PROMPT-STUDIO.md)): the PC's catalog, every prompt's status and the
coverage, Continue Missing, and Record, Stop and Save (or Save & Next), one take at a time
through the app's PortAudio capture worker (D-039). It needs to reach the PC once, for the
catalog; after that it records offline too. Until spike S-1 has passed on this platform, the
Swift app stays the macOS path, and what you record here stays on this machine
(`corpus_sync = false`, [CORPUS-CLIENT.md](CORPUS-CLIENT.md)).

The rest of this section is phase 4c's session flow, which `prompts_mode = "sessions"` in
`config.toml` brings back until the studio retires it (D-042):

1. Choose the catalog: the built-in pilot catalog (prompt suite v2, or v1), or a
   prompt-suite JSON file named in `config.toml` (`catalog_file = "/path/to/suite.json"`).
   The corpus catalog, held-out evaluation and physical-test catalogs are refused here.
2. Choose the prompts. Every room tone of the catalog comes along, and its prompt says
   **Do not speak**. **Start session** needs the UMIK-1 plugged in.
3. On the screen, and never while you type in a field:
   - **R** records, and **R** again stops. The time counts the audio frames that reached
     the disk, not the clock.
   - **Space** plays the take under review, exactly as trimmed.
   - **Return** saves (keeps) it.
   - **Save & next** keeps it and moves to the next prompt that still has no kept take.
4. Drag the waveform's handles, or type the In and Out frames, to trim a take. The trim is a
   note kept beside the take: the recording itself never changes, and the package carries
   the take as recorded. **Remove** sets a take aside, and **Restore** brings it back.
5. **Finish session** once every prompt has a kept take. The app builds the capture package
   (one kept take per prompt) in `<data dir>/prompted/packages/`. The takes, their
   history, the trims and the capture evidence stay in `<data dir>/prompted/sessions/`.

**Uploads** lists the package as *held here* until you turn prompted uploads on, which you
do only after S-1 has passed on this platform (DECISIONS.md):

```toml
prompted_uploads = true
```

It then uploads as a `capture` package, which the home-host approval must cover (`--classes
capture measurement`, §4).
