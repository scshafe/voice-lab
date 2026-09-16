# Native recording application

This Swift 6 package owns the native recording UI, persistent recorder helper,
and their local protocol clients. Python remains the authority for canonical
take evidence, decisions, analysis, and lab-package validation. The C11 target
is the allocation-free Audio Queue callback boundary, not another recorder.

The deployment floor is macOS 13. Only system frameworks are linked. Build and
test with `scripts/verify-native-recording`; SwiftPM output is directed to
`build/native-recording`. No Xcode project, package download, or full Xcode
installation is required by this harness.

The installed Command Line Tools omit XCTest. Unit tests use the installed
Swift Testing framework with explicit framework and library search paths. That
test framework requires macOS 14; tests run on the development Mac, and the
product binaries retain their independently inspected macOS 13 deployment floor.
The test build uses `build/native-recording-tests` so test-only runtime paths do
not enter product executables. macOS 13 runtime compatibility remains untested.

`RecordingHarness shell OUTPUT_DIRECTORY` hosts inert SwiftUI views through
AppKit without displaying a window or opening any audio device. It creates
synthetic light/dark PNGs and a geometry report at 1060×700 and 1440×980 logical
points, at 1×/2× scales. Images and reports go outside the repository. The
geometry probes measure the actual native hosted regions, including increased
script size and every shell transport state. This is an AppKit hosting harness;
it is not XCUI or a physical microphone acceptance test.

The app and helper are incrementally implemented. The initial shell explicitly
disables capture until the native controller and readiness gate are connected.
The helper owns capture over bounded inherited pipes, with a separate coalescing
telemetry channel. `RecorderClient` and `RecordingServiceClient` validate exact
build identities and reliable event sequencing. Connect performs the handshake;
call `snapshot()` before applying reconnect state. Apply state from guarded
`events`; direct replies also carry deterministic replay acknowledgments.

`scripts/verify-native-recording --suite bridge` launches the real clients,
native helper, and Python service against private synthetic fixtures. It checks
exact capture/sealing, blocked telemetry, shared Python/native ownership, process
loss, malformed frames, and reconnect. The service foundation reports unavailable
durable mutations explicitly when no context is configured. With its canonical
take backend, it persists independent reservations, sealed drafts, and explicit
keep decisions. Reservation replies provide Python's canonical digest and bind
the helper generation separately from the service generation. Indexed history
and navigation attach to that authority in the subsequent increments. No preview
is installed and no real audio device is opened by these checks.
