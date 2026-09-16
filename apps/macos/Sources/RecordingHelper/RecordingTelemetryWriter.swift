import Darwin
import Foundation
import RecordingContracts

/// One pending framed write plus one newest snapshot. EAGAIN can replace only a
/// not-yet-started frame; partial frames are completed before the next snapshot.
final class RecordingTelemetryWriter {
    private let descriptor: Int32
    private var pending = Data()
    private var offset = 0
    private var latest: Data?

    init(descriptor: Int32) throws {
        self.descriptor = descriptor
        guard fcntl(descriptor, F_SETFL, fcntl(descriptor, F_GETFL) | O_NONBLOCK) == 0,
              fcntl(descriptor, F_SETNOSIGPIPE, 1) == 0 else {
            throw NativeTelemetryError.configuration
        }
    }

    func offer(_ payload: Data) throws {
        latest = try RecordingFrameCodec.encode(payload, maximumBytes: RecordingFrameCodec.telemetryMaximumBytes)
        try flush()
    }

    func offerFrame(_ frame: Data) throws {
        guard frame.count > 4, frame.count <= RecordingFrameCodec.telemetryMaximumBytes + 4,
              frame.prefix(4).reduce(0, { $0 << 8 | Int($1) }) == frame.count - 4 else {
            throw NativeTelemetryError.configuration
        }
        latest = frame
        try flush()
    }

    func flush() throws {
        if pending.isEmpty, let latest {
            pending = latest
            self.latest = nil
            offset = 0
        }
        while offset < pending.count {
            let count = pending.withUnsafeBytes { bytes in
                Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), pending.count - offset)
            }
            if count < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK { return }
                if errno == EINTR { continue }
                throw NativeTelemetryError.closed
            }
            guard count > 0 else { throw NativeTelemetryError.closed }
            offset += count
        }
        pending.removeAll(keepingCapacity: true)
        offset = 0
    }
}

enum NativeTelemetryError: Error { case configuration, closed }
