import AVFoundation

/// Inspecting status never requests permission. Only the explicit Record action calls request().
public enum RecordingMicrophonePermission: String, Sendable {
    case granted, notDetermined, denied, restricted

    public static func current() -> Self {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: .granted
        case .notDetermined: .notDetermined
        case .denied: .denied
        case .restricted: .restricted
        @unknown default: .restricted
        }
    }
    public static func request() async -> Bool {
        await AVCaptureDevice.requestAccess(for: .audio)
    }
}
