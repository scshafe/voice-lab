import Darwin
import Foundation
import RecordingCore

/// Explicit synthetic fixture fault hook; never constructed for real microphone capture.
/// Waits only on the writer, with a two-second deadline and confined private FIFO.
/// The start barrier uses the same confinement under its own `start-` file names.
final class RecordingSyntheticSealBarrier: @unchecked Sendable {
    private let directory: Int32
    private let name: String
    private let lock = NSLock()
    private var consumed = false
    init(path: String, name: String = "seal") throws {
        self.name = name
        guard let resolved = realpath(path, nil) else { throw failure(name) }
        defer { free(resolved) }
        guard String(cString: resolved) == path else { throw failure(name) }
        let descriptor = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw failure(name) }
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFDIR, info.st_mode & 0o077 == 0 else { close(descriptor); throw failure(name) }
        directory = descriptor
    }
    deinit { close(directory) }
    func wait() throws {
        let reader = openat(directory, name + "-release.fifo", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard reader >= 0 else { throw failure(name) }; defer { close(reader) }
        var info = stat()
        guard fstat(reader, &info) == 0, info.st_mode & S_IFMT == S_IFIFO, info.st_mode & 0o077 == 0, info.st_nlink == 1 else { throw failure(name) }
        let keeper = openat(directory, name + "-release.fifo", O_WRONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard keeper >= 0 else { throw failure(name) }; defer { close(keeper) }
        let entered = openat(directory, name + "-entered", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard entered >= 0 else { throw failure(name) }
        var marker: UInt8 = 1
        guard write(entered, &marker, 1) == 1 else { close(entered); throw failure(name) }
        close(entered)
        var item = pollfd(fd: reader, events: Int16(POLLIN), revents: 0)
        guard poll(&item, 1, 2000) == 1, item.revents & Int16(POLLIN) != 0,
              read(reader, &marker, 1) == 1, marker == 1 else { throw failure(name) }
    }
    /// Gate only the first caller; later synthetic takes in this helper proceed unblocked.
    func waitOnFirstCall() throws {
        lock.lock(); let first = !consumed; consumed = true; lock.unlock()
        if first { try wait() }
    }
}

private func failure(_ name: String) -> NativeCaptureFailure {
    NativeCaptureFailure("RECORDING.WRITE_FAILED", "Explicit synthetic \(name) barrier is invalid or exceeded its two-second deadline")
}
