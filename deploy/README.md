# Deployment assets

These files are reviewed templates, not an automatically active service.

## Mini intake

1. Initialize the explicit asset root with `voice-lab assets init`.
2. Complete a local fixture intake and idempotency test.
3. Install the exact built wheel **non-editably** into its final
   content-addressed path:

   ```text
   /Users/cole/.local/lib/cole-voice-lab/releases/<application-wheel-sha256>/
   ```

   Create the virtual environment at that final path so its console-script
   shebang cannot retain a temporary or checkout path. Install offline from the
   verified wheel bundle with `--no-index`, `--require-hashes`, and the bundle's
   hash-locked requirements file. Do not use `pip install -e`, a repository
   `.venv`, or any executable below `/Users/cole/Projects`.
4. Seal the installed executable against the independently verified application
   wheel digest:

   ```bash
   scripts/seal-mini-install \
     --release /Users/cole/.local/lib/cole-voice-lab/releases/<application-wheel-sha256> \
     --wheel /absolute/verified-bundle/cole_voice_lab-0.1.0.dev0-py3-none-any.whl \
     --expected-wheel-sha256 '<application-wheel-sha256>'
   ```

   The sealer rejects hardlinks, unsafe ownership/modes, a mismatched
   content-addressed directory, changed wheel bytes, and editable-install
   metadata. Release symlinks are forbidden except for `bin/python`,
   `bin/python3`, and versioned `bin/python3.X` launchers that resolve to the
   exact separately hashed interpreter. In particular, symlinks below
   `site-packages` are forbidden. The sealer also requires the installed
   `cole_voice_lab` package bytes to match the verified wheel exactly and seals
   every allowed file, directory, and launcher link in the complete release
   tree. It writes a strict canonical
   `cole-voice-lab.mini-install-receipt/v2` record and prints its SHA-256. Treat
   both printed lines as deployment evidence.
5. Create a dedicated log directory with mode `0700` and a private output
   directory with mode `0700`.
6. Render the LaunchAgent with the sealed receipt and its exact digest:

   ```bash
   scripts/render-launch-agent \
     --install-receipt /Users/cole/.local/lib/cole-voice-lab/releases/<application-wheel-sha256>/INSTALL_RECEIPT.json \
     --install-receipt-sha256 '<install-receipt-sha256>' \
     --asset-root /Users/cole/.local/share/cole-voice-lab \
     --log-directory /Users/cole/Library/Logs/cole-voice-lab \
     --output /absolute/private-output/com.scshafe.cole-voice-lab.intake.plist
   ```

   The renderer refuses an unsealed release and copies a mode-`0500` durable
   launcher beside the mode-`0600` plist. On **every invocation**, that launcher
   re-hashes the receipt, installed console script, resolved Python runtime, and
   complete sealed release tree before it executes exactly
   `voice-lab intake scan --asset-root <root>`. A changed application module,
   added `.pth`/path-injection file, new executable, altered symlink, or any
   other unsealed tree entry fails closed. The launcher also removes inherited
   Python path/home/startup variables and disables user site-packages and bytecode
   writes. The LaunchAgent never invokes a mutable source checkout.

   This is a stable-state integrity check, not a sandbox against a malicious
   process already running as the same user. The launcher scans the release and
   then executes its console-script pathname; a same-UID writer can race that
   interval. Keep the sealed release quiescent and inaccessible to concurrent
   writers, or use an immutable filesystem snapshot if same-UID compromise is
   part of the deployment threat model.
7. Inspect the rendered `ProgramArguments`, then run its durable-launcher command
   in the foreground. Use the launcher's `--verify-only` flag first.
8. Install it as Cole's per-user LaunchAgent only after the foreground intake
   scan succeeds.

Any older rendered plist whose first argument is a repository `.venv` is stale
and must not be loaded. Version-1 mini install receipts are intentionally
unsupported. Preserve any v1 receipt as private evidence outside the release
tree, remove it from the release directory, reseal the verified installation
with the v2 sealer, and rerender the plist using the newly printed digest.
Rendering and linting do not bootstrap or enable the LaunchAgent.

The durable launcher is copied beside the rendered plist, so upgrading this
repository does not update an already rendered deployment. After adopting the
strict symlink policy, remove the old receipt from a preserved verified release,
rerun the v2 sealer against the exact wheel, and rerender the plist/launcher. A
normal venv containing only the validated `bin/python*` aliases can retain the
same tree digest; any other release symlink must be removed by rebuilding the
offline installation, not accepted by carrying forward its old receipt.

The service performs one short intake scan and exits. It is not a microphone or
long-running synthesis service.

## Restricted transfer lane

The committed SSH fragments deliberately contain placeholders. Before adding an
authorized key:

- install and pin an audited `rrsync` receiver (the reviewed template currently
  names Homebrew rsync **3.5.0** by its exact Cellar path; macOS
  `/usr/bin/rsync` does not supply it);
- generate the dedicated key on the MacBook Air;
- verify the mini host-key fingerprint out of band;
- confirm the Tailnet ACL only permits the capture host to reach the mini's SSH
  lane; and
- test denial of reads, arbitrary commands, PTY allocation, forwarding,
  traversal, and deletion.

The example receiver is write-only and passes `-no-del`. Treat successful denial
tests as a deployment prerequisite; the committed fragment is not an installed
or enabled SSH policy.

The locally installed 3.5.0 receiver reviewed on 2026-08-17 has SHA-256
`263d7bf7934442aa585e54152cf9ae8f93b01b1bd9719454deb4dc6f31b0bad8`.
Re-hash it before key installation; any formula upgrade requires re-reviewing
the exact path, digest, `-wo`, and `-no-del` behavior.

A local forced-command smoke on that exact file rejects arbitrary commands,
read (`--sender`) requests, `--delete`, and `..` traversal. This does not replace
the remaining end-to-end SSH denial tests for PTY allocation, forwarding,
source-address restriction, host identity, or Tailnet ACL enforcement.

Never substitute a shell `eval` wrapper around `SSH_ORIGINAL_COMMAND`.
