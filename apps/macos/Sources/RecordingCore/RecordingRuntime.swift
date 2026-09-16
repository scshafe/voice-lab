import CaptureBuffer
import Foundation

/// Native build identity independent of capture device authorization.
public enum RecordingRuntime {
    public static let deploymentFloor = "13.0"
    public static let captureBufferABI = cole_capture_buffer_abi_version()
    public static let implementation = "cole-voice-lab.native-recording/v1"
}
