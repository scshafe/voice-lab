import Foundation
import RecordingContracts
import RecordingCore

/// Where one Speak request stands, in the order the service reports it.
public enum RecordingSpeakPhase: String, Equatable, Sendable {
    case queued, running, finished, failed, cancelled

    public var isActive: Bool { self == .queued || self == .running }

    /// States only move forward: queued, running, then exactly one ending.
    fileprivate var rank: Int {
        switch self {
        case .queued: 1
        case .running: 2
        case .finished, .failed, .cancelled: 3
        }
    }
}

/// One entry of the model picker. `id` is what a request sends: "latest", a run id or "base".
public struct RecordingSpeakModelChoice: Identifiable, Equatable, Sendable {
    public static let latestID = "latest"
    public static let baseID = "base"
    public let id: String
    public let title: String
    /// The finished run whose recorded clips supply the reference: the model's own run, or
    /// for Latest and Base the newest finished run.
    public let referenceRunID: String
    /// That run's reference prompts, in catalog order; empty lets the service choose a clip.
    public let referencePromptIDs: [String]
}

/// One entry of the reference picker: a reference-candidate prompt of the chosen run.
public struct RecordingSpeakReferenceChoice: Identifiable, Equatable, Sendable {
    public let id: String
    public let title: String
}

/// The request this session sent last, as the service last reported it.
public struct RecordingSpeakRequest: Equatable, Sendable {
    public let operationID: String
    public let text: String
    public let modelID: String
    public let modelTitle: String
    /// The reference prompt the request named; nil let the service choose.
    public let referencePromptID: String?
    public var phase: RecordingSpeakPhase = .queued
    /// A cancel was sent and the request has not ended yet.
    public var cancelRequested = false
    /// The service's own words: a refused cancel while active, the failure once failed.
    public var message: String?
    public var errorCode: String?
    /// The clip a finished request spoke (its operation id).
    public var clipID: String?
}

/// A clip this session spoke. It stays on disk under `derived/speech/<operation id>/`.
public struct RecordingSpeakClip: Identifiable, Equatable, Sendable {
    /// The request's operation id.
    public let id: String
    public let text: String
    /// What the request sent: "latest", a run id or "base".
    public let modelID: String
    /// "Latest · <run id>", "<run id>" or "Base", naming the run that actually spoke.
    public let modelTitle: String
    public let runID: String?
    public let referenceRunID: String
    public let referencePromptID: String
    /// Relative to the asset root.
    public let relativePath: String
    public let sha256: String
    public let frames: Int64
    public let sampleRateHz: Int64
    public let seconds: Double

    public var durationText: String { RecordingSpeakModel.timeText(seconds: seconds) }

    /// A finished `SynthesisEvent`, already validated on the wire; nil for any other shape.
    init?(finished event: [String: RecordingJSON]) {
        guard event["state"] == .string("finished"), let id = event["operation_id"]?.string,
              let text = event["text"]?.string, let model = event["model"]?.string,
              let referenceRunID = event["reference_run_id"]?.string,
              let referencePromptID = event["reference_prompt_id"]?.string,
              let relativePath = event["relative_path"]?.string, let sha256 = event["sha256"]?.string,
              let frames = event["frames"]?.integer, let rate = event["sample_rate_hz"]?.integer,
              rate == Int64(RecordingPlaybackFormat.speech.sampleRate),
              let seconds = event["seconds"]?.number else { return nil }
        let runID = event["run_id"]?.string
        switch model {
        case RecordingSpeakModelChoice.baseID: modelTitle = "Base"
        case RecordingSpeakModelChoice.latestID:
            guard let runID else { return nil }
            modelTitle = "Latest · " + runID
        default: modelTitle = model
        }
        self.id = id
        self.text = text
        self.modelID = model
        self.runID = runID
        self.referenceRunID = referenceRunID
        self.referencePromptID = referencePromptID
        self.relativePath = relativePath
        self.sha256 = sha256
        self.frames = frames
        self.sampleRateHz = rate
        self.seconds = seconds
    }
}

/// A generation change VoiceOver announces: a request started, finished or failed. `sequence`
/// tells two equal texts apart.
public struct RecordingSpeakAnnouncement: Equatable, Sendable {
    public let sequence: UInt64
    public let text: String
}

/// What a Speak submit sends.
struct RecordingSpeakCommand: Equatable, Sendable {
    let operationID: String
    let text: String
    let model: String
    let referencePromptID: String?
    let nfeSteps: Int
}

/// The two service requests behind Speak; fixtures script their replies.
struct RecordingSpeakRequests {
    let submit: @MainActor (_ command: RecordingSpeakCommand, _ requestID: String) async throws -> RecordingWireEnvelope
    let cancel: @MainActor (_ operationID: String, _ cancelledOperationID: String) async throws -> RecordingWireEnvelope
}

/// @canonical speak -- type text, hear it in a fine-tuned voice.
/// Speak state beside the recording workspace: the text, the model and reference choices, the
/// request this session sent, this session's clips and their own playback transport. The
/// workspace owns it, routes the service's `synthesis` and `fine-tune-status` events to it,
/// and hands its playback controller to the capture interlock. Nothing here reads or writes
/// recording state.
@MainActor
public final class RecordingSpeakModel: ObservableObject {
    /// Unicode scalars per request (the service counts code points).
    public nonisolated static let textLimit = 2_000
    static let clipMemory = 200
    static let runMemory = 32

    public static let serviceUnavailableReason = "Recording service is unavailable"
    public static let channelClosedReason = "The recording service closed · restart the app to use Speak"
    public static let checkingReason = "Checking for a fine-tuned model"
    public static let noRuntimeReason = "Speak is not set up: this app has no F5 runtime to speak with"
    public static let noFinishedRunReason = "Fine-tune once before using Speak"
    public static let fineTuneActiveReason = "Speak is unavailable while a fine-tune runs"
    public static let recordingReason = "Speak is unavailable while recording"
    public static let savingReason = "Wait for the take to finish saving before using Speak"
    public static let generatingReason = "Speak is already generating"
    public static let cancellingReason = "Speak is being cancelled"
    public static let emptyTextReason = "Type the words to speak"
    public static let whitespaceTextReason = "Type words to speak, not only spaces"
    public static let hiddenCharacterReason = "Remove the hidden formatting or control character from the text"
    public static let noRequestReason = "No Speak request is running"
    /// A request's local failure when the service channel closed before it ended.
    public static let channelClosedFailure = "The recording service closed before Speak finished; restart the app to use Speak again"
    public static let playbackDuringCaptureReason = "Playback is unavailable while recording"
    public static let noClipReason = "No clip is selected"
    public static let clipLoadingReason = "The clip is loading"
    public static let clipSavingReason = "A clip is being saved"
    public static let noLibraryReason = "Speak clips need the recording library, which this workspace does not have"
    public static let generatingAnnouncement = "Generating speech"

    public static func tooLongReason(_ count: Int) -> String {
        "Shorten the text to \(textLimit.formatted()) characters (\(count.formatted()) now)"
    }

    /// Tabs are removed as they are typed or pasted (the service refuses them); CR and CRLF
    /// line ends become newlines.
    @Published public var text = "" {
        didSet { let normalized = Self.normalized(text); if normalized != text { text = normalized } }
    }
    /// "latest", a run id or "base"; a choice the run list no longer offers falls back to Latest.
    @Published public var selectedModelID = RecordingSpeakModelChoice.latestID {
        didSet { reconcileReference() }
    }
    /// A reference prompt of the chosen model's run; nil when the run offers none.
    @Published public var selectedReferencePromptID: String?
    /// The service's finished runs, newest first (at most 32).
    @Published public private(set) var runs: [RecordingFineTuneRunSummary] = []
    /// Whether the service answered a status request yet.
    @Published public private(set) var runsKnown = false
    /// Why the service could not list its finished runs (an unreadable run record), verbatim.
    @Published public private(set) var runsError: String?
    /// The service stopped supervising jobs (a restart recovers); its message.
    @Published public private(set) var supervisorFailure: String?
    @Published public private(set) var request: RecordingSpeakRequest?
    /// This session's clips, newest first.
    @Published public private(set) var clips: [RecordingSpeakClip] = []
    @Published public private(set) var selectedClipID: String?
    /// Why the selected clip cannot be played (it could not be read as authorized).
    @Published public private(set) var clipFailure: String?
    /// A playback action that was refused, until the next one succeeds.
    @Published public private(set) var playbackNotice: String?
    /// The last generation change to announce (the app posts it unless the session is synthetic).
    @Published public private(set) var announcement: RecordingSpeakAnnouncement?
    /// The clip a Save As is copying, while it runs.
    @Published public private(set) var savingClipID: String?
    /// How the last Save As ended: where the copy went, or why it was not saved.
    @Published public private(set) var saveNotice: String?
    @Published public private(set) var saveFailed = false
    var saveTask: Task<Void, Never>?
    private var announcementSequence: UInt64 = 0
    /// Speak's own transport; it yields to capture like the review transport.
    public let playback: RecordingPlaybackController
    weak var workspace: RecordingWorkspaceModel?
    /// Fixture-only transport; a configured workspace reaches its service.
    var requests: RecordingSpeakRequests?
    private var clipLoadGeneration: UInt64 = 0
    private var clipLoadTask: Task<Void, Never>?

    public init(backend: any RecordingPlaybackBackend) {
        playback = RecordingPlaybackController(backend: backend)
    }

    // MARK: text

    public var textCount: Int { text.unicodeScalars.count }

    /// Why the text cannot be sent as it is, or nil.
    public var textProblem: String? {
        if text.isEmpty { return Self.emptyTextReason }
        // Unicode White_Space, as Python's str.strip (Foundation's set also holds U+200B).
        if text.unicodeScalars.allSatisfy(\.properties.isWhitespace) { return Self.whitespaceTextReason }
        if textCount > Self.textLimit { return Self.tooLongReason(textCount) }
        // Mirrors the service: no format or control character except a newline.
        if text.unicodeScalars.contains(where: { scalar in
            scalar.properties.generalCategory == .format || (scalar.properties.generalCategory == .control && scalar != "\n")
        }) { return Self.hiddenCharacterReason }
        return nil
    }

    static func normalized(_ value: String) -> String {
        guard value.unicodeScalars.contains(where: { $0 == "\t" || $0 == "\r" }) else { return value }
        var result = String.UnicodeScalarView()
        var previousCarriageReturn = false
        for scalar in value.unicodeScalars {
            defer { previousCarriageReturn = scalar == "\r" }
            switch scalar {
            case "\t": continue
            case "\r": result.append("\n")
            case "\n" where previousCarriageReturn: continue
            default: result.append(scalar)
            }
        }
        return String(result)
    }

    // MARK: model and reference choices

    /// Latest (the newest finished run), the earlier finished runs newest first, then Base
    /// (the pinned base weights, with the newest run's reference). Empty without a finished run.
    public var modelChoices: [RecordingSpeakModelChoice] {
        guard let latest = runs.first else { return [] }
        var choices = [RecordingSpeakModelChoice(id: RecordingSpeakModelChoice.latestID, title: "Latest · " + latest.runID,
                                                 referenceRunID: latest.runID, referencePromptIDs: latest.referencePromptIDs)]
        // A run named like a keyword cannot be chosen by its id.
        for run in runs.dropFirst() where ![RecordingSpeakModelChoice.latestID, RecordingSpeakModelChoice.baseID].contains(run.runID) {
            choices.append(RecordingSpeakModelChoice(id: run.runID, title: run.runID, referenceRunID: run.runID,
                                                     referencePromptIDs: run.referencePromptIDs))
        }
        choices.append(RecordingSpeakModelChoice(id: RecordingSpeakModelChoice.baseID, title: "Base",
                                                 referenceRunID: latest.runID, referencePromptIDs: latest.referencePromptIDs))
        return choices
    }

    public var selectedModel: RecordingSpeakModelChoice? { modelChoices.first { $0.id == selectedModelID } }

    /// The chosen model's reference prompts, titled as the Library titles them.
    public var referenceChoices: [RecordingSpeakReferenceChoice] {
        (selectedModel?.referencePromptIDs ?? []).map { id in
            RecordingSpeakReferenceChoice(id: id, title: workspace?.prompts.first { $0.id == id }?.title ?? id)
        }
    }

    private func reconcileSelection() {
        if !modelChoices.contains(where: { $0.id == selectedModelID }) && selectedModelID != RecordingSpeakModelChoice.latestID {
            selectedModelID = RecordingSpeakModelChoice.latestID
        }
        reconcileReference()
    }

    private func reconcileReference() {
        let available = selectedModel?.referencePromptIDs ?? []
        if let chosen = selectedReferencePromptID, available.contains(chosen) { return }
        selectedReferencePromptID = available.first
    }

    // MARK: availability

    public var canSpeak: Bool { unavailableReason == nil }

    /// Why Speak cannot start now, first reason first: the service, the models, a fine-tune,
    /// recording or saving, a request already running, then the text.
    public var unavailableReason: String? {
        if let blocker = serviceBlocker { return blocker }
        if let blocker = modelBlocker { return blocker }
        if let workspace {
            if workspace.fineTune.isActive { return Self.fineTuneActiveReason }
            if workspace.state.showsStop { return Self.recordingReason }
            if workspace.state.phase == .saving || workspace.state.registrationPending { return Self.savingReason }
        }
        if let request, request.phase.isActive { return request.cancelRequested ? Self.cancellingReason : Self.generatingReason }
        return textProblem
    }

    public var isGenerating: Bool { request?.phase.isActive ?? false }

    public var canCancel: Bool { cancelUnavailableReason == nil }
    public var cancelUnavailableReason: String? {
        if let blocker = serviceBlocker { return blocker }
        guard let request, request.phase.isActive else { return Self.noRequestReason }
        return request.cancelRequested ? Self.cancellingReason : nil
    }

    private var serviceBlocker: String? {
        guard let workspace, requests != nil || workspace.service != nil else { return Self.serviceUnavailableReason }
        if workspace.processChannelFailed { return Self.channelClosedReason }
        return supervisorFailure
    }

    private var modelBlocker: String? {
        guard runsKnown else { return workspace?.fineTune.availabilityError ?? Self.checkingReason }
        if workspace?.fineTune.runtimeConfigured == false { return Self.noRuntimeReason }
        if let runsError { return runsError }
        return runs.isEmpty ? Self.noFinishedRunReason : nil
    }

    /// One line about the request, or nil before the first one.
    public var statusLine: String? {
        if let supervisorFailure { return supervisorFailure }
        guard let request else { return nil }
        switch request.phase {
        case .queued, .running:
            if request.cancelRequested { return request.message ?? "Cancelling Speak" }
            if let message = request.message { return message }
            return request.phase == .queued ? "Generating · waiting to start" : "Generating…"
        case .finished:
            guard let clip = clips.first(where: { $0.id == request.clipID }) else { return "Spoken" }
            return "Spoken · \(clip.durationText) · \(clip.modelTitle)"
        case .failed: return request.message ?? "Speak failed"
        case .cancelled: return "Speak cancelled"
        }
    }

    public var needsAttention: Bool { supervisorFailure != nil || request?.phase == .failed }

    // MARK: requests

    func transport() -> RecordingSpeakRequests? {
        if let requests { return requests }
        guard let service = workspace?.service else { return nil }
        return RecordingSpeakRequests(
            submit: { command, requestID in
                try await service.synthesize(operationID: command.operationID, requestID: requestID, text: command.text,
                                             model: command.model, referencePromptID: command.referencePromptID,
                                             nfeSteps: command.nfeSteps)
            },
            cancel: { try await service.cancelSynthesis(operationID: $0, cancelledOperationID: $1) })
    }

    /// Send the text with the chosen model and reference. The request shows queued at once;
    /// the reply and the service's events move it on.
    public func speak() {
        guard canSpeak, let workspace, let transport = transport(), let model = selectedModel else { return }
        let operation = workspace.newOperation("speak")
        let reference = selectedReferencePromptID.flatMap { model.referencePromptIDs.contains($0) ? $0 : nil }
        let command = RecordingSpeakCommand(operationID: operation, text: text, model: model.id,
                                            referencePromptID: reference, nfeSteps: RecordingServiceClient.speechDefaultSteps)
        request = RecordingSpeakRequest(operationID: operation, text: text, modelID: model.id, modelTitle: model.title,
                                        referencePromptID: reference)
        announce(Self.generatingAnnouncement)
        Task { [weak self] in
            do {
                // The request's own events carry its operation id as their request id.
                let response = try RecordingWorkspaceModel.result(await transport.submit(command, "request-" + operation))
                self?.applySynthesisEvent(response.data)
            } catch { self?.refuse(operation, error) }
        }
    }

    /// A refused (or unanswered) submit ends only the queued state it created. A refusal
    /// because a job holds the GPU asks for the fine-tune status, so that job is shown.
    private func refuse(_ operation: String, _ error: Error) {
        guard var current = request, current.operationID == operation, current.phase == .queued else { return }
        current.phase = .failed
        current.cancelRequested = false
        current.errorCode = (error as? RecordingContractError)?.code ?? "RECORDING.INVALID_MESSAGE"
        current.message = RecordingWorkspaceModel.errorMessage(error)
        request = current
        announce(current.message)
        if current.errorCode == "FINETUNE.JOB_ACTIVE" { workspace?.scheduleFineTuneStatus() }
    }

    /// Ask the service to stop the request. A queued request's reply is its cancelled state; a
    /// running one shows as cancelling until the service reports it cancelled.
    public func cancel() {
        guard canCancel, let workspace, let transport = transport(), let target = request?.operationID else { return }
        request?.cancelRequested = true
        let operation = workspace.newOperation("cancel-speak")
        Task { [weak self] in
            do {
                let response = try RecordingWorkspaceModel.result(await transport.cancel(operation, target))
                self?.applySynthesisEvent(response.data)
            } catch {
                guard let self, var current = request, current.operationID == target, current.phase.isActive else { return }
                current.cancelRequested = false
                current.message = RecordingWorkspaceModel.errorMessage(error)
                request = current
            }
        }
    }

    /// Apply a `synthesis` event from a reply or the service stream; each arrives twice, and a
    /// background event can overtake the submit reply. Only the shown request's events apply,
    /// states only move forward, and an ended request is final, so applying an event again
    /// changes nothing. A finished request adds its clip and selects it.
    public func applySynthesisEvent(_ event: RecordingJSON) {
        guard let object = event.object, object["event"] == .string("synthesis"),
              let operation = object["operation_id"]?.string, var current = request, current.operationID == operation,
              current.phase.isActive, let next = object["state"]?.string.flatMap(RecordingSpeakPhase.init(rawValue:)),
              next.rank > current.phase.rank else { return }
        current.phase = next
        // A notice stays until the request moves on.
        current.message = nil
        switch next {
        case .queued, .running: break
        case .finished:
            current.cancelRequested = false
            guard let clip = RecordingSpeakClip(finished: object) else {
                current.phase = .failed
                current.errorCode = "RECORDING.INVALID_MESSAGE"
                current.message = "Speak finished without a playable clip"
                request = current
                announce(current.message)
                return
            }
            current.clipID = clip.id
            remember(clip)
            request = current
            announce(Self.readyAnnouncement(seconds: clip.seconds))
            selectClip(clip.id)
            return
        case .failed:
            current.cancelRequested = false
            let error = object["error"]?.object
            current.errorCode = error?["code"]?.string
            current.message = error?["message"]?.string
            announce(current.message ?? "Speak failed")
        case .cancelled:
            current.cancelRequested = false
        }
        request = current
    }

    /// "Speech ready, 2.5 seconds": tenths, without a trailing ".0".
    public nonisolated static func readyAnnouncement(seconds: Double) -> String {
        let tenths = Int((max(0, seconds.isFinite ? seconds : 0) * 10).rounded())
        let amount = tenths % 10 == 0 ? String(tenths / 10) : String(format: "%ld.%ld", tenths / 10, tenths % 10)
        return "Speech ready, \(amount) \(tenths == 10 ? "second" : "seconds")"
    }

    private func announce(_ text: String?) {
        guard let text else { return }
        announcementSequence &+= 1
        announcement = RecordingSpeakAnnouncement(sequence: announcementSequence, text: text)
    }

    private func remember(_ clip: RecordingSpeakClip) {
        guard !clips.contains(where: { $0.id == clip.id }) else { return }
        clips.insert(clip, at: 0)
        if clips.count > Self.clipMemory { clips.removeLast(clips.count - Self.clipMemory) }
    }

    /// The service stream ended: the service went, and a request still running went with it.
    func serviceChannelClosed() {
        guard var current = request, current.phase.isActive else { return }
        current.phase = .failed
        current.cancelRequested = false
        current.errorCode = nil
        current.message = Self.channelClosedFailure
        request = current
        announce(current.message)
    }

    /// The service stopped supervising jobs: no request can end any more, and none can start.
    func supervisorFailed(_ message: String) {
        supervisorFailure = message
        guard var current = request, current.phase.isActive else { return }
        current.phase = .failed
        current.cancelRequested = false
        current.errorCode = nil
        current.message = message
        request = current
        announce(message)
    }

    // MARK: runs

    /// A `fine-tune-status` reply or event. `current`: requested under the fine-tune state the
    /// app shows now; an older reply only fills a list the app has never received.
    func applyRunList(_ event: [String: RecordingJSON], current: Bool) {
        guard current || !runsKnown else { return }
        let listed = (event["finished_runs"]?.array ?? []).compactMap(RecordingFineTuneRunSummary.init)
        runs = Array(listed.filter { $0.status == "finished" }.prefix(Self.runMemory))
        runsError = event["latest_run_error"]?.object?["message"]?.string
        runsKnown = true
        reconcileSelection()
    }

    /// A fine-tune this app showed finished: its run is now the newest.
    func learnFinishedRun(_ run: RecordingFineTuneRunSummary) {
        guard run.status == "finished" else { return }
        runs.removeAll { $0.runID == run.runID }
        runs.insert(run, at: 0)
        if runs.count > Self.runMemory { runs.removeLast(runs.count - Self.runMemory) }
        reconcileSelection()
    }

    // MARK: playback

    public var selectedClip: RecordingSpeakClip? { clips.first { $0.id == selectedClipID } }

    /// Load `id` into the player, stopped. While capture owns the audio transport the clip is
    /// loaded once capture ends (or when it is played).
    public func selectClip(_ id: String) {
        guard clips.contains(where: { $0.id == id }) else { return }
        selectedClipID = id
        clipFailure = nil
        playbackNotice = nil
        loadSelectedClip()
    }

    public var canPlay: Bool { playUnavailableReason == nil }

    public var playUnavailableReason: String? {
        guard let clip = selectedClip else { return Self.noClipReason }
        if playback.captureOwned || workspace?.state.showsStop == true { return Self.playbackDuringCaptureReason }
        if let clipFailure { return clipFailure }
        if let failure = playback.failure { return failure.message }
        guard playback.source?.takeID == clip.id, playback.canPlay else { return Self.clipLoadingReason }
        return nil
    }

    public func playPause() {
        guard canPlay else { return }
        Task { [weak self] in
            guard let self else { return }
            // One transport plays at a time: the review output pauses first.
            if let review = workspace?.review {
                do { try await review.playback.pause() } catch { review.report(error); return }
            }
            do { try await playback.playPause(); playbackNotice = nil }
            catch { playbackNotice = RecordingWorkspaceModel.errorMessage(error) }
        }
    }

    public func seek(to frame: Int64) {
        guard canPlay else { return }
        Task { [weak self] in
            guard let self else { return }
            do { try await playback.seek(to: frame); playbackNotice = nil }
            catch { playbackNotice = RecordingWorkspaceModel.errorMessage(error) }
        }
    }

    /// Recover the audio output after a playback failure, then load the selected clip again.
    public func recoverPlayback() {
        guard !playback.captureOwned else { return }
        Task { [weak self] in
            guard let self else { return }
            do {
                try await playback.recover()
                clipFailure = nil
                playbackNotice = nil
                loadSelectedClip()
            } catch { playbackNotice = RecordingWorkspaceModel.errorMessage(error) }
        }
    }

    /// A playback action outside this model (close, sleep) was refused or failed.
    func reportPlayback(_ error: Error) { playbackNotice = RecordingWorkspaceModel.errorMessage(error) }

    /// Why Record must wait for Speak: a failed Speak output, which capture may not follow.
    var captureBlocker: String? {
        playback.failure.map { "Speak playback failed: \($0.message) · recover audio on the Speak page" }
    }

    /// Capture released the audio transport: a clip that finished meanwhile is loaded now.
    func captureEnded() { loadSelectedClip() }

    private func loadSelectedClip() {
        guard let clip = selectedClip, playback.source?.takeID != clip.id, !playback.captureOwned else { return }
        guard let assetRoot = workspace?.reviewAssetRoot else {
            clipFailure = "Speak clips need the recording library, which this workspace does not have"
            return
        }
        let source: RecordingPlaybackSource
        do {
            source = try RecordingPlaybackSource(takeID: clip.id, sourceSHA256: clip.sha256, assetRoot: assetRoot,
                                                 relativePath: clip.relativePath, frameCount: clip.frames, format: .speech)
        } catch {
            clipFailure = RecordingWorkspaceModel.errorMessage(error)
            return
        }
        clipLoadGeneration &+= 1
        let token = clipLoadGeneration
        clipLoadTask = Task { [weak self] in
            guard let self else { return }
            do { try await playback.select(source, retainedRange: 0..<clip.frames) }
            catch {
                // A load superseded by capture or by another load recorded no failure.
                guard token == clipLoadGeneration, playback.failure != nil else { return }
                clipFailure = RecordingWorkspaceModel.errorMessage(error)
                // An unreadable clip is not an output failure; clear the transport so it
                // never holds capture back. A recovery that fails keeps its own failure.
                do { try await playback.recover() }
                catch { playbackNotice = RecordingWorkspaceModel.errorMessage(error) }
            }
        }
    }

    /// The window closed: a request still running ends with the service (no confirmation; it is
    /// in memory and short), and no clip load or save continues.
    func close() {
        clipLoadGeneration &+= 1
        clipLoadTask?.cancel()
        clipLoadTask = nil
        if var current = request, current.phase.isActive {
            current.phase = .cancelled
            current.cancelRequested = false
            request = current
        }
    }

    // MARK: Save As

    public var canSave: Bool { saveUnavailableReason == nil }

    public var saveUnavailableReason: String? {
        guard selectedClip != nil else { return Self.noClipReason }
        if savingClipID != nil { return Self.clipSavingReason }
        return workspace?.reviewAssetRoot == nil ? Self.noLibraryReason : nil
    }

    /// A file name for the clip's copy: its first words, lowercased and hyphenated.
    public nonisolated static func suggestedFileName(for clip: RecordingSpeakClip) -> String {
        var words: [String] = []
        var current = ""
        for character in clip.text.lowercased() {
            if character.isLetter || character.isNumber { current.append(character); continue }
            if !current.isEmpty { words.append(current); current = "" }
        }
        if !current.isEmpty { words.append(current) }
        var name = ""
        for word in words where name.count + word.count < 48 { name += (name.isEmpty ? "" : "-") + word }
        return (name.isEmpty ? "speech" : name) + ".wav"
    }

    /// Copy clip `id` to `destination` (a Save As choice): the clip is re-hashed as it is read,
    /// the bytes go to a temporary file beside the destination, which then atomically replaces
    /// it. The outcome is `saveNotice`; a copy of a changed clip is never kept.
    public func saveClip(_ id: String, to destination: URL) {
        guard canSave, let clip = clips.first(where: { $0.id == id }), let assetRoot = workspace?.reviewAssetRoot else { return }
        savingClipID = id
        saveNotice = nil
        saveFailed = false
        let maximumBytes = RecordingPlaybackFormat.speech.maximumFileBytes(frameCount: clip.frames)
        saveTask = Task { [weak self] in
            let outcome = await Task.detached(priority: .userInitiated) { () -> Result<Void, any Error> in
                Result {
                    try RecordingSpeakExport.copy(assetRoot: assetRoot, relativePath: clip.relativePath, sha256: clip.sha256,
                                                  maximumBytes: maximumBytes, to: destination)
                }
            }.value
            guard let self else { return }
            savingClipID = nil
            switch outcome {
            case .success: saveNotice = "Saved " + destination.lastPathComponent
            case .failure(let error):
                saveFailed = true
                saveNotice = RecordingWorkspaceModel.errorMessage(error)
            }
        }
    }

    // MARK: formatting

    /// `m:ss.s`, for durations and positions.
    public nonisolated static func timeText(seconds: Double) -> String {
        let tenths = Int((max(0, seconds.isFinite ? seconds : 0) * 10).rounded())
        return String(format: "%ld:%02ld.%ld", tenths / 600, (tenths / 10) % 60, tenths % 10)
    }

    public nonisolated static func timeText(frames: Int64) -> String {
        timeText(seconds: Double(frames) / RecordingPlaybackFormat.speech.sampleRate)
    }
}
