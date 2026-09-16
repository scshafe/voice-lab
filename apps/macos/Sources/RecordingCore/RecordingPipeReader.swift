import Darwin
import Foundation
import RecordingContracts

/// Bounded POSIX reads return available pipe bytes immediately. Foundation's
/// read(upToCount:) may wait to fill its requested count on persistent macOS pipes.
public struct RecordingPipeReader {
    private let descriptor: Int32
    private var buffer = [UInt8](repeating: 0, count: 65_536)

    public init(descriptor: Int32) { self.descriptor = descriptor }

    public mutating func next() throws -> Data? {
        while true {
            let count = buffer.withUnsafeMutableBytes { bytes in
                Darwin.read(descriptor, bytes.baseAddress, bytes.count)
            }
            if count == 0 { return nil }
            if count < 0 {
                if errno == EINTR { continue }
                throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Inherited process pipe read failed", context: ["errno": String(errno)])
            }
            return Data(buffer.prefix(count))
        }
    }
}
