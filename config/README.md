# Configuration

Installed defaults live in `cole_voice_lab.resources` so wheels and source
checkouts behave identically.

`site.example.toml` is **documentation only**. There is no typed site-config
loader, and no `voice-lab` command reads either that file or the ignored
`.voice-lab.toml`. Copying or editing it does not change runtime behavior. Pass
the reviewed asset root, transfer destination, and policy choices explicitly to
each command. Do not rely on this example as a policy control.

Before using any documented SSH value, verify the mini's host-key fingerprint
out of band. Keep private keys and credentials outside this repository.

The pending consent template deliberately cannot authorize synthesis. Cole must
complete the explicit consent command before a human-reference bake-off runs.
