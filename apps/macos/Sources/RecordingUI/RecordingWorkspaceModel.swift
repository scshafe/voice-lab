import Foundation
import RecordingContracts
import RecordingCore
import SwiftUI

/// Canonical UI transport binding. Durable decisions/navigation remain in Python;
/// Stop always targets the native helper directly, independent of service/QC work.
@MainActor
public final class RecordingWorkspaceModel: ObservableObject {
    @Published public internal(set) var state = RecordingWorkspaceState()
    @Published public var focusMode = false
    @Published public var scriptSize: CGFloat = RecordingLayout.scriptTextSize
    @Published public var searchText = ""
    @Published public var libraryFilter = "all"
    /// In-memory label filter (§6.4). Tokens absent from the loaded catalog are pruned at assignment,
    /// so a stale token can never hide every prompt; the catalog is fixed for a model's lifetime.
    @Published public var selectedLabels: Set<String> = [] {
        didSet { let known = selectedLabels.intersection(labelOptions); if known != selectedLabels { selectedLabels = known } }
    }
    public let waveform: RecordingWaveformModel
    public let review: RecordingReviewController
    @Published public internal(set) var reviewConfirmation: RecordingReviewConfirmation?
    @Published public var decisionReason = ""
    var reviewOperationID: String?
    var reviewTask: Task<Void, Never>?
    @Published public internal(set) var reviewDerivativeState: String?
    @Published public internal(set) var legacyReviewStatus: String?
    var deferredDerivativeEvent: RecordingJSON?
    var deferredLegacyReviewEvent: RecordingJSON?
    var legacyReviewOperation: (id: String, terminal: Bool)?
    var reviewAfterSaveIntent: RecordingReviewIntent?
    @Published public internal(set) var recoveryRows: [String: RecordingRecoveryRow] = [:]
    @Published public internal(set) var recoveryPagesLoaded = 0
    @Published public internal(set) var recoverySupervisorError: String?
    @Published public internal(set) var recoveryScanState = "scanning"
    @Published public internal(set) var recoveryScanGeneration: String?
    @Published public internal(set) var recoveryInventoryRevision: Int64 = -1
    @Published public internal(set) var recoveryPendingCount: Int64 = 0
    @Published public internal(set) var recoveryFindingCount: Int64 = 0
    @Published public internal(set) var recoveryRecoveredSealCount: Int64 = 0
    @Published public internal(set) var recoveryInventoryLoading = false {
        // A queued terminal recheck waits for the in-flight page rather than being dropped.
        didSet { if oldValue && !recoveryInventoryLoading && unsealedRecheckQueued { startUnsealedRecheck() } }
    }
    @Published public internal(set) var recoveryNextCursor: String?
    @Published public internal(set) var recoveryPreviousCursors: [String?] = []
    var recoveryPageCursor: String?
    var recoveryRequestGeneration: UInt64 = 0
    var recoveryRefreshRequested = false
    var recoveryExpectedRevision: Int64 = -1
    var recoveryBootstrapFinished = false
    var recoveryInventoryClosed = false
    @Published public var recoverySheetOpen = false
    @Published public internal(set) var packagePreparation: RecordingPackagePreparationStatus?
    @Published public internal(set) var packageCandidates: [RecordingPackageCandidate] = []
    @Published public internal(set) var packageSelectedTakeIDs: Set<String> = []
    @Published public internal(set) var packageCandidatesTruncated = false
    @Published public var packageSheetOpen = false
    var packageOperationID: String?
    /// Fixture-only catalog for package candidates; a configured workspace uses its own catalog.
    var packageFixtureCatalog: RecordingJSON?
    @Published public internal(set) var takeHistory: [RecordingJSON] = []
    @Published public internal(set) var takeHistoryLoading = false
    @Published public internal(set) var takeHistoryError: String?
    @Published public internal(set) var takeHistoryPromptID: String?
    @Published public internal(set) var takeHistoryNextCursor: String?
    @Published public internal(set) var takeHistoryPreviousCursors: [String?] = []
    var takeHistoryPageCursor: String?
    var takeHistoryGeneration: UInt64 = 0
    public var closeCancellationHandler: (@MainActor () -> Void)?
    public var closeHandler: (@MainActor () -> Void)?
    public private(set) var prompts: [RecordingPromptPresentation] = []
    public private(set) var transitions: [RecordingWorkspaceTransition] = []
    public var transitionObserver: ((RecordingWorkspaceTransition) -> Void)?
    public let configuration: RecordingApplicationConfiguration?
    /// Identities the transport paths bind replies to; fixtures carry synthetic values so the
    /// same paths run against scripted replies without a process.
    let catalogSHA256: String
    let recorderGeneration: String
    static let fixtureCatalogSHA256 = String(repeating: "a", count: 64)
    var reviewAssetRoot: URL? { configuration?.assetRoot ?? fixtureAssetRoot }
    private let recorder: RecorderClient?
    let service: RecordingServiceClient?
    let diagnosticSink: RecordingDiagnosticSink?
    private let permissionStatus: @Sendable () -> RecordingMicrophonePermission
    private let permissionRequest: @Sendable () async -> Bool
    private let beforeCapture: @MainActor () async throws -> Void
    private let afterCaptureStopped: @MainActor () -> Void
    private var consumers: [Task<Void, Never>] = []
    private var connected = false
    private var stopRequested = false
    var inputStartDispatched = false
    var startOperationID: String?
    private var stopOperationID: String?
    private var registerOperationID: String?
    private var saveOperationID: String?
    private var registeredSeal: String?
    private var cacheStates: [String: String] = [:]
    private var historyRefreshPending = false
    private var historyRefreshRequested = false
    private var queuedHistoryDiagnosticContext: RecordingDiagnosticContext?
    @Published public private(set) var historyRefreshInFlight = false
    /// Background results (analysis, inventory changes) merge into one refresh per window, so a
    /// burst costs one request that never queues ahead of the seal→review chain; each refresh
    /// then keeps at most one request in flight with one queued behind it.
    var backgroundRefreshWindow: Duration = .milliseconds(250)
    var scheduledHistoryRefresh: Task<Void, Never>?
    var scheduledRecoveryRefresh: Task<Void, Never>?
    @Published public internal(set) var audioReconnectRequired = false
    @Published public internal(set) var systemSleeping = false
    @Published public internal(set) var audioReconnectPending = false
    var audioReconnectCause: RecordingAudioReconnectCause = .systemSleep
    private var systemLifecycle: RecordingSystemLifecycle?
    /// Explicit single-take recheck transport; fixtures inject its reply without a service process.
    var recoverRequest: (@MainActor (_ operationID: String, _ takeID: String) async throws -> RecordingWireEnvelope)?
    /// Fixture-only transports for the seal→review chain and its background refreshes, in the same
    /// spirit: each replaces exactly one service request so sequencing tests can script and hold
    /// replies. A configured workspace never sets them.
    var registerSealRequest: (@MainActor (_ receipt: RecordingJSON, _ operationID: String, _ diagnosticContext: RecordingDiagnosticContext?) async throws -> RecordingWireEnvelope)?
    var reviewRequest: (@MainActor (_ takeID: String, _ selection: RecordingJSON, _ operationID: String, _ diagnosticContext: RecordingDiagnosticContext?) async throws -> RecordingWireEnvelope)?
    var historyRequest: (@MainActor (_ command: RecordingJSON, _ diagnosticContext: RecordingDiagnosticContext?) async throws -> RecordingWireEnvelope)?
    var recoverySnapshotRequest: (@MainActor (_ cursor: String?) async throws -> RecordingWireEnvelope)?
    /// Asset root of fixture review sources; a configured workspace resolves them under its own root.
    var fixtureAssetRoot: URL?
    var unsealedTerminal: RecordingUnsealedTerminal?
    var unsealedRecheckQueued = false
    var unsealedRecheckTask: Task<Void, Never>?
    /// Bounded takes this session saw end by explicit Stop before any audio arrived.
    var cancelledBeforeAudioTakeIDs: Set<String> = []
    /// The latest capture terminal's own words; background recovery progress never masks them.
    @Published public internal(set) var captureTerminalNotice: String?
    public static let cancelledBeforeAudioStatus = "Recording cancelled before audio arrived · nothing was captured"
    /// A channel consumer ended with an error: the channel closed (deadline, peer exit) or its
    /// stream stopped applying. No later terminal or recheck offers Record over it; only a
    /// restart does, so each press cannot reserve another take the recorder never starts.
    @Published public internal(set) var processChannelFailed = false
    public static let processChannelFailedReason = "A local recording channel closed · restart the app to record again"

    public init(configuration: RecordingApplicationConfiguration,
                permissionStatus: @escaping @Sendable () -> RecordingMicrophonePermission = { RecordingMicrophonePermission.current() },
                permissionRequest: @escaping @Sendable () async -> Bool = { await RecordingMicrophonePermission.request() },
                reviewBackend: any RecordingPlaybackBackend = AVFoundationRecordingPlaybackBackend(),
                diagnosticSink: RecordingDiagnosticSink? = nil,
                beforeCapture: @escaping @MainActor () async throws -> Void = {},
                afterCaptureStopped: @escaping @MainActor () -> Void = {}) throws {
        self.configuration = configuration
        catalogSHA256 = configuration.catalogSHA256; recorderGeneration = configuration.recorder.processGeneration
        self.diagnosticSink = diagnosticSink
        review = RecordingReviewController(backend: reviewBackend, diagnosticSink: diagnosticSink)
        recorder = RecorderClient(configuration: configuration.recorder)
        service = RecordingServiceClient(configuration: configuration.service, diagnosticSink: diagnosticSink)
        self.permissionStatus = permissionStatus; self.permissionRequest = permissionRequest
        self.beforeCapture = beforeCapture; self.afterCaptureStopped = afterCaptureStopped
        waveform = RecordingWaveformModel(takeID: "unselected", processGeneration: configuration.recorder.processGeneration, hostClockComparable: true)
        let purpose = configuration.catalog.object?["purpose"]?.string ?? ""
        prompts = try (configuration.catalog.object?["prompts"]?.array ?? []).map { try RecordingPromptPresentation($0, purpose: purpose) }
        state.permission = permissionStatus()
    }

    /// Coherent disabled workspace for missing or invalid launch configuration.
    public init(unavailable message: String) {
        configuration = nil; recorder = nil; service = nil; diagnosticSink = nil
        catalogSHA256 = Self.fixtureCatalogSHA256; recorderGeneration = "unconfigured"
        review = RecordingReviewController(backend: AVFoundationRecordingPlaybackBackend(mode: .offline))
        permissionStatus = { .denied }; permissionRequest = { false }
        beforeCapture = {}; afterCaptureStopped = {}
        waveform = RecordingWaveformModel(takeID: "unselected", processGeneration: "unconfigured", hostClockComparable: false)
        state.phase = .recoverableError; state.status = message; state.errorCode = "RECORDING.INVALID_MESSAGE"
    }

    /// Inert fixture adapter for the original shell harness; it cannot create process clients.
    /// A synthetic `catalog` replaces the one-prompt fixture so library tests can cover many prompts.
    public init(fixture: RecordingShellState, catalog: RecordingJSON? = nil) {
        configuration = nil; recorder = nil; service = nil; diagnosticSink = nil
        catalogSHA256 = Self.fixtureCatalogSHA256; recorderGeneration = "synthetic-generation"
        review = RecordingReviewController(backend: AVFoundationRecordingPlaybackBackend(mode: .offline))
        permissionStatus = { .denied }; permissionRequest = { false }
        beforeCapture = {}; afterCaptureStopped = {}
        waveform = RecordingWaveformModel(takeID: "synthetic-take", processGeneration: "synthetic-generation", hostClockComparable: false)
        focusMode = fixture.focusMode; scriptSize = fixture.scriptSize
        let raw = RecordingJSON.object(["prompt_id": .string(fixture.roomTone ? "quiet-room-tone" : "natural-reading"),
            "text": .string(fixture.roomTone ? "Remain still and quiet. Let the room sound settle naturally. Do not read these instructions aloud." : "The morning light moved slowly across the desk. I paused, took a breath, and began again. This is a synthetic interface fixture."),
            "category": .string(fixture.roomTone ? "room-tone" : "neutral"), "style": .string(fixture.roomTone ? "Stay quiet · avoid moving the microphone" : "Comfortable pace · natural delivery"), "expected_seconds": .number(20)])
        if let catalog {
            let purpose = catalog.object?["purpose"]?.string ?? ""
            prompts = (catalog.object?["prompts"]?.array ?? []).compactMap { try? RecordingPromptPresentation($0, purpose: purpose) }
            state.prompt = prompts.first
        } else if let prompt = try? RecordingPromptPresentation(raw, purpose: "capture-pilot") { prompts = [prompt]; state.prompt = prompt }
        state.phase = fixture.phase; state.status = fixture.phase.status
        state.deviceReason = "Synthetic fixture · microphone remains closed"
        state.selection = .object(["take_id": .string("synthetic-take"), "prompt_id": .string(prompts.first?.id ?? "natural-reading")])
    }

    /// Library rows (§6.4): search, status and labels combine with AND; selected labels with OR.
    /// Filters never reach Continue Missing, Save & Next or the covered count.
    public var filteredPrompts: [RecordingPromptPresentation] {
        prompts.filter { prompt in
            let searchMatches = searchText.isEmpty || prompt.searchHaystack.contains { $0.localizedCaseInsensitiveContains(searchText) }
            let status = state.promptStatuses[prompt.id] ?? "unknown"
            let statusMatches = libraryFilter == "all" || libraryFilter == status ||
                (libraryFilter == "attention" && ["unknown", "draft", "needs-review"].contains(status))
            let labelMatches = selectedLabels.isEmpty || !selectedLabels.isDisjoint(with: prompt.effectiveLabels)
            return searchMatches && statusMatches && labelMatches
        }
    }
    public var currentPromptFilteredOut: Bool { state.prompt.map { current in !filteredPrompts.contains { $0.id == current.id } } ?? false }
    /// Label filter options: the union of effective labels in first-appearance catalog order.
    public var labelOptions: [String] {
        var seen: Set<String> = []
        return prompts.flatMap(\.effectiveLabels).filter { seen.insert($0).inserted }
    }
    /// Menu title: "Labels: Any", up to two selected titles in option order, else the selected count.
    public var labelsMenuTitle: String {
        let selected = labelOptions.filter(selectedLabels.contains)
        switch selected.count {
        case 0: return "Labels: Any"
        case 1, 2: return "Labels: " + selected.map(RecordingPromptPresentation.labelTitle).joined(separator: ", ")
        default: return "Labels: \(selected.count) selected"
        }
    }
    public func setLabel(_ token: String, selected: Bool) {
        if selected { selectedLabels.insert(token) } else { selectedLabels.remove(token) }
    }
    public func showAllLabels() { selectedLabels.removeAll() }
    public var waveformReady: Bool {
        guard let snapshot = waveform.snapshot, let frames = selectedFrames,
              state.takeID == snapshot.takeID, snapshot.processGeneration == recorderGeneration else { return false }
        return snapshot.writtenFrames == frames && ((snapshot.firstFrame == 0 && snapshot.endFrame == frames) || selectedReview != nil)
    }
    public var canRecord: Bool { state.canRecord && !audioReconnectRequired && !processChannelFailed && review.playback.failure == nil }
    public var recordUnavailableReason: String {
        review.playback.failure?.message ?? (processChannelFailed ? Self.processChannelFailedReason : state.recordUnavailableReason)
    }
    public var canSave: Bool { (selectedReview == nil || selectedReview?.originalKind == "native-original") && state.phase == .draftReview && selectedDecision == "draft" && waveformReady && !state.registrationPending && !state.navigationPending && review.fieldError == nil }
    public var canContinueMissing: Bool { state.canNavigate && state.contextReady && state.historyCompleteness == "complete" }
    public var saveAdvances: Bool { state.historyCompleteness == "complete" }
    public var saveActionTitle: String { saveAdvances ? "Save & Next" : "Save" }
    public var saveUnavailableReason: String {
        if state.phase == .saving { return "The current Save is awaiting its durable acknowledgment" }
        if state.registrationPending { return "The sealed take is being registered" }
        if state.decision != "draft" { return "Select the current unsaved draft" }
        if !waveformReady { return "The matching stopped waveform is being prepared" }
        return saveAdvances ? "Save this take and select the next missing prompt" : "Save this take; the prompt stays selected while history is updating"
    }

    /// Launch inherited local channels and prepare a context without opening an input device.
    public func connect() async {
        guard !connected, let configuration, let recorder, let service else { return }
        connected = true
        consumeChannels(recorder: recorder, service: service)
        do {
            async let native: Void = recorder.connect()
            async let metadata: Void = service.connect()
            _ = try await (native, metadata)
            var updated = state; updated.serviceReady = true; publish(updated)
            _ = try result(await recorder.snapshot())
            let restored = try result(await service.snapshot())
            let revision = restored.data.object?["cursor"]?.object?["selection"]?.object?["selection_revision"]?.integer
            let operation = newOperation("context")
            let response = try result(await service.channel.request(configuration.openContextCommand(operationID: operation, expectedSelectionRevision: revision), takeID: nil, timeout: 30))
            try applyContext(response.data, operationID: operation)
            await refreshReadiness()
            // Persisted inventory replays pre-handshake notifications without rescanning.
            await refreshRecoveryInventory()
            recoveryBootstrapFinished = true
        } catch { fail(error, phase: .recoverableError) }
    }

    public func refreshReadiness() async {
        guard let configuration, let recorder else { return }
        var updated = state; updated.permission = permissionStatus(); publish(updated)
        async let storageResult: String? = Task.detached(priority: .userInitiated) {
            do { try NativeCaptureStorage.preflight(assetRoot: configuration.assetRoot); return nil }
            catch let error as NativeCaptureFailure { return error.message }
            catch { return "Private recording storage is unavailable" }
        }.value
        do {
            _ = try result(await recorder.channel.request(.object(["command": .string("prepare"), "device": configuration.captureDevice]), takeID: nil))
            updated = state; updated.deviceReady = true; updated.deviceReason = "Selected microphone is available"; publish(updated)
        } catch {
            updated = state; updated.deviceReady = false; updated.deviceReason = errorMessage(error); publish(updated)
        }
        let storageError = await storageResult
        updated = state; updated.storageReady = storageError == nil; updated.storageReason = storageError ?? "Private storage is ready"
        if state.phase == .ready { updated.status = updated.canRecord ? "Ready to record" : updated.recordUnavailableReason }
        publish(updated)
    }

    /// App-owned notifications are detached with the channels; fixtures may inject a local center.
    public func observeSystemLifecycle(center: NotificationCenter) {
        systemLifecycle?.close()
        systemLifecycle = RecordingSystemLifecycle(model: self, center: center)
    }

    public var canReconnectAudio: Bool {
        audioReconnectRequired && !systemSleeping && !audioReconnectPending &&
        [.ready, .recoverableError].contains(state.phase) && !state.registrationPending &&
        !state.navigationPending && !review.isDirty && !(state.decision == "draft" && state.sealedFrames != nil)
    }

    /// A fresh context follows an explicit gesture only after the prior draft is resolved.
    public func reconnectAudio() async {
        guard canReconnectAudio, let configuration, let service else { return }
        audioReconnectPending = true
        defer { audioReconnectPending = false }
        do {
            try await review.playback.recover()
            await refreshReadiness()
            guard state.deviceReady, !systemSleeping else { return }
            let operation = newOperation("reconnect-context")
            let revision = state.selection.object?["selection_revision"]?.integer
            let response = try result(await service.channel.request(configuration.openContextCommand(
                operationID: operation, expectedSelectionRevision: revision,
                newSetup: true, setupDescription: audioReconnectCause.contextDescription), takeID: nil, timeout: 30))
            guard !systemSleeping else { return }
            try applyContext(response.data, operationID: operation)
            audioReconnectRequired = false
            var updated = state; updated.errorCode = nil
            updated.status = updated.canRecord ? "Ready to record" : updated.recordUnavailableReason
            publish(updated)
        } catch { fail(error, phase: .recoverableError) }
    }

    /// Coalesced Record action. Permission is requested only here, following an explicit gesture.
    public func record() {
        if review.isDirty { reviewConfirmation = .discardTrim(.record); return }
        guard canRecord, let configuration, let recorder, let service, let groupID = state.groupID else { return }
        let operation = newOperation("record")
        startOperationID = operation; stopOperationID = nil; registerOperationID = nil; saveOperationID = nil
        registeredSeal = nil; stopRequested = false; inputStartDispatched = false; captureTerminalNotice = nil
        let selection = state.selection
        // Capture takes the transport from an automatic review that is still loading, e.g. just
        // after a prompt selection; that superseded load is never reported as a review failure.
        supersedeAutomaticReview()
        waveform.select(takeID: "pending-" + operation, processGeneration: configuration.recorder.processGeneration, hostClockComparable: true)
        var updated = state; updated.phase = .starting; updated.activeOperationID = operation; updated.status = "Starting microphone…"; updated.errorCode = nil; publish(updated)
        Task { [weak self] in
            guard let self else { return }
            // A start ending before its reservation leaves the same selection: show its take again.
            defer { if state.selection == selection, state.phase == .ready, selectedReview == nil { prepareCurrentReview() } }
            do {
                if state.permission == .notDetermined {
                    let granted = await permissionRequest()
                    updated = state; updated.permission = granted ? .granted : .denied; publish(updated)
                    guard granted else { throw RecordingContractError(code: "RECORDING.PERMISSION_DENIED", message: "Microphone permission was not granted") }
                }
                guard !stopRequested, startOperationID == operation else { cancelBeforeInput(); return }
                try await review.playback.beginCapture()
                try await beforeCapture()
                guard !stopRequested else { cancelBeforeInput(); return }
                let response = try result(await service.reserve(groupID: groupID, recorderGeneration: configuration.recorder.processGeneration, selection: selection, operationID: operation))
                guard startOperationID == operation else { return }
                guard let take = response.data.object?["take"], let reservation = take.object?["reservation"],
                      let takeID = reservation.object?["take_id"]?.string,
                      let digest = response.data.object?["reservation_sha256"]?.string,
                      let cursor = response.data.object?["cursor"] else { throw invalid("Reservation response requires its durable cursor and digest") }
                try applyCursor(cursor, preserveTake: true) { $0.take = take }
                waveform.select(takeID: takeID, processGeneration: configuration.recorder.processGeneration, hostClockComparable: true)
                guard !stopRequested else { cancelBeforeInput(); return }
                inputStartDispatched = true
                _ = try result(await recorder.start(reservation: reservation, reservationSHA256: digest))
            } catch { handleStartFailure(error) }
        }
    }

    /// A start rejected after input dispatch still leaves its reservation to the service.
    func handleStartFailure(_ error: Error) {
        guard inputStartDispatched else { captureStopped(); fail(error, phase: .ready); return }
        guard !currentTerminalResolved else { return }
        fail(error, phase: .recoverableError)
        if state.errorCode == "RECORDING.DEVICE_UNAVAILABLE" {
            captureStopped()
            captureTerminalNotice = state.status
            requireAudioReconnect(after: .deviceUnavailable)
        } else if let takeID = state.takeID {
            resolveUnsealedTerminal(takeID: takeID, writtenFrames: nil)
        }
    }

    public func stop() {
        guard state.canStop else { return }
        stopRequested = true
        var updated = state; updated.phase = .stopping; updated.status = "Finishing take…"; publish(updated)
        guard inputStartDispatched, let takeID = state.takeID, let recorder, registeredSeal == nil,
              state.take.object?["reservation"]?.object?["operation_id"]?.string == startOperationID else { return }
        let operation = stopOperationID ?? newOperation("stop"); stopOperationID = operation
        Task { [weak self] in
            guard let self else { return }
            do { _ = try result(await recorder.stop(takeID: takeID, operationID: operation)) }
            catch { handleUnsealedFailure(error, takeID: takeID) }
        }
    }

    /// A failed Stop request or seal registration is a terminal without a registered seal.
    func handleUnsealedFailure(_ error: Error, takeID: String, captureReleased: Bool = false) {
        guard state.takeID == takeID, !currentTerminalResolved else { return }
        fail(error, phase: .recoverableError)
        resolveUnsealedTerminal(takeID: takeID, writtenFrames: nil, captureReleased: captureReleased)
    }

    /// Keep current text and waveform until one matching durable Save response supplies its cursor.
    public func save() { save(advance: saveAdvances) }

    public func save(advance: Bool) {
        if review.isDirty { saveTrim(thenKeep: true, advance: advance); return }
        guard canSave, let service, let takeID = state.takeID, let frames = selectedFrames else { return }
        let operation = saveOperationID ?? newOperation("save"); saveOperationID = operation
        let selection = state.selection
        let decisionRevision = selectedReview?.decisionRevision ?? state.take.object?["decision_revision"]?.integer ?? 0
        let editRevision = selectedReview?.editRevision ?? state.take.object?["edit_revision"]?.integer ?? 0
        let range = selectedReview.map { $0.frameRange($0.retainedRange) } ?? state.take.object?["frame_range"] ?? .object(["source_frames": .integer(frames), "start_frame": .integer(0), "end_frame": .integer(frames)])
        var updated = state; updated.phase = .saving; updated.activeOperationID = operation; updated.status = "Saving take…"; publish(updated)
        Task { [weak self] in
            guard let self else { return }
            do {
                try await review.playback.shutdown()
                let response = try result(await service.save(selection: selection, operationID: operation, decisionRevision: decisionRevision, editRevision: editRevision, frameRange: range, advance: advance, waveformReady: true))
                guard state.activeOperationID == operation, state.takeID == takeID, state.selection == selection,
                      response.data.object?["operation"]?.object?["operation_id"] == .string(operation),
                      let cursor = response.data.object?["cursor"], let saved = response.data.object?["take"] else { return }
                try applyCursor(cursor, preserveTake: true) { updated in
                    updated.take = saved; updated.phase = .ready; updated.activeOperationID = nil
                    updated.status = cursor.object?["complete"] == .boolean(true) ? "All prompts recorded · saved" : (saved.object?["analysis"] == .string("verified") ? "Saved · verified" : "Saved · checking")
                }
                if let selected = selectedReview { try applyReviewMutation(.object(["take": saved]), previous: selected) }
                await refreshHistory()
            } catch { fail(error, phase: .draftReview) }
        }
    }

    public func selectPrompt(_ promptID: String) {
        if review.isDirty { reviewConfirmation = .discardTrim(.selectPrompt(promptID)); return }
        guard state.canNavigate, promptID != state.prompt?.id else { return }
        navigate(promptID: promptID)
    }
    public func continueMissing() {
        if review.isDirty { reviewConfirmation = .discardTrim(.continueMissing); return }
        guard canContinueMissing else { return }; navigate(promptID: nil)
    }

    private func navigate(promptID: String?) {
        guard let service else { return }
        let operation = newOperation("navigate"); let selection = state.selection
        var updated = state; updated.navigationPending = true; updated.activeOperationID = operation; publish(updated)
        Task { [weak self] in
            guard let self else { return }
            do {
                try await review.playback.shutdown()
                let response: RecordingWireEnvelope
                if let promptID { response = try result(await service.selectPrompt(promptID, selection: selection, operationID: operation)) }
                else { response = try result(await service.continueMissing(selection: selection, operationID: operation)) }
                guard state.activeOperationID == operation, state.selection == selection, let cursor = response.data.object?["cursor"] else { return }
                try applyCursor(cursor, preserveTake: false) { updated in
                    updated.navigationPending = false; updated.activeOperationID = nil
                    updated.status = cursor.object?["complete"] == .boolean(true) ? "All prompts recorded" : "Ready to record"
                }
                // An explicit prompt selection names its latest retained take; load it stopped.
                if promptID != nil { prepareCurrentReview() }
            } catch { updated = state; updated.navigationPending = false; publish(updated); fail(error, phase: .ready) }
        }
    }

    public func refreshHistory(diagnosticContext: RecordingDiagnosticContext? = nil) async {
        let span = RecordingDiagnosticSpan("history.refresh", context: diagnosticContext, sink: diagnosticSink)
        if historyRefreshPending {
            historyRefreshRequested = true
            if queuedHistoryDiagnosticContext == nil { queuedHistoryDiagnosticContext = span.childContext }
            span.finish(.coalesced); return
        }
        guard let request = historyTransport() else { span.finish(.superseded); return }
        historyRefreshPending = true
        if !historyRefreshInFlight { historyRefreshInFlight = true }
        var outcome = RecordingDiagnosticEvent.Outcome.success
        var requestID: String?
        var failure: (any Error)?
        defer { finishHistoryRefresh(); span.finish(outcome, error: failure, requestID: requestID) }
        do {
            let response = try result(await request(.object(["command": .string("history"), "catalog_sha256": .string(catalogSHA256), "cursor": .null, "prompt_cursor": .null, "group_cursor": .null, "page_size": .integer(200)]), span.childContext))
            requestID = response.meta.request_id
            let application = RecordingDiagnosticSpan("history.application", context: span.childContext?.request(response.meta.request_id), sink: diagnosticSink)
            let applied = applyHistory(response.data)
            application.finish(applied ? .success : .superseded)
        } catch {
            outcome = .failure; failure = error
            var updated = state; updated.historyCompleteness = "unknown"; updated.status = "History unavailable · capture readiness is separate"; publish(updated)
        }
    }

    private func consumeChannels(recorder: RecorderClient, service: RecordingServiceClient) {
        consume(recorder.channel.events) { $0.handleRecorder($1) }
        consume(recorder.channel.telemetry) { try $0.handleTelemetry($1) }
        consume(service.channel.events) { $0.handleService($1) }
    }

    /// A channel stream ends with an error only once its channel closed or stopped applying.
    /// A request failing on the same closure resumes separately; either order ends unavailable.
    @discardableResult
    func consume(_ stream: AsyncThrowingStream<RecordingWireEnvelope, Error>,
                 apply: @escaping @MainActor @Sendable (RecordingWorkspaceModel, RecordingWireEnvelope) throws -> Void) -> Task<Void, Never> {
        let consumer = Task { [weak self] in
            do { for try await event in stream { guard let self else { return }; try apply(self, event) } }
            catch { self?.processChannelFailed = true; self?.fail(error, phase: .recoverableError) }
        }
        consumers.append(consumer)
        return consumer
    }

    func handleRecorder(_ response: RecordingWireEnvelope) {
        guard response.meta.process_generation == (configuration?.recorder.processGeneration ?? waveform.processGeneration) else { return }
        let event = response.data.object?["event"]?.string
        // A resolved terminal applies once; a duplicate error cannot rename or re-alarm it.
        if response.error != .null, response.meta.take_id == state.takeID, !currentTerminalResolved {
            var updated = state
            updated.errorCode = response.error.object?["code"]?.string
            updated.status = response.error.object?["message"]?.string ?? "Native capture failed"
            publish(updated)
        }
        if event == "waveform-cache", let takeID = response.data.object?["take_id"]?.string {
            if cacheStates.count == 200, cacheStates[takeID] == nil { cacheStates.removeAll(keepingCapacity: true) }
            cacheStates[takeID] = response.data.object?["status"]?.string
            return
        }
        guard event == "recorder-state", response.meta.take_id == state.takeID,
              let takeID = state.takeID, state.take.object?["reservation"]?.object?["operation_id"]?.string == startOperationID else { return }
        let phase = response.data.object?["transport"]?.string
        if let seal = response.data.object?["seal"], seal != .null, let digest = seal.object?["original_sha256"]?.string,
           registeredSeal != digest, let register = registerSealTransport() {
            captureStopped()
            registeredSeal = digest
            let operation = registerOperationID ?? newOperation("register"); registerOperationID = operation
            var updated = state; updated.phase = .stopping; updated.registrationPending = true; updated.status = "Preparing draft…"; publish(updated)
            Task { [weak self] in
                guard let self else { return }
                let context = diagnosticSink == nil ? nil : RecordingDiagnosticContext(traceID: operation, operationID: operation, takeID: takeID)
                let registration = RecordingDiagnosticSpan("workspace.register_seal", context: context, sink: diagnosticSink)
                do {
                    let result = try result(await register(seal, operation, registration.childContext))
                    guard state.takeID == takeID, registeredSeal == digest, let take = result.data.object?["take"] else { registration.finish(.superseded, requestID: result.meta.request_id); return }
                    let application = RecordingDiagnosticSpan("workspace.register_application", context: registration.childContext?.request(result.meta.request_id), sink: diagnosticSink)
                    updated = state; updated.take = take; updated.registrationPending = false; updated.phase = .draftReview; updated.status = "Draft · checking"; updated.activeOperationID = nil; publish(updated)
                    application.finish()
                    registration.finish(requestID: result.meta.request_id)
                    // The review source is requested as soon as the sealed draft is applied; the
                    // history page refreshes concurrently and never gates the review (§12 row 5).
                    prepareCurrentReview(diagnosticContext: registration.childContext)
                    await refreshHistory(diagnosticContext: registration.childContext)
                } catch {
                    registration.finish(.failure, error: error)
                    guard registeredSeal == digest else { return }
                    handleUnsealedFailure(error, takeID: takeID, captureReleased: true)
                }
            }
        } else if phase == "recording", !stopRequested, state.phase == .starting {
            var updated = state; updated.phase = .recording; updated.status = "Recording"; publish(updated)
        } else if phase == "error", state.errorCode == "RECORDING.DEVICE_UNAVAILABLE" {
            captureStopped()
            var updated = state; updated.phase = .recoverableError; captureTerminalNotice = updated.status; publish(updated)
            requireAudioReconnect(after: .deviceUnavailable)
        } else if phase == "error" {
            resolveUnsealedTerminal(takeID: takeID, writtenFrames: response.data.object?["written_frames"]?.integer)
        }
    }

    private var currentTerminalResolved: Bool {
        guard let takeID = state.takeID, let operationID = startOperationID else { return false }
        return unsealedTerminal == RecordingUnsealedTerminal(takeID: takeID, operationID: operationID)
    }

    /// A capture terminal without a registered seal leaves its current-session reservation
    /// protected. Request exactly one explicit service recheck per take and start operation,
    /// then return to Ready. The service classifies preserved evidence; nothing is rewritten.
    func resolveUnsealedTerminal(takeID: String, writtenFrames: Int64?, captureReleased: Bool = false) {
        guard let operationID = startOperationID else { return }
        let terminal = RecordingUnsealedTerminal(takeID: takeID, operationID: operationID)
        guard unsealedTerminal != terminal else { return }
        unsealedTerminal = terminal
        if !captureReleased { captureStopped() }
        // Zero frames after an explicit Stop captured nothing; say so once, without alarm.
        let cancelledBeforeAudio = stopRequested && writtenFrames == 0 && state.errorCode == "RECORDING.CANCELLED"
        var updated = state
        updated.registrationPending = false; updated.activeOperationID = nil; updated.unsealedRecheckPending = true
        if cancelledBeforeAudio {
            if cancelledBeforeAudioTakeIDs.count == 50 { cancelledBeforeAudioTakeIDs.removeAll(keepingCapacity: true) }
            cancelledBeforeAudioTakeIDs.insert(takeID)
            updated.phase = .ready; updated.errorCode = nil; updated.status = Self.cancelledBeforeAudioStatus
        } else {
            updated.phase = .recoverableError
            if updated.errorCode == nil { updated.status = "Capture stopped · recovery required" }
        }
        captureTerminalNotice = updated.status
        publish(updated)
        startUnsealedRecheck()
    }

    func startUnsealedRecheck() {
        guard let terminal = unsealedTerminal, state.unsealedRecheckPending, unsealedRecheckTask == nil,
              !recoveryInventoryClosed else { return }
        guard !recoveryInventoryLoading else { unsealedRecheckQueued = true; return }
        unsealedRecheckQueued = false
        unsealedRecheckTask = Task { [weak self] in await self?.finishUnsealedRecheck(terminal) }
    }

    private func finishUnsealedRecheck(_ terminal: RecordingUnsealedTerminal) async {
        let checked = await recoverTake(terminal.takeID)
        unsealedRecheckTask = nil
        guard unsealedTerminal == terminal, state.unsealedRecheckPending else { return }
        // A page load began before this request; its completion drains the retry.
        if !checked && recoveryInventoryLoading && !recoveryInventoryClosed { unsealedRecheckQueued = true; return }
        var updated = state; updated.unsealedRecheckPending = false
        // The recheck resolves the terminal's own failure, never a closed channel's.
        if updated.phase == .recoverableError, !processChannelFailed { updated.phase = .ready }
        publish(updated)
    }

    private func handleTelemetry(_ event: RecordingWireEnvelope) throws {
        let snapshot = try RecordingWaveformSnapshot.project(event)
        guard state.takeID == snapshot.takeID else { return }
        _ = waveform.accept(snapshot)
        if snapshot.writtenFrames > 0, state.phase == .starting, !stopRequested {
            var updated = state; updated.phase = .recording; updated.status = "Recording"; publish(updated)
        }
    }

    func handleService(_ event: RecordingWireEnvelope) {
        switch event.data.object?["event"]?.string {
        case "analysis":
            guard let result = event.data.object?["result"], let takeID = result.object?["take_id"]?.string,
                  let analysis = result.object?["state"]?.string else { return }
            var updated = state
            if updated.takeID == takeID, var take = updated.take.object,
               result.object?["source_sha256"] == take["seal"]?.object?["original_sha256"],
               result.object?["edit_revision"] == take["edit_revision"],
               result.object?["context_revision"] == take["reservation"]?.object?["group"]?.object?["context_revision"] {
                take["analysis"] = .string(analysis); updated.take = .object(take)
                if [.ready, .draftReview].contains(updated.phase) { updated.status = analysis == "verified" ? "Verified" : (analysis == "invalid" ? "Invalid take · needs attention" : "Needs review") }
            }
            publish(updated)
            // Indexed rows intentionally omit edit/context identities; refresh their bounded
            // authoritative projection instead of applying an unverifiable late QC patch. A
            // burst of results merges into one deferred refresh behind the seal→review chain.
            scheduleHistoryRefresh()
        case "recovery": applyRecoveryEvent(event.data)
        case "recovery-inventory-changed": invalidateRecoveryInventory(event.data)
        case "context":
            // A full service resnapshot repairs reliable event gaps; refresh this independent
            // inventory too, without treating its partial snapshot as a transport resnapshot.
            if recoveryBootstrapFinished {
                Task { [weak self] in await self?.refreshRecoveryInventory() }
            }
        case "package-preparation", "package": applyPackageEvent(event.data)
        case "history": applyHistory(event.data)
        case "derivative": handleDerivative(event.data)
        case "review-action": handleLegacyReviewAction(event.data)
        default: break // Correlated context/reserve/save/navigation replies apply exactly once in their operation.
        }
    }

    private func applyContext(_ event: RecordingJSON, operationID: String) throws {
        guard event.object?["event"] == .string("context"), let group = event.object?["group"], let cursor = event.object?["cursor"],
              cursor.object?["operation_id"] == .string(operationID) else { throw invalid("Context reply does not match its operation") }
        try applyCursor(cursor, preserveTake: false)
        var updated = state; updated.groupID = group.object?["group_id"]?.string
        let gate = group.object?["gate"]?.object?["state"]?.string
        updated.contextReady = gate == "ready" || gate == "not-required"
        updated.contextReason = updated.contextReady ? "Recording context is ready" : "Corpus capture approval is not prepared"
        updated.phase = .ready; publish(updated)
        if let history = event.object?["history"], history != .null { applyHistory(history) }
    }

    func applyCursor(_ cursor: RecordingJSON, preserveTake: Bool, update: (inout RecordingWorkspaceState) -> Void = { _ in }) throws {
        guard let selection = cursor.object?["selection"],
              selection.object?["catalog_sha256"] == .string(catalogSHA256),
              let promptID = selection.object?["prompt_id"]?.string,
              let prompt = prompts.first(where: { $0.id == promptID }) else { throw invalid("Cursor does not bind the configured catalog prompt") }
        let promptChanged = state.prompt?.id != promptID
        let changed = promptChanged || state.takeID != selection.object?["take_id"]?.string
        var updated = state; update(&updated); updated.selection = selection; updated.prompt = prompt
        updated.complete = cursor.object?["complete"] == .boolean(true)
        if changed { deferredDerivativeEvent = nil; legacyReviewStatus = nil; deferredLegacyReviewEvent = nil; reviewDerivativeState = nil; legacyReviewOperation = nil; reviewTask?.cancel(); reviewOperationID = nil; review.discardDisplay() }
        if changed && (!preserveTake || updated.take.object?["reservation"]?.object?["take_id"]?.string != selection.object?["take_id"]?.string) {
            updated.take = .null
            waveform.select(takeID: selection.object?["take_id"]?.string ?? "unselected", processGeneration: recorderGeneration, hostClockComparable: false)
        }
        publish(updated)
        if promptChanged { Task { [weak self] in await self?.loadTakeHistory(reset: true) } }
    }

    /// Load one bounded metadata page on explicit library demand. A stale cursor refreshes
    /// the coherent first page; it never combines prompt coverage across index revisions.
    public func loadMorePromptStatuses() async {
        guard !historyRefreshPending, let cursor = state.promptCursor, let service else { return }
        historyRefreshPending = true
        if !historyRefreshInFlight { historyRefreshInFlight = true }
        let revision = state.historyRevision
        do {
            let response = try result(await service.command(.object(["command": .string("history"), "catalog_sha256": .string(catalogSHA256), "cursor": .null, "prompt_cursor": .string(cursor), "group_cursor": .null, "page_size": .integer(200)]), takeID: nil))
            guard response.data.object?["index_revision"]?.integer == revision, state.historyRevision == revision else {
                finishHistoryRefresh(); await refreshHistory(); return
            }
            applyHistory(response.data)
            finishHistoryRefresh()
        } catch {
            finishHistoryRefresh(); await refreshHistory()
        }
    }

    /// Service requests behind the fixture seams; a configured workspace always reaches its service.
    private func registerSealTransport() -> (@MainActor (RecordingJSON, String, RecordingDiagnosticContext?) async throws -> RecordingWireEnvelope)? {
        if let registerSealRequest { return registerSealRequest }
        guard let service else { return nil }
        return { receipt, operation, context in try await service.registerSeal(receipt, operationID: operation, diagnosticContext: context) }
    }
    private func historyTransport() -> (@MainActor (RecordingJSON, RecordingDiagnosticContext?) async throws -> RecordingWireEnvelope)? {
        if let historyRequest { return historyRequest }
        guard let service else { return nil }
        return { command, context in try await service.command(command, takeID: nil, diagnosticContext: context) }
    }

    /// Background results merge into one deferred refresh per window (see `backgroundRefreshWindow`).
    func scheduleHistoryRefresh() {
        guard scheduledHistoryRefresh == nil else { return }
        scheduledHistoryRefresh = Task { [weak self] in
            let settled = await self?.settleBackgroundWindow() ?? false
            guard let self else { return }
            scheduledHistoryRefresh = nil
            if settled { await refreshHistory() }
        }
    }
    /// One merge window; a window cancelled by close schedules nothing.
    func settleBackgroundWindow() async -> Bool {
        do { try await Task.sleep(for: backgroundRefreshWindow); return true } catch { return false }
    }

    private func finishHistoryRefresh() {
        historyRefreshPending = false
        if historyRefreshRequested {
            historyRefreshRequested = false
            let context = queuedHistoryDiagnosticContext; queuedHistoryDiagnosticContext = nil
            Task { [weak self] in await self?.refreshHistory(diagnosticContext: context) }
        } else if historyRefreshInFlight { historyRefreshInFlight = false }
    }

    @discardableResult private func applyHistory(_ event: RecordingJSON) -> Bool {
        guard event.object?["event"] == .string("history"),
              event.object?["catalog_sha256"] == .string(catalogSHA256),
              event.object?["take_prompt_id"] == nil || event.object?["take_prompt_id"] == .null,
              let revision = event.object?["index_revision"]?.integer, revision >= state.historyRevision else { return false }
        var updated = state
        if revision > updated.historyRevision { updated.promptStatuses.removeAll(keepingCapacity: true) }
        updated.historyRevision = revision
        updated.historyCompleteness = event.object?["completeness"]?.string ?? "unknown"
        updated.coveredCount = event.object?["covered_prompt_count"]?.integer ?? 0
        updated.totalCount = event.object?["total_prompt_count"]?.integer ?? Int64(prompts.count)
        updated.history = event.object?["takes"]?.array ?? []
        updated.historyCursor = event.object?["next_cursor"]?.string
        updated.promptCursor = event.object?["next_prompt_cursor"]?.string
        for row in event.object?["prompts"]?.array ?? [] {
            if let promptID = row.object?["prompt_id"]?.string { updated.promptStatuses[promptID] = row.object?["status"]?.string ?? "unknown" }
        }
        publish(updated)
        return true
    }

    private func cancelBeforeInput() {
        captureStopped()
        var updated = state; updated.phase = .ready; updated.activeOperationID = nil; updated.status = "Recording cancelled before input opened"; publish(updated)
    }
    func result(_ response: RecordingWireEnvelope) throws -> RecordingWireEnvelope {
        if response.error != .null { throw RecordingContractError(code: response.error.object?["code"]?.string ?? "RECORDING.INVALID_MESSAGE", message: response.error.object?["message"]?.string ?? "Local recording operation failed") }
        return response
    }
    private func invalid(_ message: String) -> RecordingContractError { .init(code: "RECORDING.INVALID_MESSAGE", message: message) }
    func newOperation(_ purpose: String) -> String { purpose + "-" + UUID().uuidString.lowercased() }
    func errorMessage(_ error: Error) -> String {
        if let error = error as? RecordingContractError { return error.message }
        if let error = error as? NativeCaptureFailure { return error.message }
        return "Local recording operation failed; inspect recovery details"
    }
    func fail(_ error: Error, phase: RecordingShellPhase) {
        var updated = state; updated.phase = phase; updated.status = errorMessage(error)
        updated.errorCode = (error as? RecordingContractError)?.code ?? (error as? NativeCaptureFailure)?.code ?? "RECORDING.INVALID_MESSAGE"
        publish(updated)
    }
    func publish(_ updated: RecordingWorkspaceState) {
        state = updated
        let event = RecordingWorkspaceTransition(monotonicNanoseconds: DispatchTime.now().uptimeNanoseconds,
            phase: updated.phase.rawValue, promptID: updated.prompt?.id, promptText: updated.prompt?.scriptBody,
            takeID: updated.takeID, operationID: updated.activeOperationID, status: updated.status)
        transitions.append(event)
        if transitions.count > 256 { transitions.removeFirst(transitions.count - 256) }
        transitionObserver?(event)
    }

    private func captureStopped() {
        review.playback.endCapture()
        afterCaptureStopped()
    }

    public func close() {
        systemLifecycle?.close(); systemLifecycle = nil
        recoveryInventoryClosed = true
        recoveryRequestGeneration &+= 1
        recoveryRefreshRequested = false
        scheduledHistoryRefresh?.cancel(); scheduledHistoryRefresh = nil
        scheduledRecoveryRefresh?.cancel(); scheduledRecoveryRefresh = nil
        reviewTask?.cancel(); reviewTask = nil
        for consumer in consumers { consumer.cancel() }
        consumers.removeAll(); recorder?.channel.close(); service?.channel.close()
    }
}
