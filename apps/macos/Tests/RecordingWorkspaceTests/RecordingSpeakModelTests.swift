import CryptoKit
import Darwin
import Foundation
import RecordingContracts
@testable import RecordingCore
@testable import RecordingUI
import Testing

/// A playback backend that never opens audio: it records what the Speak transport asked for
/// and can report an output failure for the current generation.
private actor SpeechBackend: RecordingPlaybackBackend {
    var loads: [RecordingPlaybackSource] = []
    var plays = 0
    var stops = 0
    var failNextLoad = false
    var failStop = false
    private var generation: UInt64 = 0
    private var handler: (@Sendable (RecordingPlaybackBackendEvent) async -> Void)?
    func setEventHandler(_ handler: @escaping @Sendable (RecordingPlaybackBackendEvent) async -> Void) { self.handler = handler }
    func load(_ source: RecordingPlaybackSource, generation: UInt64) throws {
        self.generation = generation
        if failNextLoad {
            failNextLoad = false
            throw RecordingContractError(code: "RECORDING.SOURCE_CHANGED", message: "Selected playback source no longer matches its checksum")
        }
        loads.append(source)
    }
    func play(from frame: Int64, through endFrame: Int64, generation: UInt64) { self.generation = generation; plays += 1 }
    func pause() -> Int64? { 0 }
    func position() -> Int64? { 0 }
    func stop() throws {
        stops += 1
        if failStop { throw RecordingContractError(code: "RECORDING.PLAYBACK_FAILED", message: "Audio output did not stop; capture remains unavailable") }
    }
    func recover() { failStop = false }
    func failTheNextLoad() { failNextLoad = true }
    func setStopFailure() { failStop = true }
    func failOutput() async {
        await handler?(.failed(generation: generation, error: RecordingContractError(code: "RECORDING.PLAYBACK_FAILED",
            message: "Audio output configuration changed; review playback before continuing")))
    }
}

/// Pure-model coverage for Speak: every reply and event is a real wire shape (checked against
/// the committed response schema) delivered through scripted requests or the service stream
/// handler. No process is created; only the real-clip test opens a file, rendered offline.
@Suite(.serialized)
struct RecordingSpeakModelTests {
    static let runID = "finetune-20260916t080000z"
    static let earlierRunID = "finetune-20260915t080000z"
    static let oldestRunID = "finetune-20260914t080000z"
    static let neutral = "reference-neutral-01"
    static let warm = "reference-warm-02"
    static let text = "The meeting moved to Thursday."
    static let clipSHA256 = String(repeating: "5", count: 64)
    static let deadline = "speech synthesis took longer than 2 min and was stopped; try a shorter text or fewer steps"
    static let supervisorFailure = "fine-tune supervision stopped; restart the app to recover the job"

    private static let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()

    /// Scripted Speak requests: records each one, optionally holds it until released, then
    /// answers with the current scripted reply.
    @MainActor final class ScriptedSpeak {
        struct Sent: Equatable {
            let command: String
            let operationID: String
            /// The submit's request id, or the cancelled operation.
            let detail: String
            let speech: RecordingSpeakCommand?
        }
        var sent: [Sent] = []
        var answered: [String: Int] = [:]
        var holding: Set<String> = []
        private var held: [String: [CheckedContinuation<Void, Never>]] = [:]
        var submitReply: (RecordingSpeakCommand) throws -> RecordingWireEnvelope = {
            try RecordingFineTuneTests.envelope(RecordingSpeakModelTests.synthesis($0.operationID, "queued", model: $0.model, text: $0.text))
        }
        var cancelReply: (String) throws -> RecordingWireEnvelope = {
            try RecordingFineTuneTests.envelope(RecordingSpeakModelTests.synthesis($0, "cancelled"))
        }
        var commands: [String] { sent.map(\.command) }
        var heldCount: Int { held.values.map(\.count).reduce(0, +) }

        func release(_ command: String) {
            holding.remove(command)
            for waiter in held.removeValue(forKey: command) ?? [] { waiter.resume() }
        }

        private func hold(_ command: String) async {
            if holding.contains(command) { await withCheckedContinuation { held[command, default: []].append($0) } }
        }

        var requests: RecordingSpeakRequests {
            RecordingSpeakRequests(
                submit: { [self] command, requestID in
                    sent.append(Sent(command: "synthesize", operationID: command.operationID, detail: requestID, speech: command))
                    await hold("synthesize")
                    defer { answered["synthesize", default: 0] += 1 }
                    return try submitReply(command)
                },
                cancel: { [self] operation, target in
                    sent.append(Sent(command: "cancel-synthesis", operationID: operation, detail: target, speech: nil))
                    await hold("cancel-synthesis")
                    defer { answered["cancel-synthesis", default: 0] += 1 }
                    return try cancelReply(target)
                })
        }
    }

    // MARK: wire shapes

    /// A Speak event. `runID` is the run that spoke (and whose clip was the reference); base
    /// speaks without a run, with the newest run's reference.
    static func synthesis(_ operation: String, _ state: String, model: String = "latest", text: String = RecordingSpeakModelTests.text,
                          runID: String = RecordingSpeakModelTests.runID, reference: String = RecordingSpeakModelTests.neutral,
                          frames: Int64 = 48_000, sha256: String = RecordingSpeakModelTests.clipSHA256,
                          error: RecordingJSON? = nil) -> RecordingJSON {
        var event: [String: RecordingJSON] = ["event": .string("synthesis"), "operation_id": .string(operation),
                                              "state": .string(state), "model": .string(model), "text": .string(text)]
        switch state {
        case "finished":
            if model != "base" { event["run_id"] = .string(runID) }
            event["reference_run_id"] = .string(runID)
            event["reference_prompt_id"] = .string(reference)
            event["relative_path"] = .string("derived/speech/\(operation)/speech-01.wav")
            event["sha256"] = .string(sha256)
            event["frames"] = .integer(frames)
            event["sample_rate_hz"] = .integer(24_000)
            event["seconds"] = .number(Double(frames) / 24_000)
        case "failed":
            event["error"] = error ?? RecordingFineTuneTests.failure("FINETUNE.RUNTIME_FAILED", "the F5 synthesizer exited with status 1")
        default: break
        }
        return .object(event)
    }

    static func stream(_ event: RecordingJSON) throws -> RecordingWireEnvelope {
        try RecordingFineTuneTests.envelope(event, requestID: event.object?["operation_id"]?.string ?? "fixture-request")
    }

    static func run(_ id: String, finishedAt: String, references: [String]) -> RecordingJSON {
        .object(["run_id": .string(id), "run_root": .string("derived/finetune/\(id)"), "status": .string("finished"),
                 "started_at": .string("2026-09-01T08:00:00Z"), "finished_at": .string(finishedAt), "epochs": .integer(120),
                 "prompt_count": .integer(34), "retained_seconds": .number(181.25), "finetuned_sample_paths": .array([]),
                 "base_sample_paths": .array([]), "reference_prompt_ids": .array(references.map(RecordingJSON.string))])
    }

    static let latestRun = run(runID, finishedAt: "2026-09-16T10:00:00Z", references: [neutral, warm])
    static let earlierRun = run(earlierRunID, finishedAt: "2026-09-15T10:00:00Z", references: [warm])
    static let oldestRun = run(oldestRunID, finishedAt: "2026-09-14T10:00:00Z", references: [])
    static let threeRuns = [latestRun, earlierRun, oldestRun]

    static func status(_ operation: String, configured: Bool = true, runs: [RecordingJSON] = RecordingSpeakModelTests.threeRuns,
                       latestError: String? = nil, active: RecordingJSON? = nil) -> RecordingJSON {
        var event: [String: RecordingJSON] = [
            "event": .string("fine-tune-status"), "operation_id": .string(operation), "runtime_configured": .boolean(configured),
            "active": active ?? .null, "latest_run": runs.first ?? .null, "finished_runs": .array(runs),
        ]
        if let latestError { event["latest_run_error"] = RecordingFineTuneTests.failure("FINETUNE.SOURCE_CHANGED", latestError) }
        return .object(event)
    }

    /// The two reference prompts, so the reference picker can title them.
    static let catalog: RecordingJSON = .object(["purpose": .string("capture-pilot"), "prompts": .array([
        .object(["prompt_id": .string(neutral), "text": .string("A calm and even sentence."), "category": .string("neutral"),
                 "style": .string("Natural pace"), "expected_seconds": .integer(6), "reference_candidate": .boolean(true)]),
        .object(["prompt_id": .string(warm), "text": .string("A warm and friendly sentence."), "category": .string("warm"),
                 "style": .string("Warm delivery"), "expected_seconds": .integer(6), "reference_candidate": .boolean(true)]),
    ])])

    // MARK: helpers

    /// Lets the model's request tasks run until `condition` holds; records an issue otherwise.
    @MainActor private func waitUntil(_ condition: @MainActor () -> Bool,
                                      sourceLocation: SourceLocation = #_sourceLocation) async {
        for _ in 0..<5_000 {
            if condition() { return }
            await Task.yield()
        }
        #expect(condition(), "the scripted requests did not settle", sourceLocation: sourceLocation)
    }

    /// A workspace whose Speak and fine-tune requests are scripted. When `runs` is given, one
    /// fine-tune status listing them has been applied; the text is ready to speak.
    @MainActor private func workspace(_ speak: ScriptedSpeak, runs: [RecordingJSON]? = RecordingSpeakModelTests.threeRuns,
                                      phase: RecordingShellPhase = .ready,
                                      backend: any RecordingPlaybackBackend = SpeechBackend())
        async -> (RecordingWorkspaceModel, RecordingFineTuneTests.ScriptedFineTune) {
        let model = RecordingWorkspaceModel(fixture: .init(phase: phase), catalog: Self.catalog, speechBackend: backend)
        model.speak.requests = speak.requests
        let fineTune = RecordingFineTuneTests.ScriptedFineTune()
        model.fineTuneRequests = fineTune.requests
        if let runs {
            fineTune.statusReply = { try RecordingFineTuneTests.envelope(Self.status($0, runs: runs)) }
            await model.requestFineTuneStatus()
            fineTune.sent.removeAll()
        }
        model.speak.text = Self.text
        return (model, fineTune)
    }

    /// Presses Speak and waits for the submit reply; returns the request's operation id.
    @MainActor private func speak(_ model: RecordingWorkspaceModel, _ service: ScriptedSpeak) async throws -> String {
        let answered = service.answered["synthesize", default: 0]
        model.speak.speak()
        await waitUntil { service.answered["synthesize", default: 0] == answered + 1 }
        return try #require(service.sent.last { $0.command == "synthesize" }?.operationID)
    }

    // MARK: client

    @Test func theClientsSpeakCommandsAreRequestSchemaShapesAndTheRequestIDNeverEqualsTheOperation() async throws {
        let schema = try Data(contentsOf: Self.repository.appendingPathComponent("schemas/recording-service-request.schema.json"))
        func requireRequest(_ command: RecordingJSON) throws {
            let body: RecordingJSON = .object(["data": command, "error": .null, "meta": .object([
                "protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("request-speak-one"),
                "process_generation": .string("synthetic-generation"), "state_revision": .integer(0),
                "event_sequence": .integer(0), "take_id": .null])])
            _ = try RecordingWireEnvelope.decode(body.transportData(), schema: schema)
        }
        let submit = RecordingServiceClient.synthesizeCommand(operationID: "speak-one", text: "Line one.\nLine two.", model: "latest",
                                                              referencePromptID: nil, nfeSteps: RecordingServiceClient.speechDefaultSteps)
        #expect(submit == .object(["command": .string("synthesize"), "operation_id": .string("speak-one"),
                                   "text": .string("Line one.\nLine two."), "model": .string("latest"),
                                   "reference_prompt_id": .null, "nfe_steps": .integer(32)]))
        try requireRequest(submit)
        try requireRequest(RecordingServiceClient.synthesizeCommand(operationID: "speak-one", text: Self.text, model: "base",
                                                                    referencePromptID: Self.neutral, nfeSteps: 4))
        try requireRequest(RecordingServiceClient.synthesizeCommand(operationID: "speak-one", text: String(repeating: "é", count: 2_000),
                                                                    model: Self.runID, referencePromptID: nil, nfeSteps: 64))
        for (text, steps) in [("Hello\tthere.", 32), (String(repeating: "é", count: 2_001), 32), (Self.text, 3), (Self.text, 65)] {
            #expect(throws: RecordingContractError.self) {
                try requireRequest(RecordingServiceClient.synthesizeCommand(operationID: "speak-one", text: text, model: "latest",
                                                                            referencePromptID: nil, nfeSteps: steps))
            }
        }
        let cancel = RecordingServiceClient.cancelSynthesisCommand(operationID: "cancel-speak-one", cancelledOperationID: "speak-one")
        #expect(cancel == .object(["command": .string("cancel-synthesis"), "operation_id": .string("cancel-speak-one"),
                                   "cancelled_operation_id": .string("speak-one")]))
        try requireRequest(cancel)
        #expect(RecordingServiceClient.speechDefaultSteps == 32 && RecordingServiceClient.fineTuneRequestTimeout == 30)

        // A request id equal to the operation id is refused before anything is sent.
        let client = RecordingServiceClient(configuration: RecordingProcessConfiguration(
            executable: URL(fileURLWithPath: "/usr/bin/false"), arguments: [],
            schemaDirectory: Self.repository.appendingPathComponent("schemas"), processGeneration: "synthetic-generation",
            clientBuild: .null, expectedPeerBuild: .null, peer: .service))
        do {
            _ = try await client.synthesize(operationID: "speak-one", requestID: "speak-one", text: Self.text, model: "latest",
                                            referencePromptID: nil)
            Issue.record("A request id equal to the operation id was accepted")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.INVALID_MESSAGE")
        }
        client.channel.close()
    }

    // MARK: text

    @Test @MainActor func textIsCountedInScalarsWithTabsStrippedAndHiddenCharactersRefused() async {
        let (model, _) = await workspace(ScriptedSpeak())
        let speak = model.speak
        #expect(speak.textProblem == nil && speak.canSpeak && speak.textCount == Self.text.unicodeScalars.count)
        speak.text = ""
        #expect(speak.textProblem == RecordingSpeakModel.emptyTextReason && speak.unavailableReason == RecordingSpeakModel.emptyTextReason)
        speak.text = "  \n \u{3000} "
        #expect(speak.textProblem == RecordingSpeakModel.whitespaceTextReason && !speak.canSpeak)
        // Tabs are removed as typed; CR and CRLF become newlines; newlines are fine.
        speak.text = "Hello\tthere.\r\nLine two.\rLine three."
        #expect(speak.text == "Hellothere.\nLine two.\nLine three." && speak.textProblem == nil)
        speak.text = "\t\t"
        #expect(speak.text == "" && speak.textProblem == RecordingSpeakModel.emptyTextReason)
        // The limit counts Unicode scalars, as the service does.
        speak.text = String(repeating: "é", count: 2_000)
        #expect(speak.textCount == 2_000 && speak.textProblem == nil)
        speak.text = String(repeating: "é", count: 1_999) + "e\u{301}"
        #expect(speak.text.count == 2_000 && speak.textCount == 2_001)
        #expect(speak.textProblem == RecordingSpeakModel.tooLongReason(2_001))
        #expect(RecordingSpeakModel.tooLongReason(2_001).hasPrefix("Shorten the text to "))
        // Format and control characters other than a newline are refused.
        for hidden in ["Hello\u{200B}there.", "Hello\u{0007}there.", "\u{FEFF}Hello."] {
            speak.text = hidden
            #expect(speak.textProblem == RecordingSpeakModel.hiddenCharacterReason, "\(hidden.unicodeScalars.map(\.value))")
        }
        speak.text = "\u{200B}"
        #expect(speak.textProblem == RecordingSpeakModel.hiddenCharacterReason, "a zero-width space is not whitespace")
        speak.text = "Line one.\nLine two."
        #expect(speak.canSpeak)
    }

    // MARK: model and reference choices

    @Test @MainActor func modelAndReferenceChoicesFollowTheFinishedRunsNewestFirst() async throws {
        let service = ScriptedSpeak()
        let (model, fineTune) = await workspace(service, runs: nil)
        let speak = model.speak
        #expect(speak.modelChoices.isEmpty && speak.referenceChoices.isEmpty && !speak.runsKnown)
        #expect(speak.unavailableReason == RecordingSpeakModel.checkingReason)
        fineTune.statusReply = { try RecordingFineTuneTests.envelope(Self.status($0)) }
        await model.requestFineTuneStatus()
        #expect(speak.runsKnown && speak.runs.map(\.runID) == [Self.runID, Self.earlierRunID, Self.oldestRunID])
        #expect(speak.modelChoices.map(\.id) == ["latest", Self.earlierRunID, Self.oldestRunID, "base"])
        #expect(speak.modelChoices.map(\.title) == ["Latest · \(Self.runID)", Self.earlierRunID, Self.oldestRunID, "Base"])
        #expect(speak.modelChoices.map(\.referenceRunID) == [Self.runID, Self.earlierRunID, Self.oldestRunID, Self.runID])
        #expect(speak.selectedModelID == "latest" && speak.selectedReferencePromptID == Self.neutral)
        #expect(speak.referenceChoices == [RecordingSpeakReferenceChoice(id: Self.neutral, title: "Reference Neutral 01"),
                                           RecordingSpeakReferenceChoice(id: Self.warm, title: "Reference Warm 02")])
        #expect(speak.canSpeak)

        // Base speaks with the newest run's references; a chosen reference it offers is kept.
        speak.selectedReferencePromptID = Self.warm
        speak.selectedModelID = "base"
        #expect(speak.referenceChoices.map(\.id) == [Self.neutral, Self.warm] && speak.selectedReferencePromptID == Self.warm)
        // An earlier run offers its own; one it lacks falls back to its first.
        speak.selectedReferencePromptID = Self.neutral
        speak.selectedModelID = Self.earlierRunID
        #expect(speak.referenceChoices.map(\.id) == [Self.warm] && speak.selectedReferencePromptID == Self.warm)
        // A run without reference prompts lets the service choose: the request names none.
        speak.selectedModelID = Self.oldestRunID
        #expect(speak.referenceChoices.isEmpty && speak.selectedReferencePromptID == nil)
        let oldest = try await self.speak(model, service)
        #expect(service.sent.last?.speech == RecordingSpeakCommand(operationID: oldest, text: Self.text, model: Self.oldestRunID,
                                                                  referencePromptID: nil, nfeSteps: 32))
        #expect(speak.request?.modelTitle == Self.oldestRunID && speak.request?.referencePromptID == nil)
        model.handleService(try Self.stream(Self.synthesis(oldest, "cancelled", model: Self.oldestRunID)))

        // A later list without the chosen run falls back to Latest.
        fineTune.statusReply = { try RecordingFineTuneTests.envelope(Self.status($0, runs: [Self.latestRun, Self.earlierRun])) }
        await model.requestFineTuneStatus()
        #expect(speak.modelChoices.map(\.id) == ["latest", Self.earlierRunID, "base"])
        #expect(speak.selectedModelID == "latest" && speak.selectedReferencePromptID == Self.neutral)

        // A fine-tune this app showed finishing makes its run the newest at once.
        let job = "fine-tune-job-new"
        fineTune.submitReply = { try RecordingFineTuneTests.envelope(RecordingFineTuneTests.progress($0, "queued", runID: nil)) }
        model.startFineTune()
        await waitUntil { fineTune.answered["fine-tune"] == 1 }
        let shown = try #require(model.fineTune.operationID)
        let newRun = Self.run("finetune-20260917t080000z", finishedAt: "2026-09-17T10:00:00Z", references: [Self.warm])
        model.handleService(try RecordingFineTuneTests.envelope(RecordingFineTuneTests.runEvent(job, newRun), requestID: job))
        #expect(speak.runs.count == 2, "another job's run is not this app's news")
        model.handleService(try RecordingFineTuneTests.envelope(RecordingFineTuneTests.runEvent(shown, newRun), requestID: shown))
        model.handleService(try RecordingFineTuneTests.envelope(RecordingFineTuneTests.runEvent(shown, newRun), requestID: shown))
        #expect(speak.modelChoices.map(\.title) == ["Latest · finetune-20260917t080000z", Self.runID, Self.earlierRunID, "Base"])
        #expect(speak.selectedReferencePromptID == Self.warm, "Latest now offers only the new run's reference")
        // A status requested before that job ended describes an older list and changes nothing.
        let known = speak.runs
        speak.applyRunList(Self.status("fine-tune-status-old").object ?? [:], current: false)
        #expect(speak.runs == known)

        // An unreadable run record: its words, and no choices.
        fineTune.statusReply = { try RecordingFineTuneTests.envelope(Self.status($0, runs: [], latestError: "run.json is invalid")) }
        await model.requestFineTuneStatus()
        #expect(speak.modelChoices.isEmpty && speak.runsError == "run.json is invalid")
        #expect(speak.unavailableReason == "run.json is invalid")
    }

    @Test @MainActor func aRunNamedLikeAKeywordIsNotOfferedByItsID() async {
        let keyword = Self.run("base", finishedAt: "2026-09-15T10:00:00Z", references: [])
        let (model, _) = await workspace(ScriptedSpeak(), runs: [Self.latestRun, keyword])
        #expect(model.speak.modelChoices.map(\.id) == ["latest", "base"])
        #expect(model.speak.modelChoices.map(\.title) == ["Latest · \(Self.runID)", "Base"])
        // Without the fine-tune status, an older reply may still fill an empty list.
        let fresh = RecordingWorkspaceModel(fixture: .init(), catalog: Self.catalog)
        fresh.speak.applyRunList(Self.status("fine-tune-status-old").object ?? [:], current: false)
        #expect(fresh.speak.runsKnown && fresh.speak.runs.count == 3)
    }

    // MARK: availability

    @Test @MainActor func availabilityNamesTheFirstReasonSpeakCannotStart() async throws {
        let unconfigured = RecordingWorkspaceModel(fixture: .init())
        #expect(unconfigured.speak.unavailableReason == RecordingSpeakModel.serviceUnavailableReason)
        #expect(unconfigured.speak.cancelUnavailableReason == RecordingSpeakModel.serviceUnavailableReason)
        unconfigured.speak.text = Self.text
        unconfigured.speak.speak()
        #expect(unconfigured.speak.request == nil)

        let service = ScriptedSpeak()
        let (unknown, fineTune) = await workspace(service, runs: nil)
        #expect(unknown.speak.unavailableReason == RecordingSpeakModel.checkingReason)
        fineTune.statusReply = { _ in try RecordingFineTuneTests.refusal("RECORDING.GATE_NOT_READY", "fine-tuning is unavailable in this recording service") }
        await unknown.requestFineTuneStatus()
        #expect(unknown.speak.unavailableReason == "fine-tuning is unavailable in this recording service")
        unknown.speak.speak()
        #expect(service.sent.isEmpty)

        let cases: [(String, [RecordingJSON], Bool, String?, RecordingJSON?, String)] = [
            ("no runtime", Self.threeRuns, false, nil, nil, RecordingSpeakModel.noRuntimeReason),
            ("no finished run", [], true, nil, nil, RecordingSpeakModel.noFinishedRunReason),
            ("an unreadable run", [], true, "the latest fine-tuned model cannot be read", nil, "the latest fine-tuned model cannot be read"),
            ("a fine-tune running", Self.threeRuns, true, nil, RecordingFineTuneTests.training(), RecordingSpeakModel.fineTuneActiveReason),
            ("a fine-tune queued", Self.threeRuns, true, nil, RecordingFineTuneTests.progress("fine-tune-job-one", "queued", runID: nil),
             RecordingSpeakModel.fineTuneActiveReason),
        ]
        for (name, runs, configured, latestError, active, reason) in cases {
            let (model, statuses) = await workspace(ScriptedSpeak(), runs: nil)
            statuses.statusReply = {
                try RecordingFineTuneTests.envelope(Self.status($0, configured: configured, runs: runs, latestError: latestError, active: active))
            }
            await model.requestFineTuneStatus()
            #expect(model.speak.unavailableReason == reason, "\(name)")
            #expect(!model.speak.canSpeak, "\(name)")
        }

        for phase in [RecordingShellPhase.starting, .recording, .stopping] {
            let (model, _) = await workspace(ScriptedSpeak(), phase: phase)
            #expect(model.speak.unavailableReason == RecordingSpeakModel.recordingReason, "\(phase)")
        }
        let (saving, _) = await workspace(ScriptedSpeak(), phase: .saving)
        #expect(saving.speak.unavailableReason == RecordingSpeakModel.savingReason)
        let (registering, _) = await workspace(ScriptedSpeak(), phase: .draftReview)
        registering.state.registrationPending = true
        #expect(registering.speak.unavailableReason == RecordingSpeakModel.savingReason)
        for phase in [RecordingShellPhase.ready, .draftReview, .recoverableError] {
            let (model, _) = await workspace(ScriptedSpeak(), phase: phase)
            #expect(model.speak.canSpeak && model.speak.unavailableReason == nil, "\(phase) does not block Speak")
        }

        // Already generating, then being cancelled; a channel closed or a stopped supervisor first.
        let busyService = ScriptedSpeak()
        busyService.holding = ["cancel-synthesis"]
        let (busy, _) = await workspace(busyService)
        _ = try await speak(busy, busyService)
        #expect(busy.speak.isGenerating && busy.speak.unavailableReason == RecordingSpeakModel.generatingReason)
        #expect(busy.speak.canCancel && busy.speak.cancelUnavailableReason == nil)
        busy.speak.cancel()
        #expect(busy.speak.unavailableReason == RecordingSpeakModel.cancellingReason)
        #expect(!busy.speak.canCancel && busy.speak.cancelUnavailableReason == RecordingSpeakModel.cancellingReason)
        busy.processChannelFailed = true
        #expect(busy.speak.unavailableReason == RecordingSpeakModel.channelClosedReason)
        #expect(busy.speak.cancelUnavailableReason == RecordingSpeakModel.channelClosedReason)
        busyService.release("cancel-synthesis")
        await waitUntil { busyService.answered["cancel-synthesis"] == 1 }

        let (idle, _) = await workspace(ScriptedSpeak())
        #expect(!idle.speak.canCancel && idle.speak.cancelUnavailableReason == RecordingSpeakModel.noRequestReason)
        idle.handleService(try RecordingFineTuneTests.envelope(.null, error: RecordingFineTuneTests.failure("RECORDING.WRITE_FAILED",
            Self.supervisorFailure, retryable: true), requestID: RecordingWorkspaceModel.fineTuneSupervisorRequestID))
        #expect(idle.speak.unavailableReason == Self.supervisorFailure && idle.speak.statusLine == Self.supervisorFailure)
        #expect(idle.speak.needsAttention && idle.speak.request == nil)
    }

    // MARK: speaking

    @Test @MainActor func speakingSendsOneRequestWithItsOwnRequestIDAndHoldsFineTuneBack() async throws {
        let service = ScriptedSpeak()
        service.holding = ["synthesize"]
        let (model, fineTune) = await workspace(service)
        #expect(model.canStartFineTune)
        model.speak.selectedReferencePromptID = Self.warm
        model.speak.speak()
        // Shown at once, before the reply.
        let request = try #require(model.speak.request)
        #expect(request.phase == .queued && request.text == Self.text && request.modelID == "latest")
        #expect(request.modelTitle == "Latest · \(Self.runID)" && request.referencePromptID == Self.warm)
        #expect(model.speak.statusLine == "Generating · waiting to start" && !model.speak.needsAttention)
        await waitUntil { service.heldCount == 1 }
        let sent = try #require(service.sent.first)
        #expect(sent.operationID == request.operationID && sent.operationID.hasPrefix("speak-"))
        #expect(sent.detail == "request-" + sent.operationID && sent.detail != sent.operationID)
        #expect(sent.speech == RecordingSpeakCommand(operationID: sent.operationID, text: Self.text, model: "latest",
                                                     referencePromptID: Self.warm, nfeSteps: 32))
        // Pressing again sends nothing; the fine-tune control waits for Speak.
        model.speak.speak()
        #expect(service.sent.count == 1)
        #expect(!model.canStartFineTune && model.fineTuneUnavailableReason == "Wait for Speak to finish")
        #expect(model.fineTuneSlot == RecordingFineTuneSlot(title: "Fine-tune model", identifier: "transport.fine-tune",
                                                            enabled: false, help: RecordingWorkspaceModel.fineTuneWaitForSpeakReason))
        model.startFineTune()
        #expect(fineTune.sent.isEmpty)
        service.release("synthesize")
        await waitUntil { service.answered["synthesize"] == 1 }
        #expect(model.speak.request?.phase == .queued)
        model.handleService(try Self.stream(Self.synthesis(sent.operationID, "finished")))
        #expect(model.canStartFineTune && model.fineTuneUnavailableReason == RecordingFineTuneTests.startHelp)
        // Changing the text afterwards does not change what was sent.
        model.speak.text = "Something else."
        #expect(model.speak.request?.text == Self.text && model.speak.clips.first?.text == Self.text)
    }

    @Test @MainActor func eventsApplyInOrderOnceWhicheverArrivesFirst() async throws {
        // The reply first, then every stream copy, each twice.
        let service = ScriptedSpeak()
        let (model, _) = await workspace(service)
        let first = try await speak(model, service)
        func apply(_ event: RecordingJSON) throws {
            model.speak.applySynthesisEvent(event)
            let once = (model.speak.request, model.speak.clips)
            model.handleService(try Self.stream(event))
            #expect(model.speak.request == once.0 && model.speak.clips == once.1, "a duplicate changes nothing")
        }
        try apply(Self.synthesis(first, "queued"))
        #expect(model.speak.request?.phase == .queued)
        try apply(Self.synthesis(first, "running"))
        #expect(model.speak.request?.phase == .running && model.speak.statusLine == "Generating…")
        try apply(Self.synthesis(first, "queued"))
        #expect(model.speak.request?.phase == .running, "a queued copy is older than running")
        try apply(Self.synthesis(first, "finished"))
        let clip = try #require(model.speak.clips.first)
        #expect(model.speak.request?.phase == .finished && model.speak.request?.clipID == first)
        #expect(clip.id == first && clip.text == Self.text && clip.modelID == "latest" && clip.modelTitle == "Latest · \(Self.runID)")
        #expect(clip.runID == Self.runID && clip.referenceRunID == Self.runID && clip.referencePromptID == Self.neutral)
        #expect(clip.relativePath == "derived/speech/\(first)/speech-01.wav" && clip.sha256 == Self.clipSHA256)
        #expect(clip.frames == 48_000 && clip.sampleRateHz == 24_000 && clip.seconds == 2 && clip.durationText == "0:02.0")
        #expect(model.speak.selectedClipID == first)
        #expect(model.speak.statusLine == "Spoken · 0:02.0 · Latest · \(Self.runID)")
        let finished = (model.speak.request, model.speak.clips)
        for late in [Self.synthesis(first, "running"), Self.synthesis(first, "cancelled"),
                     Self.synthesis(first, "failed", error: RecordingFineTuneTests.failure("FINETUNE.RUNTIME_FAILED", "late")),
                     Self.synthesis(first, "finished", frames: 24_000)] {
            try apply(late)
            #expect(model.speak.request == finished.0 && model.speak.clips == finished.1, "an ended request is final")
        }

        // The service's running event overtakes the submit reply.
        service.holding = ["synthesize"]
        model.speak.selectedModelID = "base"
        model.speak.speak()
        await waitUntil { service.heldCount == 1 }
        let second = try #require(model.speak.request?.operationID)
        model.handleService(try Self.stream(Self.synthesis(second, "queued", model: "base")))
        model.handleService(try Self.stream(Self.synthesis(second, "running", model: "base")))
        service.release("synthesize")
        await waitUntil { service.answered["synthesize"] == 2 }
        #expect(model.speak.request?.phase == .running, "the queued reply is older than the event")

        // Its end overtakes the reply's stream copy.
        model.handleService(try Self.stream(Self.synthesis(second, "finished", model: "base", frames: 60_000)))
        model.handleService(try RecordingFineTuneTests.envelope(Self.synthesis(second, "queued", model: "base"), requestID: "request-" + second))
        #expect(model.speak.request?.phase == .finished && model.speak.clips.map(\.id) == [second, first])
        #expect(model.speak.clips.first?.modelTitle == "Base" && model.speak.clips.first?.runID == nil)
        #expect(model.speak.clips.first?.durationText == "0:02.5" && model.speak.selectedClipID == second)

        // A request ending before its submit reply returns: the reply changes nothing.
        service.holding = ["synthesize"]
        model.speak.selectedModelID = Self.earlierRunID
        model.speak.speak()
        await waitUntil { service.heldCount == 1 }
        let third = try #require(model.speak.request?.operationID)
        model.handleService(try Self.stream(Self.synthesis(third, "finished", model: Self.earlierRunID, runID: Self.earlierRunID,
                                                           reference: Self.warm)))
        service.release("synthesize")
        await waitUntil { service.answered["synthesize"] == 3 }
        #expect(model.speak.request?.phase == .finished && model.speak.clips.count == 3)
        #expect(model.speak.clips.first?.modelTitle == Self.earlierRunID)

        // Events of another operation, unknown events and malformed shapes are ignored.
        let shown = (model.speak.request, model.speak.clips)
        for foreign in [Self.synthesis("speak-elsewhere", "finished"), Self.synthesis("speak-elsewhere", "running")] {
            model.handleService(try Self.stream(foreign))
        }
        model.speak.applySynthesisEvent(.object(["event": .string("synthesis-unknown"), "operation_id": .string(third)]))
        model.speak.applySynthesisEvent(.null)
        #expect(model.speak.request == shown.0 && model.speak.clips == shown.1)
    }

    @Test @MainActor func aFinishedEventWithoutAPlayableClipFailsTheRequest() async throws {
        let service = ScriptedSpeak()
        let (model, _) = await workspace(service)
        let operation = try await speak(model, service)
        // Wire-valid clips are always playable; a shape outside the contract is refused here too.
        guard case .object(var event) = Self.synthesis(operation, "finished") else { Issue.record("an object"); return }
        event["sample_rate_hz"] = .integer(48_000)
        model.speak.applySynthesisEvent(.object(event))
        #expect(model.speak.request?.phase == .failed && model.speak.request?.errorCode == "RECORDING.INVALID_MESSAGE")
        #expect(model.speak.statusLine == "Speak finished without a playable clip" && model.speak.clips.isEmpty)
    }

    // MARK: failures

    @Test @MainActor func aFailedRequestShowsTheServicesWordsVerbatim() async throws {
        let service = ScriptedSpeak()
        let (model, fineTune) = await workspace(service)
        let operation = try await speak(model, service)
        model.handleService(try Self.stream(Self.synthesis(operation, "running")))
        let deadline = RecordingFineTuneTests.failure("FINETUNE.SYNTHESIS_DEADLINE", Self.deadline, retryable: true)
        model.handleService(try Self.stream(Self.synthesis(operation, "failed", error: deadline)))
        #expect(model.speak.request?.phase == .failed && model.speak.request?.errorCode == "FINETUNE.SYNTHESIS_DEADLINE")
        #expect(model.speak.request?.message == Self.deadline && model.speak.statusLine == Self.deadline)
        #expect(model.speak.needsAttention && model.speak.clips.isEmpty && model.speak.canSpeak, "it can be tried again")

        // A refused submit ends the queued request with the refusal's words.
        let refusal = "no fine-tuned model has finished yet; fine-tune once before using Speak"
        service.submitReply = { _ in try RecordingFineTuneTests.refusal("FINETUNE.NO_FINISHED_RUN", refusal) }
        _ = try await speak(model, service)
        #expect(model.speak.request?.phase == .failed && model.speak.request?.errorCode == "FINETUNE.NO_FINISHED_RUN")
        #expect(model.speak.statusLine == refusal && fineTune.sent.isEmpty)

        // A request past its deadline closed the channel: the thrown error's words.
        service.submitReply = { _ in
            throw RecordingContractError(code: "RECORDING.CANCELLED", message: "Local process request exceeded its deadline; channel closed for recovery")
        }
        _ = try await speak(model, service)
        #expect(model.speak.request?.errorCode == "RECORDING.CANCELLED")
        #expect(model.speak.statusLine == "Local process request exceeded its deadline; channel closed for recovery")

        // Refused because a fine-tune holds the GPU: that job is looked up and shown.
        let busy = "fine-tune fine-tune-other is running; Speak is available once it has finished or been cancelled"
        service.submitReply = { _ in try RecordingFineTuneTests.refusal("FINETUNE.JOB_ACTIVE", busy, retryable: true) }
        fineTune.statusReply = {
            try RecordingFineTuneTests.envelope(Self.status($0, active: RecordingFineTuneTests.training("fine-tune-other")))
        }
        _ = try await speak(model, service)
        #expect(model.speak.statusLine == busy)
        await model.fineTuneStatusTask?.value
        #expect(fineTune.commands == ["fine-tune-status"] && model.fineTune.operationID == "fine-tune-other")
        #expect(model.speak.unavailableReason == RecordingSpeakModel.fineTuneActiveReason)
    }

    @Test @MainActor func aClosedServiceChannelOrAStoppedSupervisorFailsTheRequestLocally() async throws {
        func stream() -> (AsyncThrowingStream<RecordingWireEnvelope, Error>, AsyncThrowingStream<RecordingWireEnvelope, Error>.Continuation) {
            AsyncThrowingStream.makeStream(of: RecordingWireEnvelope.self, throwing: Error.self)
        }
        let closed = RecordingContractError(code: "RECORDING.PARENT_LOST", message: "Peer control channel ended")

        // The service's stream ends while the request runs: the service took the request along.
        let service = ScriptedSpeak()
        let (model, _) = await workspace(service)
        let operation = try await speak(model, service)
        let (events, continuation) = stream()
        let consumer = model.consumeService(events)
        continuation.yield(try Self.stream(Self.synthesis(operation, "running")))
        continuation.finish(throwing: closed)
        await consumer.value
        #expect(model.processChannelFailed && model.speak.request?.phase == .failed)
        #expect(model.speak.statusLine == RecordingSpeakModel.channelClosedFailure && model.speak.needsAttention)
        #expect(model.speak.unavailableReason == RecordingSpeakModel.channelClosedReason)
        model.speak.applySynthesisEvent(Self.synthesis(operation, "finished"))
        #expect(model.speak.request?.phase == .failed && model.speak.clips.isEmpty)

        // The recorder's stream ending leaves the service and its request running.
        let recorderService = ScriptedSpeak()
        let (recorderClosed, _) = await workspace(recorderService)
        let running = try await speak(recorderClosed, recorderService)
        let (recorderEvents, recorderContinuation) = stream()
        let recorderConsumer = recorderClosed.consume(recorderEvents) { $0.handleRecorder($1) }
        recorderContinuation.finish(throwing: closed)
        await recorderConsumer.value
        #expect(recorderClosed.processChannelFailed && recorderClosed.speak.request?.phase == .queued)
        #expect(recorderClosed.speak.unavailableReason == RecordingSpeakModel.channelClosedReason)
        recorderClosed.handleService(try Self.stream(Self.synthesis(running, "finished")))
        #expect(recorderClosed.speak.request?.phase == .finished && recorderClosed.speak.clips.count == 1)

        // An idle model has nothing to fail.
        let (idle, _) = await workspace(ScriptedSpeak())
        let (idleEvents, idleContinuation) = stream()
        let idleConsumer = idle.consumeService(idleEvents)
        idleContinuation.finish(throwing: closed)
        await idleConsumer.value
        #expect(idle.speak.request == nil && idle.speak.statusLine == nil)

        // The supervisor stops: the request cannot end any more.
        let supervised = ScriptedSpeak()
        let (stopped, _) = await workspace(supervised)
        let pending = try await speak(stopped, supervised)
        stopped.handleService(try Self.stream(Self.synthesis(pending, "running")))
        // Error-only copies of refused replies are not supervisor failures.
        stopped.handleService(try RecordingFineTuneTests.refusal("FINETUNE.JOB_ACTIVE", "a copy of a refused reply"))
        #expect(stopped.speak.request?.phase == .running && stopped.speak.supervisorFailure == nil)
        stopped.handleService(try RecordingFineTuneTests.envelope(.null, error: RecordingFineTuneTests.failure("RECORDING.WRITE_FAILED",
            Self.supervisorFailure, retryable: true), requestID: RecordingWorkspaceModel.fineTuneSupervisorRequestID))
        #expect(stopped.speak.request?.phase == .failed && stopped.speak.request?.message == Self.supervisorFailure)
        #expect(stopped.speak.statusLine == Self.supervisorFailure && stopped.fineTuneStatusLine == Self.supervisorFailure)
        #expect(stopped.speak.unavailableReason == Self.supervisorFailure && !stopped.speak.canCancel)
    }

    // MARK: cancel

    @Test @MainActor func cancellingFollowsTheRequestsStateAndIsSentOnce() async throws {
        // Queued: the reply is its cancelled state.
        let service = ScriptedSpeak()
        service.holding = ["synthesize"]
        let (model, _) = await workspace(service)
        model.speak.speak()
        await waitUntil { service.heldCount == 1 }
        let queued = try #require(model.speak.request?.operationID)
        model.speak.cancel()
        #expect(model.speak.request?.cancelRequested == true && model.speak.statusLine == "Cancelling Speak")
        await waitUntil { service.answered["cancel-synthesis"] == 1 }
        let cancel = try #require(service.sent.last)
        #expect(cancel.command == "cancel-synthesis" && cancel.detail == queued)
        #expect(cancel.operationID.hasPrefix("cancel-speak-") && cancel.operationID != queued)
        #expect(model.speak.request?.phase == .cancelled && model.speak.request?.cancelRequested == false)
        #expect(model.speak.statusLine == "Speak cancelled" && !model.speak.needsAttention && model.speak.canSpeak)
        service.release("synthesize")
        await waitUntil { service.answered["synthesize"] == 1 }
        #expect(model.speak.request?.phase == .cancelled, "the late queued reply changes nothing")

        // Running: the reply is still running; the request shows cancelling until it ends.
        let runningService = ScriptedSpeak()
        let (running, _) = await workspace(runningService)
        let job = try await speak(running, runningService)
        running.handleService(try Self.stream(Self.synthesis(job, "running")))
        runningService.holding = ["cancel-synthesis"]
        runningService.cancelReply = { try RecordingFineTuneTests.envelope(Self.synthesis($0, "running")) }
        running.speak.cancel()
        await waitUntil { runningService.heldCount == 1 }
        running.speak.cancel()
        #expect(runningService.commands == ["synthesize", "cancel-synthesis"], "one cancel per request")
        runningService.release("cancel-synthesis")
        await waitUntil { runningService.answered["cancel-synthesis"] == 1 }
        #expect(running.speak.request?.phase == .running && running.speak.request?.cancelRequested == true)
        #expect(running.speak.statusLine == "Cancelling Speak" && running.speak.unavailableReason == RecordingSpeakModel.cancellingReason)
        running.handleService(try Self.stream(Self.synthesis(job, "cancelled")))
        #expect(running.speak.request?.phase == .cancelled && running.speak.request?.cancelRequested == false)
        #expect(running.speak.canSpeak && !running.speak.canCancel)

        // Already ended: the reply is its final state.
        let endedService = ScriptedSpeak()
        let (ended, _) = await workspace(endedService)
        let finished = try await speak(ended, endedService)
        endedService.cancelReply = { try RecordingFineTuneTests.envelope(Self.synthesis($0, "finished")) }
        ended.speak.cancel()
        await waitUntil { endedService.answered["cancel-synthesis"] == 1 }
        #expect(ended.speak.request?.phase == .finished && ended.speak.clips.map(\.id) == [finished])

        // Refused: explained, and it can be tried again; the next state replaces the notice.
        let refusedService = ScriptedSpeak()
        let (refused, _) = await workspace(refusedService)
        let lost = try await speak(refused, refusedService)
        refusedService.cancelReply = { try RecordingFineTuneTests.refusal("RECORDING.INVALID_TRANSITION", "no Speak request \($0) is queued or running") }
        refused.speak.cancel()
        await waitUntil { refusedService.answered["cancel-synthesis"] == 1 }
        #expect(refused.speak.request?.phase == .queued && refused.speak.request?.cancelRequested == false)
        #expect(refused.speak.statusLine == "no Speak request \(lost) is queued or running" && refused.speak.canCancel)
        refused.handleService(try Self.stream(Self.synthesis(lost, "running")))
        #expect(refused.speak.statusLine == "Generating…")
    }

    // MARK: playback and the capture interlock

    @Test @MainActor func speakPlaybackYieldsToCaptureAndCannotStartWhileRecording() async throws {
        let backend = SpeechBackend()
        let service = ScriptedSpeak()
        let (model, _) = await workspace(service, backend: backend)
        model.fixtureAssetRoot = URL(fileURLWithPath: "/private/tmp/synthetic-speech-root")
        #expect(model.speak.playUnavailableReason == RecordingSpeakModel.noClipReason)
        let first = try await speak(model, service)
        model.handleService(try Self.stream(Self.synthesis(first, "finished")))
        await waitUntil { model.speak.canPlay }
        let loaded = try #require(model.speak.playback.source)
        #expect(loaded.takeID == first && loaded.format == .speech && loaded.frameCount == 48_000)
        #expect(loaded.relativePath == "derived/speech/\(first)/speech-01.wav" && loaded.sourceSHA256 == Self.clipSHA256)
        #expect(model.speak.playback.retainedRange == 0..<48_000 && model.speak.playback.phase == .stopped, "never autoplays")
        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .playing }

        // A second request is running when a take starts.
        let second = try await speak(model, service)
        let stopsBefore = await backend.stops
        try await model.beginCaptureInterlock()
        #expect(model.speak.playback.captureOwned && model.speak.playback.phase == .stopped)
        #expect(await backend.stops > stopsBefore, "Speak output stopped before capture")
        model.state.phase = .recording
        #expect(!model.speak.canPlay && model.speak.playUnavailableReason == RecordingSpeakModel.playbackDuringCaptureReason)
        #expect(model.speak.unavailableReason == RecordingSpeakModel.recordingReason)
        let plays = await backend.plays
        model.speak.playPause()
        model.speak.seek(to: 100)
        for _ in 0..<50 { await Task.yield() }
        #expect(await backend.plays == plays)
        // It finishes during the take: listed and selected, loaded once capture ends.
        model.handleService(try Self.stream(Self.synthesis(second, "finished")))
        #expect(model.speak.selectedClipID == second && model.speak.clips.map(\.id) == [second, first])
        for _ in 0..<50 { await Task.yield() }
        #expect(model.speak.playback.source?.takeID == first)
        // The seal arrives: capture ends while the take is still being registered.
        model.state.phase = .stopping
        model.endCaptureInterlock()
        #expect(!model.speak.playback.captureOwned && !model.review.playback.captureOwned)
        await waitUntil { model.speak.playback.source?.takeID == second }
        #expect(model.speak.playUnavailableReason == RecordingSpeakModel.playbackDuringCaptureReason, "still stopping")
        model.state.phase = .draftReview
        #expect(model.speak.canPlay)
        // Choosing the earlier clip loads it again.
        model.speak.selectClip(first)
        await waitUntil { model.speak.playback.source?.takeID == first }
        model.speak.selectClip("speak-unknown")
        #expect(model.speak.selectedClipID == first)
    }

    @Test @MainActor func aFailedSpeakOutputHoldsRecordBackUntilRecovered() async throws {
        let backend = SpeechBackend()
        let service = ScriptedSpeak()
        let (model, _) = await workspace(service, backend: backend)
        model.fixtureAssetRoot = URL(fileURLWithPath: "/private/tmp/synthetic-speech-root")
        model.state.contextReady = true; model.state.deviceReady = true; model.state.storageReady = true
        model.state.serviceReady = true; model.state.permission = .granted
        #expect(model.canRecord && model.speak.captureBlocker == nil)
        let operation = try await speak(model, service)
        model.handleService(try Self.stream(Self.synthesis(operation, "finished")))
        await waitUntil { model.speak.canPlay }
        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .playing }
        await backend.failOutput()
        await waitUntil { model.speak.playback.failure != nil }
        let blocker = "Speak playback failed: Audio output configuration changed; review playback before continuing · recover audio on the Speak page"
        #expect(model.speak.captureBlocker == blocker)
        #expect(!model.canRecord && model.recordUnavailableReason == blocker)
        #expect(model.speak.playUnavailableReason == "Audio output configuration changed; review playback before continuing")
        await #expect(throws: RecordingContractError.self) { try await model.beginCaptureInterlock() }
        #expect(model.review.playback.captureOwned && !model.speak.playback.captureOwned)
        // The refused start releases the transports again (handleStartFailure → captureStopped).
        model.endCaptureInterlock()
        model.speak.recoverPlayback()
        await waitUntil { model.speak.playback.failure == nil && model.speak.canPlay }
        #expect(model.canRecord && model.speak.captureBlocker == nil)
        try await model.beginCaptureInterlock()
        #expect(model.speak.playback.captureOwned)
        model.endCaptureInterlock()

        // A clip that cannot be read is the clip's problem, never Record's.
        await backend.failTheNextLoad()
        let next = try await speak(model, service)
        model.handleService(try Self.stream(Self.synthesis(next, "finished")))
        await waitUntil { model.speak.clipFailure != nil }
        #expect(model.speak.clipFailure == "Selected playback source no longer matches its checksum")
        await waitUntil { model.speak.playback.failure == nil && model.speak.playback.phase == .stopped }
        #expect(model.speak.captureBlocker == nil && model.canRecord)
        #expect(model.speak.playback.source?.takeID == operation, "the earlier clip stays loaded")
        #expect(model.speak.selectedClipID == next && model.speak.playUnavailableReason == model.speak.clipFailure)
    }

    @Test @MainActor func closingAndSleepingStopSpeakPlayback() async throws {
        let backend = SpeechBackend()
        let service = ScriptedSpeak()
        let (model, _) = await workspace(service, backend: backend)
        model.fixtureAssetRoot = URL(fileURLWithPath: "/private/tmp/synthetic-speech-root")
        let operation = try await speak(model, service)
        model.handleService(try Self.stream(Self.synthesis(operation, "finished")))
        await waitUntil { model.speak.canPlay }
        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .playing }
        let beforeSleep = await backend.stops
        model.handleSystemSleep()
        await waitUntil { model.speak.playback.phase == .stopped }
        #expect(await backend.stops > beforeSleep)
        model.handleSystemWake()

        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .playing }
        let beforeClose = await backend.stops
        var closed = false
        model.closeHandler = { closed = true }
        model.requestClose()
        await waitUntil { closed }
        #expect(model.speak.playback.phase == .stopped)
        #expect(await backend.stops > beforeClose)

        // A Speak output that cannot stop keeps the window open.
        let stuck = SpeechBackend()
        let stuckService = ScriptedSpeak()
        let (open, _) = await workspace(stuckService, backend: stuck)
        open.fixtureAssetRoot = URL(fileURLWithPath: "/private/tmp/synthetic-speech-root")
        let stuckOperation = try await speak(open, stuckService)
        open.handleService(try Self.stream(Self.synthesis(stuckOperation, "finished")))
        await waitUntil { open.speak.canPlay }
        await stuck.setStopFailure()
        var kept = false, finished = false
        open.closeHandler = { finished = true }
        open.closeCancellationHandler = { kept = true }
        open.requestClose()
        await waitUntil { kept }
        #expect(!finished && open.speak.playback.failure?.code == "RECORDING.PLAYBACK_FAILED")
        #expect(open.review.failure == nil, "a Speak failure is not shown as a review failure")
        #expect(open.speak.playbackNotice == "Audio output did not stop; capture remains unavailable")
    }

    @Test @MainActor func aFinishedClipPlaysFromTheLibraryAsSpeechAndAnAlteredOneIsRefused() async throws {
        let library = try SpeechLibrary()
        defer { library.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let service = ScriptedSpeak()
        let (model, _) = await workspace(service, backend: backend)
        model.fixtureAssetRoot = library.root
        let first = try await speak(model, service)
        let clip = try library.write(operation: first, frames: 2_400)
        model.handleService(try Self.stream(Self.synthesis(first, "finished", frames: 2_400, sha256: clip.sha256)))
        await waitUntil { model.speak.canPlay || model.speak.clipFailure != nil }
        #expect(model.speak.clipFailure == nil && model.speak.playback.source?.format == .speech)
        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .playing }
        let rendered = try await backend.renderOffline(frames: 480)
        #expect(rendered.count == 480)
        for (index, sample) in rendered.enumerated() { #expect(abs(sample - Float(clip.samples[index]) / 32_768) < 0.000001) }
        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .paused }

        // A clip whose bytes no longer match: the clip says why, and Record is not held back.
        let second = try await speak(model, service)
        _ = try library.write(operation: second, frames: 2_400)
        model.handleService(try Self.stream(Self.synthesis(second, "finished", frames: 2_400,
                                                           sha256: String(repeating: "0", count: 64))))
        await waitUntil { model.speak.clipFailure != nil }
        #expect(model.speak.clipFailure == "Selected playback source no longer matches its checksum")
        await waitUntil { model.speak.playback.failure == nil && model.speak.playback.phase == .stopped }
        #expect(model.speak.captureBlocker == nil && model.speak.playUnavailableReason == model.speak.clipFailure)
        #expect(model.speak.playback.source?.takeID == first, "the earlier clip stays loaded")
        // Choosing the earlier clip plays it again.
        model.speak.selectClip(first)
        #expect(model.speak.clipFailure == nil && model.speak.canPlay)
    }
}

/// A private asset root with Speak clips written as the service writes them (24 kHz, 16-bit,
/// mono, 0600) under `derived/speech/<operation id>/speech-01.wav`.
private struct SpeechLibrary {
    let root: URL

    init() throws {
        let temporary = try #require(realpath(FileManager.default.temporaryDirectory.path, nil))
        defer { free(temporary) }
        root = URL(fileURLWithPath: String(cString: temporary), isDirectory: true)
            .appendingPathComponent("cole-speak-\(UUID().uuidString.lowercased())", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    }

    func write(operation: String, frames: Int) throws -> (sha256: String, samples: [Int16]) {
        let directory = root.appendingPathComponent("derived/speech/\(operation)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let samples = (0..<frames).map { Int16(($0 % 97) * 600 - 29_000) }
        var wav = Data("RIFF".utf8)
        func u32(_ value: UInt32) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        func u16(_ value: UInt16) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        u32(UInt32(36 + frames * 2)); wav.append(Data("WAVEfmt ".utf8)); u32(16)
        u16(1); u16(1); u32(24_000); u32(48_000); u16(2); u16(16)
        wav.append(Data("data".utf8)); u32(UInt32(frames * 2))
        for sample in samples { u16(UInt16(bitPattern: sample)) }
        let file = directory.appendingPathComponent("speech-01.wav")
        try wav.write(to: file, options: .withoutOverwriting)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        return (SHA256.hash(data: wav).map { String(format: "%02x", $0) }.joined(), samples)
    }

    func remove() { try? FileManager.default.removeItem(at: root) }
}
