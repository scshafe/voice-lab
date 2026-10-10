# voice-lab mirror (macOS sender)

The Voice Memos mirror: it pushes the recordings in a Mac's Voice Memos container,
and the container's metadata database, into the voice-lab **memos drop** with a
write-only rsync (design `docs/design/VOICE-PLATFORM.md` §5.2 and §5.5). It runs
from launchd every 15 minutes on the Mac that syncs Voice Memos from iCloud: the Mac
mini today, the MacBook Air after the owner's iCloud Voice Memos sync.

It is a small Python package, `voice_lab_mirror`, that uses the standard library
only. It needs Python 3.12 or newer and no voice-lab install. It was ported from
voice-journey's `src/mirror.mjs` and `src/host-access.mjs`.

| Path | What |
| --- | --- |
| `src/voice_lab_mirror/mirror.py` | The mirror (`python -m voice_lab_mirror`) |
| `src/voice_lab_mirror/host_access.py` | The read-only corpus seam (`python -m voice_lab_mirror.host_access`) |
| `launcher.c` | The launcher that holds the Full Disk Access grant |
| `launchd/com.scshafe.voice-lab.mirror.plist.in` | The launchd job (a template) |
| `bin/voice-lab-mirror`, `bin/voice-lab-corpus` | Run either from a checkout, without installing |
| `tests/` | The tests (part of the repository's `pytest`; the launcher's tests need `cc`) |

## What a run does

The mirror reads the corpus only through the host-access seam: a directory listing,
`stat`, and read-only streams of bare `.m4a` names and `CloudRecordings.db` (with its
`-wal` and `-shm` sidecars). It never writes the container and never deletes anything
on the receiver: a recording deleted on the phone stays in the drop's history.

1. It lists the corpus and hashes (SHA-256) only the files whose size or mtime
   changed since the last run. The cache is in the state directory.
2. It snapshots the database files into `<state>/staging/`, so the bytes it hashes
   are the bytes it sends.
3. It rsyncs the recordings.
4. Only if that succeeded, it rsyncs the database snapshot plus
   `_mirror/manifest-<runId>.json` and `_mirror/latest.json`
   (`voice-journey-mirror-manifest/1`: size, mtime and SHA-256 per file). The
   manifest's arrival means the files it lists are complete; the receiver's ingest
   holds back a file whose size differs from it. A copy stays in `<state>/manifests/`.

Logs carry counts, never recording names. rsync's own diagnostics, which can quote
file names, are reduced to a line count, and an I/O error is logged by its error
number only.

```sh
apps/mirror/bin/voice-lab-mirror --dry-run      # the plan and both rsync lines; reads nothing
apps/mirror/bin/voice-lab-mirror --approval "<release-gate decision>" [--dest TARGET] [--ssh-key PATH]
```

Exit codes: `0` ok; `1` the corpus is unreadable, or another failure; `2` usage or
refused (a local destination, state inside the corpus, no `--approval`); `3` locked,
because another local run holds the lock or the receiver's rrsync drop is locked
("Another instance of rrsync is already accessing this directory"), so the next
period retries; `4` rsync failed (the next run retries).

## The target is configuration

Each setting is a flag, and each has an environment variable that the launchd plist
sets. Moving the mirror from the laptop's drop to the PC's is an edit of
`VOICE_LAB_MIRROR_DEST` in the plist and a reload. It needs no code change, no
rebuild and no new grant.

| Flag | Environment | Default |
| --- | --- | --- |
| `--dest` | `VOICE_LAB_MIRROR_DEST` | `vj-mirror@cole-laptop:` |
| `--ssh-key` | `VOICE_LAB_MIRROR_SSH_KEY` | none (the plist sets `~/.ssh/vj-mirror`) |
| `--rsync-bin` | `VOICE_LAB_MIRROR_RSYNC` | `/opt/homebrew/bin/rsync` |
| `--state-dir` | `VOICE_LAB_MIRROR_STATE` | `~/.local/state/voice-lab-mirror` |
| `--corpus-root` | `VOICE_LAB_MIRROR_CORPUS_ROOT` | `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings` |

The destination must be remote (`[user@]host:path` or `rsync://`), and it is relative
to the receiver's rrsync root: `vj-mirror@host:` is the drop itself. Never give it an
absolute `/srv/...` or `/data/...` path. The state directory must be outside the
corpus.

The rsync line, the same for both passes:

```text
/opt/homebrew/bin/rsync -rt --no-perms --no-owner --no-group --omit-dir-times \
  --chmod=D2750,F0640 --partial-dir=.rsync-partial --timeout=300 --no-motd \
  -e "ssh -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes \
      -o StrictHostKeyChecking=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 \
      -i ~/.ssh/vj-mirror" \
  --from0 --files-from=LIST  SRC/  vj-mirror@cole-laptop:
```

This works under the receiver's `rrsync -wo -no-del -munge` because the Mac only
pushes. It sends no `--delete*`, never pulls, and reads its file list locally.
`--chmod` is there because the Mac's files may be `0600` and the receiver reads them
through the `vj-mirror` group. Do not add `--delay-updates`: rrsync rewrites
`--partial-dir` to an absolute path, and rsync then silently discards the files and
still exits 0. The rsync is Homebrew's 3.x, by full path. macOS 26's `/usr/bin/rsync`
is openrsync (protocol 29), which is untested against rrsync. The receiver's side,
including the drop account, its single restricted key and the contract this side
follows, is in infra's `replication/voice-journey-mirror/README.md`.

## Full Disk Access

The Voice Memos group container is protected by TCC. A launchd job can read it only
if its **responsible process** holds a Full Disk Access grant. A command run from a
terminal inherits the terminal's grant, which is why a manual run works while the same
command under launchd is refused.

Granting Full Disk Access to Python would grant it to every Python script on the
Mac, and the grant binds to the exact binary, so every interpreter upgrade would break
it silently. Instead launchd runs `launcher.c`, a small compiled binary that never
needs to change, and the grant goes to that binary once. The launcher accepts exactly
one argument from a fixed allowlist, `mirror`. Any other argument, no argument, or an
extra argument exits 64 and starts nothing. For `mirror` it spawns, as a child (not
`exec`, which would give up its identity), the interpreter path compiled into it:
`<python> -I -m voice_lab_mirror --approval "<the standing approval>"`. It forwards
`SIGTERM`, `SIGINT` and `SIGHUP` to the child and exits with the child's status. The
environment from the plist passes through. `-I` keeps `PYTHONPATH` and the user site
out, so the module comes from the interpreter's own environment.

Recompiling the launcher changes its ad-hoc signature, so the grant has to be given
again. Nothing that changes routinely (the target, the key, the rsync binary, the
Python packages in the venv) requires a recompile.

## Install on a Mac (owner)

The receiver must exist first: the drop account and its key line (infra,
`replication/voice-journey-mirror`). The steps below use the Air's paths; on another
Mac only the host name in the key comment changes.

```sh
brew install rsync python@3.12                    # rsync 3.x at /opt/homebrew/bin/rsync
REPO=~/src/voice-lab                              # a checkout, used only to install from
VLM=~/.local/share/voice-lab-mirror
mkdir -p "$VLM/bin" ~/.local/state/voice-lab-mirror ~/Library/Logs

# 1. The package, in its own venv (the standard library only; pip fetches setuptools to build).
/opt/homebrew/bin/python3.12 -m venv "$VLM/venv"
"$VLM/venv/bin/python3" -m pip install "$REPO/apps/mirror"

# 2. The launcher, with the venv's interpreter compiled in, ad-hoc signed.
cc -O2 -Wall -Werror -DVL_MIRROR_PYTHON="\"$VLM/venv/bin/python3\"" \
  -o "$VLM/bin/voice-lab-mirror-launcher" "$REPO/apps/mirror/launcher.c"
codesign -s - -f -i com.scshafe.voice-lab.mirror-launcher "$VLM/bin/voice-lab-mirror-launcher"

# 3. Full Disk Access: System Settings > Privacy & Security > Full Disk Access > "+",
#    then Cmd-Shift-G, enter the launcher's path ($VLM/bin/voice-lab-mirror-launcher),
#    Add, and switch it on.

# 4. The drop's key: new, used for nothing else, without a passphrase (launchd runs it).
ssh-keygen -t ed25519 -N '' -f ~/.ssh/vj-mirror -C "vj-mirror@$(scutil --get LocalHostName)"
#    Give ~/.ssh/vj-mirror.pub (public) to the infra session that installs the
#    receiver's key line. ssh runs with StrictHostKeyChecking=yes, so the receiver's
#    host key must already be in ~/.ssh/known_hosts.

# 5. Check without reading anything, then the launchd job.
"$VLM/venv/bin/voice-lab-mirror" --dry-run --dest vj-mirror@cole-laptop: --ssh-key ~/.ssh/vj-mirror
sed -e "s|@LAUNCHER@|$VLM/bin/voice-lab-mirror-launcher|" \
    -e "s|@DEST@|vj-mirror@cole-laptop:|" \
    -e "s|@SSH_KEY@|$HOME/.ssh/vj-mirror|" \
    -e "s|@RSYNC@|/opt/homebrew/bin/rsync|" \
    -e "s|@STATE_DIR@|$HOME/.local/state/voice-lab-mirror|" \
    -e "s|@LOG@|$HOME/Library/Logs/voice-lab-mirror.log|" \
    "$REPO/apps/mirror/launchd/com.scshafe.voice-lab.mirror.plist.in" \
    > ~/Library/LaunchAgents/com.scshafe.voice-lab.mirror.plist
plutil -lint ~/Library/LaunchAgents/com.scshafe.voice-lab.mirror.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.scshafe.voice-lab.mirror.plist
launchctl kickstart gui/$(id -u)/com.scshafe.voice-lab.mirror     # the first run, now
tail ~/Library/Logs/voice-lab-mirror.log                          # counts only
```

**Retarget** (the PC cutover, §5.5: only after the PC's drop exists and the Air's key
is authorized there): change `VOICE_LAB_MIRROR_DEST` in the installed plist, then
`launchctl bootout gui/$(id -u)/com.scshafe.voice-lab.mirror` and `bootstrap` it
again. The hash cache stays valid: it only spares rehashing the Mac's files. What is
sent is decided by rsync's quick check against the receiver, so the first run to an
empty drop sends everything.

**Update the package** without touching the grant:
`"$VLM/venv/bin/python3" -m pip install --force-reinstall "$REPO/apps/mirror"`.

**Uninstall**: `launchctl bootout gui/$(id -u)/com.scshafe.voice-lab.mirror`, remove
the plist, `$VLM` and `~/.local/state/voice-lab-mirror`, and remove the launcher from
Full Disk Access. On the receiver, revoke the key (infra's README, "Check, revoke,
remove").

## Development

The tests run with the repository's suite (`pytest` collects `apps/mirror/tests`; its
`conftest.py` puts `src/` on `sys.path`), and `scripts/verify` runs strict mypy over
`apps/mirror/src`. The launcher's tests compile `launcher.c` with `cc` into a
temporary directory and skip without a compiler.

```sh
.venv/bin/mypy apps/mirror/src
.venv/bin/python -I -B -m pytest apps/mirror/tests --cov=voice_lab_mirror --cov-branch
```
