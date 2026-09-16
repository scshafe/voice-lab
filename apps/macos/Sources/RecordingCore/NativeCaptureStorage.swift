import Darwin
import Foundation

/// Canonical read-only storage readiness shared by the workspace and actual writer.
public enum NativeCaptureStorage {
    public static func preflight(assetRoot: URL, maximumFrames: UInt64 = 28_800_000) throws {
        let descriptor = try openPrivateRoot(assetRoot)
        defer { Darwin.close(descriptor) }
        try requireAvailable(descriptor: descriptor, maximumFrames: maximumFrames)
    }

    static func openPrivateRoot(_ root: URL) throws -> Int32 {
        guard root.path.hasPrefix("/") else { throw NativeCaptureFailure("RECORDING.DISK_SPACE", "Capture asset root must be absolute") }
        var opened = Darwin.open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard opened >= 0 else { throw NativeCaptureFailure.system("open filesystem root") }
        do {
            for component in root.path.split(separator: "/") {
                let next = Darwin.openat(opened, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard next >= 0 else { throw NativeCaptureFailure.system("open confined asset root") }
                Darwin.close(opened); opened = next
            }
            var info = stat()
            guard fstat(opened, &info) == 0, info.st_mode & S_IFMT == S_IFDIR, info.st_mode & 0o077 == 0 else {
                throw NativeCaptureFailure("RECORDING.DISK_SPACE", "Capture asset root must be a private directory")
            }
            return opened
        } catch { Darwin.close(opened); throw error }
    }

    static func requireAvailable(descriptor: Int32, maximumFrames: UInt64) throws {
        guard (1...28_800_000).contains(maximumFrames) else { throw NativeCaptureFailure("RECORDING.DISK_SPACE", "Maximum capture frame reservation is invalid") }
        var space = statvfs()
        guard fstatvfs(descriptor, &space) == 0 else { throw NativeCaptureFailure.system("read available storage") }
        let available = UInt64(space.f_bavail).multipliedReportingOverflow(by: UInt64(space.f_frsize))
        let required = maximumFrames * 3 + 256 * 1024 * 1024
        guard !available.overflow, available.partialValue >= required else {
            // Name the actual and required amounts: an unexplained refusal to record is
            // indistinguishable from a bug, and the operator cannot act on it.
            let actual = available.overflow ? 0 : available.partialValue
            throw NativeCaptureFailure(
                "RECORDING.DISK_SPACE",
                "Insufficient private storage: \(actual / (1024 * 1024)) MiB available, \(required / (1024 * 1024)) MiB required for the maximum take and its 256 MiB reserve"
            )
        }
    }
}
