# Recording editor UX decisions

This records the treatment of the September 8, 2026 recording-editor feedback.
The implementation stays in the existing native desktop window and waveform
widget; capture, playback, raw preservation, consent, and model boundaries do
not change. Usage is in [Native recording library](DESKTOP.md).

The organizing principle is one visible recording context: the selected prompt
and its instructions, the selected take and its waveform, then an explicit
decision about that take. Visual improvements must not silently change audio,
save a draft trim, approve a different take, or open the microphone.

## 1. Optimize waveform and trimming

| Feedback item | Decision | Implementation and rationale |
| --- | --- | --- |
| 1a. Visually mute trimmed regions | Incorporated | Darker shading covers audio before In and after Out. The full original waveform remains visible, making the retained selection and excluded material distinguishable without deleting either. |
| 1b. Chunky, grabbable trim handles | Incorporated | Larger blue grips and a generous pointer target make both boundaries easier to grab. A handle drag changes trim bounds, not the playback cursor. |
| 1c. Interactive hover states | Incorporated | A vertical hover guide displays the original-timeline position to the millisecond. The pointer changes over editable grips, distinguishing trim dragging from seeking. |
| 1d. Put numeric inputs at their corresponding handles | Adapted | In stays on the left and Out on the right of the row immediately below the waveform. Values update with the grips, and numeric edits update the selection. The fields do not literally follow the handles: fixed positions preserve keyboard access and avoid collisions when bounds are close or near an edge. |
| 1e. Auto-apply trims, or provide a clearer save action | Incorporated using the explicit-save alternative | A solid blue **Save trim** button sits beside the numeric fields. Pending bounds affect playback preview immediately; saving persists the reversible edit. **Save & next** combines saving the current pending trim, keeping that take, and advancing. There is no background auto-save on drag, so an exploratory movement does not become a durable edit unexpectedly. |

Quiet recordings additionally use **Fit height** by default, with an explicit
display-only scale label. This enlarges the waveform vertically without changing
samples, playback gain, time coordinates, or files. It makes editing possible
at low recorded levels; it does not normalize audio or certify recording quality.

## 2. Streamline transport

| Feedback item | Decision | Implementation and rationale |
| --- | --- | --- |
| 2a. Larger, centralized playback controls | Incorporated | Return-to-trim-start, one-second skip backward, a larger play/pause button, one-second skip forward, and Stop sit together beneath the waveform. Seeking and skips are bounded by the retained selection. Native icons retain tooltips and accessible names. |
| 2b. Change action hierarchy between empty and review states | Incorporated | A ready or empty recording context emphasizes a large Record button. Recording emphasizes Stop recording. Review removes that prominent Record action and emphasizes the waveform and playback, with secondary **Retake** in the footer. Retake prepares another attempt; the operator must explicitly press Record to capture it. |
| 2c. Click to seek; double-click to play | Incorporated with transport-safe semantics | Clicking or dragging seeks. Double-clicking inside retained waveform audio seeks and starts or resumes preview; if already playing, it seeks and keeps playing instead of toggling to pause. Double-clicking a trim grip or excluded region does not start playback. |

The existing interlocks remain: recording and the takes list are unavailable
while playback is active or paused. Stop playback before recording or selecting
another take. A paused library-prompt change stops preview and remembers its cursor.
A playback-control fault blocks capture rather than risking simultaneous
speaker output and microphone recording. Double-click and Space never open the
microphone.

## 3. Make takes easy to compare

| Feedback item | Decision | Implementation and rationale |
| --- | --- | --- |
| 3a. Selectable Take 1 / Take 2 / Current tabs or list | Incorporated as a compact list | Takes are numbered, dated in local time, and accompanied by status. **Current** identifies the active take when applicable. Selecting a take loads its waveform without auto-playing; each take's playhead position is remembered while comparing in the open window and clamped to its retained bounds when loaded. A list supports additional takes without a growing tab row. |
| 3b. Place takes directly beside the waveform | Incorporated | The takes list is immediately above the waveform, within the editor card. It is expanded by default. Its disclosure remains available to reclaim space without moving the take selector to a distant panel. |

The selected history entry and loaded audio must both be the current pending
take before **Save & next** or current-take Retake can act. Selecting an earlier
take does not approve an unseen current take: the interface explains that the
Current entry must be selected again. Earlier takes can use their own Save trim
action. QC for the active review is not displayed as if it described a different
historical selection.

Changing takes with unsaved trim bounds still requires confirmation. Remembering
a playhead is an in-window navigation convenience, not a promise of persisted
playback position after reopening the app.

## 4. Strengthen visual hierarchy

| Feedback item | Decision | Implementation and rationale |
| --- | --- | --- |
| 4a. Prompt at the top in a subtle reference card | Incorporated | A palette-aware prompt card groups the recording target, state, large **Read aloud** script, and delivery guidance. Long scripts scroll inside the card. Room tone retains a separate **Do not speak** presentation, never a spoken transcript. |
| 4b. Cohesive middle editor including delivery instructions | Adapted | Takes, waveform, trim fields, playback controls, and relevant QC form one editor card. Delivery instructions deliberately remain with the script, labeled **Delivery guidance**. They describe how to perform the prompt, not an attribute of whichever historical waveform is selected; keeping them beside the script also preserves the distinction between speech and instruction. |
| 4c. Sticky bottom actions for Save & next / Retake / Delete | Incorporated | A fixed footer separates take/session decisions from transport. Save & next has primary blue emphasis; Retake and recoverable Delete are secondary. Session completion instead exposes Finalize session. Available actions continue to depend on the actual session, selected take, and playback state. |

## End-to-end review flow

The delivery footer was clarified after follow-up screenshot feedback: its earlier
"do not read aloud" wording appeared to contradict the Read aloud heading.
It now says **Delivery guidance**, and duration is explicitly approximate, not a
limit. Only the large script is spoken; metadata and corpus timing values are unchanged.

1. Select the prompt and read only the spoken script, or follow the explicitly
   silent room-tone instructions. Press Record when ready.
2. Press Stop after the take. The interface changes from recording controls to
   the take selector, full waveform, and central playback controls.
3. Drag the larger grips or edit In/Out to choose boundaries. Excluded material
   is shaded. This is manual editing: the feedback's proposed “snap to speech”
   is not implemented as automatic speech detection or inferred boundary changes.
4. Press Space or double-click retained audio to preview the pending selection.
   Compare earlier numbered takes if useful, then return to Current.
5. Stop playback and choose Save & next. It saves any pending trim, keeps that
   current take, and selects the next unrecorded prompt in catalog order, wrapping
   to earlier gaps. Existing usable takes across sessions, including room tone,
   are skipped. Unfinished batches remain available through Resume.
   The next prompt waits for an explicit Record action; it never starts the mic
   automatically.

Save trim is also available independently when the operator wants to edit without
advancing. Reset clears a saved trim and restores the complete original selection.
Delete remains confirmation-backed, recoverable exclusion from usable coverage;
Restore reverses that exclusion. Retakes preserve earlier audio. Trims remain
frame-exact derived copies with provenance, never overwrites of original audio.

## Explicit limits and verification

The adaptations are intentional: no silent trim auto-save, no numeric controls
that chase moving handles, no relocation of delivery instructions away from the
script, and no unreviewed speech-boundary snapping. All requested areas are
addressed; these variations preserve predictable editing and recording behavior.

Verification completed on September 8, 2026: `scripts/verify` passed all 1149 tests
with 91.44% branch-aware coverage, including 147 GUI and 62 waveform cases.
Synthetic offscreen light/dark screenshots were checked at 1060×700 and 1440×980.
Checks cover pending-trim previews, current-take approval guards, per-take cursor
memory, recoverable removal, playback/capture interlocks, and delivery-guidance
wording/accessibility across spoken and silent task states. No live microphone,
audible playback, or private recording was used for this UI verification.

There are no changes to training/model configuration, accepted-raw reference
selection, consent evidence, or transfer approvals in this UX increment.
