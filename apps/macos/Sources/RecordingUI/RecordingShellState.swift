import Foundation

/// Native workspace transport phases; durable wire values are projected by RecordingWorkspaceModel.
public enum RecordingShellPhase: String, CaseIterable, Sendable {
    case ready, starting, recording, stopping, draftReview, saving, recoverableError

    public var status: String {
        switch self {
        case .ready: "Choose a prompt to record"
        case .starting: "Starting microphone…"
        case .recording: "Recording"
        case .stopping: "Finishing take…"
        case .draftReview: "Draft · ready to review"
        case .saving: "Saving take…"
        case .recoverableError: "Capture unavailable · recovery required"
        }
    }
}

/// Synthetic-only display input without file paths, devices, or live audio.
public struct RecordingShellState: Sendable {
    public var phase: RecordingShellPhase
    public var scriptSize: CGFloat
    public var focusMode: Bool
    public var roomTone: Bool

    public init(
        phase: RecordingShellPhase = .ready,
        scriptSize: CGFloat = RecordingLayout.scriptTextSize,
        focusMode: Bool = false,
        roomTone: Bool = false
    ) {
        self.phase = phase
        self.scriptSize = scriptSize
        self.focusMode = focusMode
        self.roomTone = roomTone
    }
}
