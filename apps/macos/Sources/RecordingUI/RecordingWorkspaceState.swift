import Foundation
import RecordingContracts
import RecordingCore

/// Coherent low-frequency workspace state. Sample telemetry lives separately in WaveformModel.
public struct RecordingWorkspaceState: Sendable {
    public init() {}
    public var phase: RecordingShellPhase = .ready
    public var prompt: RecordingPromptPresentation?
    public var selection: RecordingJSON = .null
    public var groupID: String?
    public var take: RecordingJSON = .null
    public var status = "Preparing recording workspace…"
    public var contextReady = false
    public var deviceReady = false
    public var storageReady = false
    public var serviceReady = false
    public var permission: RecordingMicrophonePermission = .notDetermined
    public var contextReason = "The recording library is opening"
    public var deviceReason = "Microphone discovery is pending"
    public var storageReason = "Private storage is being checked"
    public var historyCompleteness = "unknown"
    public var historyRevision: Int64 = 0
    public var coveredCount: Int64 = 0
    public var totalCount: Int64 = 0
    public var complete = false
    public var promptStatuses: [String: String] = [:]
    public var history: [RecordingJSON] = []
    public var historyCursor: String?
    public var promptCursor: String?
    public var activeOperationID: String?
    public var registrationPending = false
    public var navigationPending = false
    public var errorCode: String?
    /// A capture ended without a registered seal and its explicit service recheck has not
    /// replied. Record waits so it cannot race that recheck for capture ownership.
    public var unsealedRecheckPending = false

    public var takeID: String? { selection.object?["take_id"]?.string }
    /// Outside active capture, a reservation without a seal is neither a draft nor a take to act on.
    public var reservationUnsealed: Bool { !showsStop && take.object?["reservation"] != nil && (take.object?["seal"] ?? .null) == .null }
    public var decision: String? { take.object?["decision"]?.string }
    public var analysis: String? { take.object?["analysis"]?.string }
    public var sealedFrames: Int64? { take.object?["seal"]?.object?["audio"]?.object?["frames"]?.integer }
    public var showsStop: Bool { [.starting, .recording, .stopping].contains(phase) }
    public var canStop: Bool { [.starting, .recording].contains(phase) }
    public var canNavigate: Bool { phase == .ready && !navigationPending }
    public var canRecord: Bool {
        phase == .ready && prompt != nil && contextReady && deviceReady && storageReady && serviceReady &&
        [.granted, .notDetermined].contains(permission) && !navigationPending && !unsealedRecheckPending
    }
    public var recordUnavailableReason: String {
        if !serviceReady { return "Recording service is unavailable" }
        if !contextReady { return contextReason }
        if !deviceReady { return deviceReason }
        if !storageReady { return storageReason }
        if permission == .denied || permission == .restricted { return "Microphone permission is unavailable. Review macOS Privacy settings." }
        if unsealedRecheckPending { return "Checking the stopped take before the next recording" }
        if phase != .ready { return "Finish or save the current take first" }
        return navigationPending ? "Prompt selection is being saved" : "Ready to record"
    }
}
