import Darwin
import Foundation

/// Native side of the shared Python/Qt flock boundary. The lock inode is never
/// removed; ownership lasts through input disposal and writer sealing.
public final class NativeCaptureOwnership {
    private let descriptor: Int32

    public init(assetRoot: URL) throws {
        let root = assetRoot.path
        guard let resolved = realpath(root, nil) else {
            throw NativeCaptureFailure.system("resolve ownership asset root")
        }
        let resolvedPath = String(cString: resolved)
        free(resolved)
        guard resolvedPath == root else {
            throw NativeCaptureFailure("RECORDING.OWNERSHIP_CONFLICT", "Capture asset root cannot contain symlinks")
        }
        var rootMetadata = stat()
        guard lstat(root, &rootMetadata) == 0, rootMetadata.st_mode & S_IFMT == S_IFDIR,
              rootMetadata.st_mode & 0o077 == 0 else {
            throw NativeCaptureFailure("RECORDING.OWNERSHIP_CONFLICT", "Capture asset root must be a private real directory")
        }
        var directory = root
        for component in ["provenance", "capture-desktop"] {
            directory += "/" + component
            if mkdir(directory, 0o700) != 0 && errno != EEXIST {
                throw NativeCaptureFailure.system("create ownership directory")
            }
            var metadata = stat()
            guard lstat(directory, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFDIR,
                  metadata.st_mode & 0o077 == 0 else {
                throw NativeCaptureFailure("RECORDING.OWNERSHIP_CONFLICT", "Capture ownership directory is not private and real")
            }
        }
        let path = directory + "/.capture.lock"
        let fd = open(path, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw NativeCaptureFailure.system("open capture ownership") }
        do {
            try Self.validate(fd, path: path)
            guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
                throw NativeCaptureFailure("RECORDING.OWNERSHIP_CONFLICT", "Another recorder owns this asset root")
            }
            try Self.validate(fd, path: path)
        } catch { Darwin.close(fd); throw error }
        descriptor = fd
    }

    private static func validate(_ descriptor: Int32, path: String) throws {
        var opened = stat()
        var current = stat()
        guard fstat(descriptor, &opened) == 0, lstat(path, &current) == 0,
              opened.st_mode & S_IFMT == S_IFREG, opened.st_nlink == 1,
              opened.st_mode & 0o077 == 0, opened.st_dev == current.st_dev,
              opened.st_ino == current.st_ino else {
            throw NativeCaptureFailure("RECORDING.OWNERSHIP_CONFLICT", "Capture lock must bind one private unchanged regular file")
        }
    }

    deinit { Darwin.close(descriptor) }
}
