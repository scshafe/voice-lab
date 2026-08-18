# Cole Voice Lab

Private, local-first infrastructure for:

1. capturing a checksum-manifested voice corpus on a MacBook;
2. transferring it over the Tailnet into a fail-closed Mac mini intake;
3. building reference clips and versioned datasets without modifying raw audio;
4. running a reproducible, blind comparison of Qwen3-TTS, Chatterbox Turbo,
   Chatterbox multilingual V3, F5-TTS, and Seed-VC; and
5. exporting a hash-pinned handoff manifest for ChloeSpeech.

```text
MacBook capture -> checksum-bound READY package -> Tailnet landing zone
    -> verified immutable raw store -> references/derived datasets
    -> isolated candidate workers -> normalized outputs + metrics
    -> blind review -> promotion manifest -> ChloeSpeech
```

The Git repository contains only code, schemas, prompts, and policy. Recordings,
weights, consent evidence, runtime environments, and generated speech belong in
an explicit private asset root and are rejected by the repository guard.

## Development bootstrap

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e '.[dev]'
scripts/verify
```

## First pilot

The complete operator flow is in [docs/RUNBOOK.md](docs/RUNBOOK.md). The short
version is:

```bash
voice-lab doctor
voice-lab assets init --asset-root /Users/cole/.local/share/cole-voice-lab
voice-lab capture devices
voice-lab capture run --asset-root <macbook-private-root> \
  --session-id cole-pilot-001 --suite pilot --device '<device>'
voice-lab sync plan <session-directory>
voice-lab intake validate <mini-private-root>/incoming/cole-pilot-001
voice-lab intake accept --asset-root <mini-private-root> \
  --session-id cole-pilot-001
voice-lab reference --help
voice-lab models list
voice-lab models verify-registry
voice-lab audition --help
voice-lab blind --help
voice-lab promotion --help
```

`sync plan` prints a dry plan. A live transfer requires a separate `--live-plan`
preview, copy/paste approval of its exact `plan_sha256`, and all execution gates:
`--live-plan --execute --allow-network --approved-plan-sha256 <digest>`. The
destination is restricted to the primary mini Tailnet IP or its single reviewed
alias; arbitrary public hosts are not configurable. Model provisioning is
likewise a plan unless both `--execute --allow-downloads` are supplied.
Audition execution is offline, resumable, and requires its own `--execute`;
each selected runtime also requires the exact reviewed
`--runtime-receipt RUNTIME_ID=SHA256` binding printed by preparation. Completed
trials retain their runtime receipt and source-integrity preimages under the
experiment so status/blind/promotion remain verifiable after cache eviction.
candidate snapshots can only be evicted with an exact manifest digest and two
destructive confirmation flags.

The CLI authors intake, reference-review, blind-review, and handoff evidence,
and deeply validates a schema-authored promotion manifest. It does **not**
choose a winner or author the listening summary, verdict, winning-run, or
promotion manifest; those reviewed records remain explicit inputs at the
promotion boundary. `promotion handoff` writes one new canonical private JSON
file and never copies audio or mutates ChloeSpeech.

## Status

See [CURRENT.md](CURRENT.md) and [PROGRESS_CONTRACT.md](PROGRESS_CONTRACT.md).
