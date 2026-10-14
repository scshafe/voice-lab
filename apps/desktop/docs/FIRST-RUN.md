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

## 4. Approve uploads to your PC (once per machine)

Nothing goes to the PC through `/api/v1/intake` until you approve it as the destination on
this machine (D-021): a standing approval that names the PC's endpoint, its Tailscale node
and what may be sent, for a year. Every upload is then written into this machine's ledger
before its first byte.

Press **Approve uploads to my PC**. Status always has it (the card *Uploads to the PC*);
Uploads shows it, and the studio's status line says *Uploads need your OK* with
**Approve**, whenever no approval stands. The app fills in everything:

- the endpoint: the host of the server in Settings, lowercased;
- the tailnet and the node: from Tailscale on this machine (`tailscale status --json`, the
  peer whose `DNSName` is that endpoint, and `whois` for its stable ID). The node must carry
  `tag:voice-lab`. If Tailscale is not running, knows no such peer, or the peer is not
  tagged, the app says why and approves nothing;
- `capture` and `measurement` (studio takes, prompted and measurement sessions), from now
  for 365 days, approved by the name your sign-in gives (else "Cole"), with the statement
  "My recordings, to my own PC.", under a new id.

One dialog shows the PC, the node, the classes and the expiry. Pressing **Approve** is your
review: the app writes exactly that approval, write-once, into its own library and binds it,
then syncs and uploads what waits. Pressing it again while an approval stands for the same PC
and node writes nothing and says so. An approval that has expired, names another PC or node,
lacks a class, or expires within 30 days is replaced by a new one (the old file stays;
Status and Uploads offer the renewal in its last 30 days). Before each upload the app still
checks, through Tailscale, that the node answering is the approved one.

The library is the app's own: `~/.local/share/voice-lab-desktop/library` (Linux) or
`~/Library/Application Support/voice-lab-desktop/library` (macOS), where its ledger lives too.
An approval made for the Swift app's library (`~/.local/share/cole-voice-lab`) does not count
here; that one serves only `voice-lab corpus-import`
([RECORDING-V1.md](../../../docs/design/RECORDING-V1.md) §7).

**Headless** (no window, the same record): preview, review the printed digest, then approve
with it.

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

The node's stable ID comes from `tailscale status --json` (the peer whose `DNSName` is the
endpoint), never from a document. The server in Settings must be `https://` that same
endpoint.

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

It then uploads as a `capture` package, which the home-host approval must cover (an
approval made with **Approve uploads to my PC** covers it; §4).
