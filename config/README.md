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

There is no consent template or consent command any more (D-050): Cole's standing
decision permits his own voice for evaluation, and only references of the pinned
subject (`cole-self`) can be published. The bake-off's candidate registry and model-source
lock (`candidates.toml`, `model-sources.lock.json`) were retired with its runtimes (D-057).
