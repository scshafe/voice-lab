import Darwin
import Foundation
import RecordingContracts
import RecordingCore

/// Persistent recorder process. Only an explicit validated Start opens input.
@main
struct RecordingHelper {
    static func main() {
        umask(0o077)
        do {
            let configuration = try RecordingHelperConfiguration(arguments: Array(CommandLine.arguments.dropFirst()))
            let session = try RecordingHelperSession(configuration: configuration)
            var codec = RecordingFrameCodec()
            var input = RecordingPipeReader(descriptor: STDIN_FILENO)
            do {
                while let chunk = try input.next() {
                    // The persistent main loop exits only with the process. Bound
                    // Foundation's request-validation temporaries to this read,
                    // while queued commands retain their validated Swift values.
                    try autoreleasepool {
                        for payload in try codec.append(chunk) { try session.receive(payload) }
                    }
                }
                try codec.finish()
            } catch {
                _ = session.parentLost()
                _exit(65)
            }
            _exit(session.parentLost() ? 0 : 70)
        } catch { _exit(78) }
    }
}
