# Recording core

Owns native device lifecycle, writer, sample clock, playback controller, and
local process clients. Imports strict shared contracts and the C11 callback
boundary. Audio devices stay closed until an explicit authorized command.

`NativeCaptureController` can record independent takes with a new device lifetime
for each Start. Stop writes the C atomic immediately and schedules device shutdown
on a separate executor from the ordered file writer. Successful disposal and
writer drain precede final WAV header, file sync, full-file SHA-256, exclusive
publication, directory sync, and the emergency recovery receipt. Python validates
that receipt and owns canonical lab persistence. Failed captures retain partials.

The C callback copies into preallocated bounded storage and checks native sample
timestamps. It performs no allocation, Swift callback, file I/O, DSP, or mutex wait.
The default raw ring holds 262,144 mono 24-bit frames (786,432 bytes). Overflow
fails capture; blocked envelope telemetry only drops visual updates. Device UID,
physical format, and running state are checked by the control executor. Startup
and maximum-duration deadlines remain independent of UI activity.

`SyntheticCaptureBackend` feeds the same C ring without creating an Audio Queue.
`scripts/verify-native-recording --suite capture` checks exact native-written WAVs
and Python receipt conformance. `--suite capture-sanitizers` runs C address/undefined
and thread sanitizers plus Swift address and thread lifecycle tests. These checks
do not establish microphone timing, physical continuity, or release performance.
