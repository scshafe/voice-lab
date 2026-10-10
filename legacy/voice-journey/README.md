# legacy/voice-journey (phase 3, being emptied)

voice-journey's Node tree, imported with its history in phase 3 (design
[§5.1](../../docs/design/VOICE-PLATFORM.md#51-repository-strategy), D-030) and ported to
Python module by module ([PHASE-3-LOG.md](../../docs/design/PHASE-3-LOG.md)): the pipeline is
`cole_voice_lab.journey`, the job runner `cole_voice_lab.jobs`, the HTTP surface
`cole_voice_lab.api` and the web view `cole_voice_lab.view`; the mirror sender is
`apps/mirror/`. The pinned original for the parity harness is this directory at commit
`1f236b5` (`scripts/journey-parity`).

What remains is `src/capture.mjs` and its test, the AVFoundation capture path that
voice-journey's draft PR #1 retires once a real UMIK-1 session has been accepted. That PR is
carried here as the draft branch `phase3/chains-retire-capture` (design §5.3, "`chains.mjs`
(PR #1) ... Lands with PR #1's semantics"); merging it deletes this directory.

voice-journey's design documents moved to [docs/journey/](../../docs/journey/).
