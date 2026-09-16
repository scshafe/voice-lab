import Foundation

/// Bounded big-endian framing shared by helper/service clients.
public struct RecordingFrameCodec: Sendable {
    public static let controlMaximumBytes = 1_048_576
    public static let telemetryMaximumBytes = 262_144
    private let maximumBytes: Int
    private var buffer = Data()

    public init(maximumBytes: Int = Self.controlMaximumBytes) {
        precondition(maximumBytes > 0 && maximumBytes <= Self.controlMaximumBytes)
        self.maximumBytes = maximumBytes
    }

    /// Encode one complete payload; empty and oversized messages fail closed.
    public static func encode(_ payload: Data, maximumBytes: Int = controlMaximumBytes) throws -> Data {
        guard !payload.isEmpty, payload.count <= maximumBytes else {
            throw RecordingContractError(code: "RECORDING.FRAME_SIZE", message: "Frame payload is empty or exceeds its bound")
        }
        var count = UInt32(payload.count).bigEndian
        var data = withUnsafeBytes(of: &count) { Data($0) }
        data.append(payload)
        return data
    }

    /// Consume arbitrary pipe chunks without allocating an unbounded pending frame.
    public mutating func append(_ chunk: Data) throws -> [Data] {
        var frames: [Data] = []
        var offset = chunk.startIndex
        while offset < chunk.endIndex {
            let needed: Int
            if buffer.count < 4 { needed = 4 - buffer.count }
            else {
                let length = try expectedLength()
                needed = length + 4 - buffer.count
            }
            let count = min(needed, chunk.endIndex - offset)
            buffer.append(chunk[offset..<(offset + count)])
            offset += count
            if buffer.count >= 4 {
                let length = try expectedLength()
                if buffer.count == length + 4 {
                    frames.append(Data(buffer.dropFirst(4)))
                    buffer.removeAll(keepingCapacity: true)
                }
            }
        }
        return frames
    }

    /// EOF is only clean between frames; a partial prefix/body is a protocol error.
    public func finish() throws {
        guard buffer.isEmpty else {
            throw RecordingContractError(code: "RECORDING.FRAME_TRUNCATED", message: "Control channel ended in a partial frame")
        }
    }

    private func expectedLength() throws -> Int {
        let length = buffer.prefix(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard length > 0, length <= maximumBytes else {
            throw RecordingContractError(code: "RECORDING.FRAME_SIZE", message: "Declared frame length is empty or exceeds its bound")
        }
        return Int(length)
    }
}
