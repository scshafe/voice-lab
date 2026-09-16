import AppKit
import CryptoKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingUI
import Testing

private struct ReviewFixture {
    let root: URL
    let bytes: Data
    let source: RecordingReviewSource
    init(samples: [Int32], takeID: String = "synthetic") throws {
        let parent = try #require(realpath(FileManager.default.temporaryDirectory.path, nil)); defer { free(parent) }
        root = URL(fileURLWithPath: String(cString: parent), isDirectory: true).appendingPathComponent("cole-review-\(UUID().uuidString.lowercased())", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        var wav = Data("RIFF".utf8)
        func u32(_ value: UInt32) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        func u16(_ value: UInt16) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        u32(UInt32(36 + samples.count * 3)); wav.append(Data("WAVEfmt ".utf8)); u32(16)
        u16(1); u16(1); u32(48_000); u32(144_000); u16(3); u16(24); wav.append(Data("data".utf8)); u32(UInt32(samples.count * 3))
        for sample in samples { wav.append(contentsOf: [UInt8(truncatingIfNeeded: sample), UInt8(truncatingIfNeeded: sample >> 8), UInt8(truncatingIfNeeded: sample >> 16)]) }
        bytes = wav
        let path = root.appendingPathComponent("original.wav")
        try wav.write(to: path, options: .withoutOverwriting)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        var identity = stat(); guard lstat(path.path, &identity) == 0 else { throw POSIXError(.EIO) }
        let digest = SHA256.hash(data: wav).map { String(format: "%02x", $0) }.joined()
        let raw: RecordingJSON = .object([
            "take_id": .string(takeID), "subject_id": .string("synthetic"), "catalog_sha256": .string(String(repeating: "a", count: 64)),
            "prompt": .object(["prompt_id": .string("reading")]), "source_path": .string("original.wav"), "source_sha256": .string(digest), "source_frames": .integer(Int64(samples.count)),
            "source_identity": .object(["relative_path": .string("original.wav"), "device": .integer(Int64(identity.st_dev)), "inode": .integer(Int64(identity.st_ino)), "size_bytes": .integer(identity.st_size), "mode": .integer(Int64(identity.st_mode)), "mtime_ns": .integer(Int64(identity.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(identity.st_mtimespec.tv_nsec)), "ctime_ns": .integer(Int64(identity.st_ctimespec.tv_sec) * 1_000_000_000 + Int64(identity.st_ctimespec.tv_nsec))]),
            "frame_range": .object(["source_frames": .integer(Int64(samples.count)), "start_frame": .integer(0), "end_frame": .integer(Int64(samples.count))]),
            "decision": .string("draft"), "decision_revision": .integer(0), "edit_revision": .integer(0), "analysis": .string("pending"), "export": .string("local"),
            "recorded_at": .null, "date_precision": .string("unknown"), "original_kind": .string("native-original"), "edit_recipe_sha256": .null, "eligibility_messages": .array([]),
        ])
        source = try RecordingReviewSource(raw, assetRoot: root)
    }
    func unchanged() throws -> Bool { try Data(contentsOf: root.appendingPathComponent("original.wav")) == bytes }
    func remove() throws { try FileManager.default.removeItem(at: root) }
}

private final class DiagnosticEvents: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [RecordingDiagnosticEvent] = []
    var sink: RecordingDiagnosticSink { { [self] event in lock.lock(); stored.append(event); lock.unlock() } }
    var events: [RecordingDiagnosticEvent] { lock.lock(); defer { lock.unlock() }; return stored }
    func named(_ name: String, phase: RecordingDiagnosticEvent.Phase) -> [RecordingDiagnosticEvent] {
        events.filter { $0.name == name && $0.phase == phase }
    }
}

@Suite(.serialized) struct RecordingSourceDiagnosticTests {
    @Test func retainedLeaseReportsEveryHashAndReadWithoutInventingAnotherOpen() throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 0, count: 128)); defer { try? fixture.remove() }
        let evidence = DiagnosticEvents()
        let context = RecordingDiagnosticContext(traceID: "retained-lease", operationID: "review-positive-control", takeID: "synthetic")
        let lease = try RecordingPrivateFile.openReadOnly(fixture.root.appendingPathComponent("original.wav").path,
            maximumBytes: 4096, diagnosticContext: context, diagnosticSink: evidence.sink)
        #expect(try lease.sha256() == fixture.source.playback.sourceSHA256)
        #expect(try lease.sha256() == fixture.source.playback.sourceSHA256)
        #expect(evidence.named("private_file.open", phase: .begin).count == 1)
        let opens = evidence.named("private_file.open", phase: .end)
        #expect(opens.count == 1 && opens.first?.outcome == .success)
        #expect(opens.first?.device != nil && opens.first?.inode != nil)
        for name in ["private_file.sha256", "private_file.read"] {
            let reads = evidence.named(name, phase: .end)
            #expect(reads.count == 2)
            #expect(reads.allSatisfy { $0.outcome == .success && $0.byteCount == Int64(fixture.bytes.count) })
            #expect(reads.allSatisfy { $0.context.traceID == "retained-lease" && $0.context.operationID == "review-positive-control" })
            #expect(reads.allSatisfy { $0.context.sourcePath == fixture.root.appendingPathComponent("original.wav").path && $0.context.parentSpanID == opens.first?.spanID })
        }
    }

    @Test func failedOpenRetainsAttemptAndTypedFailureWithoutSuccessfulIdentity() throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 0, count: 128)); defer { try? fixture.remove() }
        let evidence = DiagnosticEvents()
        let path = fixture.root.appendingPathComponent("missing.wav").path
        #expect(throws: RecordingContractError.self) {
            try RecordingPrivateFile.openReadOnly(path, maximumBytes: 4096, diagnosticSink: evidence.sink)
        }
        let begin = try #require(evidence.named("private_file.open", phase: .begin).first)
        let end = try #require(evidence.named("private_file.open", phase: .end).first)
        #expect(begin.context.sourcePath == path && begin.spanID == end.spanID)
        #expect(end.outcome == .failure && end.errorCode == "RECORDING.SOURCE_CHANGED")
        #expect(end.device == nil && end.inode == nil && end.byteCount == nil)
        #expect(evidence.named("private_file.read", phase: .begin).isEmpty)
    }

    @Test func nilSinkDoesNotCreateDiagnosticIdentityOrChangeVerifiedReads() throws {
        let span = RecordingDiagnosticSpan("disabled", sink: nil)
        #expect(span.spanID == nil && span.context == nil && span.childContext == nil)
        span.finish(.failure)
        let fixture = try ReviewFixture(samples: Array(repeating: 0, count: 128)); defer { try? fixture.remove() }
        let lease = try RecordingPrivateFile.openReadOnly(fixture.root.appendingPathComponent("original.wav").path, maximumBytes: 4096)
        #expect(try lease.sha256() == fixture.source.playback.sourceSHA256)
    }

    @Test func selectedCacheMissAndHitExposeDifferentRealSourceWork() throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 1, count: 128)); defer { try? fixture.remove() }
        let miss = DiagnosticEvents()
        let cache = try RecordingWaveformSelectionLoader.load(fixture.source.playback, processGeneration: "diagnostic-generation", diagnosticSink: miss.sink)
        #expect(miss.named("waveform.cache_load", phase: .end).first?.outcome == .cacheMiss)
        #expect(miss.named("private_file.sha256", phase: .end).count == 1)
        #expect(miss.named("waveform.av_audio_file_open", phase: .end).count == 1)
        #expect(miss.named("waveform.decode", phase: .end).first?.byteCount == 384)
        let result = RecordingWaveformArchiveStore.persist(try cache.archive(sourceSHA256: fixture.source.playback.sourceSHA256, finalWrittenFrames: 128),
            sourceSHA256: fixture.source.playback.sourceSHA256, assetRoot: fixture.root)
        guard case .stored = result else { Issue.record("Synthetic cache fixture did not publish"); return }
        let hit = DiagnosticEvents()
        let loaded = try RecordingWaveformSelectionLoader.load(fixture.source.playback, processGeneration: "diagnostic-generation", diagnosticSink: hit.sink)
        #expect(try loaded.snapshot().writtenFrames == 128)
        #expect(hit.named("waveform.cache_load", phase: .end).first?.outcome == .cacheHit)
        #expect(hit.named("private_file.open", phase: .end).count == 1)
        #expect(hit.named("private_file.sha256", phase: .begin).isEmpty)
        #expect(hit.named("waveform.av_audio_file_open", phase: .begin).isEmpty)
        #expect(hit.named("waveform.decode", phase: .begin).isEmpty)
    }

    @Test @MainActor func parallelReviewBranchesCarryOnePreparationAndExactSelectedSource() async throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 1, count: 4800)); defer { try? fixture.remove() }
        let evidence = DiagnosticEvents()
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline, diagnosticSink: evidence.sink)
        let review = RecordingReviewController(backend: backend, diagnosticSink: evidence.sink)
        let context = RecordingDiagnosticContext(traceID: "stop-cycle", operationID: "review-operation", requestID: "automatic-request")
        let prepared = try await review.prepare(fixture.source, processGeneration: "diagnostic-generation", diagnosticContext: context)
        let parent = try #require(evidence.named("review.prepare", phase: .begin).first)
        let parentEnd = try #require(evidence.named("review.prepare", phase: .end).first)
        #expect(parent.context.preparationID != nil)
        for name in ["waveform.prepare", "playback.prepare"] {
            let begin = try #require(evidence.named(name, phase: .begin).first)
            let end = try #require(evidence.named(name, phase: .end).first)
            #expect(begin.context.preparationID == parent.context.preparationID)
            #expect(begin.context.parentSpanID == parent.spanID)
            #expect(begin.context.operationID == context.operationID && begin.context.requestID == context.requestID)
            #expect(begin.context.takeID == "synthetic" && begin.context.sourceSHA256 == fixture.source.playback.sourceSHA256)
            #expect(begin.uptimeNanoseconds >= parent.uptimeNanoseconds && end.uptimeNanoseconds <= parentEnd.uptimeNanoseconds)
            #expect(end.outcome == .success)
        }
        let waveform = RecordingWaveformModel(takeID: "synthetic", processGeneration: "diagnostic-generation", hostClockComparable: false)
        try review.commit(prepared, waveform: waveform)
        #expect(evidence.named("review.commit", phase: .end).first?.context.preparationID == parent.context.preparationID)
        #expect(evidence.named("private_file.open", phase: .end).count == 2)
        #expect(evidence.named("playback.av_audio_file_open", phase: .end).count == 1)
        #expect(try fixture.unchanged())
        try await backend.recover()
    }
}

@Suite(.serialized) struct RecordingReviewTests {
    @Test @MainActor func microphoneReconnectPreservesUnsavedTrimWithoutStartingOutput() async throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 1, count: 4800)); defer { try? fixture.remove() }
        let model = RecordingWorkspaceModel(fixture: .init(phase: .draftReview))
        try model.review.commit(try await model.review.prepare(fixture.source, processGeneration: "synthetic-generation"), waveform: model.waveform)
        model.review.setStartText("123"); model.review.setEndText("3210")
        let range = model.review.trimRange
        model.requireAudioReconnect(after: .deviceUnavailable)
        #expect(model.review.isDirty && !model.canReconnectAudio)
        await model.reconnectAudio()
        #expect(model.review.trimRange == range && model.review.isDirty)
        #expect(model.review.playback.phase == .stopped)
        #expect(try fixture.unchanged())
        try await model.review.playback.shutdown()
    }
    @Test func selectedRebuildPreservesExactSilenceQuietClippingAndGeneration() throws {
        let samples: [Int32] = Array(repeating: 0, count: 64) + [1, -1] + Array(repeating: 0, count: 62) + [8_388_607, -8_388_608] + Array(repeating: 0, count: 62)
        let fixture = try ReviewFixture(samples: samples); defer { try? fixture.remove() }
        let cache = try RecordingWaveformSelectionLoader.load(fixture.source.playback, processGeneration: "review-generation")
        let snapshot = try cache.snapshot()
        #expect(snapshot.processGeneration == "review-generation" && snapshot.sampleClock == nil && snapshot.writtenFrames == 192)
        #expect(snapshot.buckets.count == 3)
        #expect(snapshot.buckets[0].minimum == 0 && snapshot.buckets[0].maximum == 0)
        #expect(snapshot.buckets[1].minimum == -1 && snapshot.buckets[1].maximum == 1)
        #expect(snapshot.buckets[2].minimum == -8_388_608 && snapshot.buckets[2].maximum == 8_388_607)
        #expect(try fixture.unchanged())
    }
    @Test func selectedDescriptorIdentityIsRequiredEvenWhenDigestMatches() async throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 0, count: 128)); defer { try? fixture.remove() }
        var raw = try #require(fixture.source.raw.object)
        var identity = try #require(raw["source_identity"]?.object); identity["inode"] = .integer(1); raw["source_identity"] = .object(identity)
        let wrong = try RecordingReviewSource(.object(raw), assetRoot: fixture.root)
        #expect(throws: RecordingContractError.self) { try RecordingWaveformSelectionLoader.load(wrong.playback, processGeneration: "test") }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        await #expect(throws: RecordingContractError.self) { try await backend.load(wrong.playback, generation: 1) }
        #expect(try fixture.unchanged())
    }
    @Test @MainActor func numericTrimPreviewNeverAutoplaysAndPreservesOriginal() async throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 1, count: 4800)); defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let review = RecordingReviewController(backend: backend)
        let waveform = RecordingWaveformModel(takeID: "synthetic", processGeneration: "generation", hostClockComparable: false)
        try review.commit(try await review.prepare(fixture.source, processGeneration: "generation"), waveform: waveform)
        review.setStartText("123"); review.setEndText("3210")
        #expect(review.trimRange == 123..<3210 && review.isDirty && review.fieldError == nil)
        review.setStartText("1.5"); #expect(review.fieldError != nil && review.trimRange == 123..<3210)
        review.setStartText("999999999999999999999"); #expect(review.fieldError != nil)
        review.setStartText("123"); review.discardPreview()
        #expect(review.trimRange == 0..<4800 && !review.isDirty && review.playback.phase == .stopped)
        try await review.playback.shutdown()
        #expect(try fixture.unchanged())
    }
    @Test @MainActor func nativeKeyEventsHonorModifiersRepeatsTextAndWaveformFocus() throws {
        func event(_ text: String, code: UInt16 = 15, flags: NSEvent.ModifierFlags = [], repeatKey: Bool = false) throws -> NSEvent {
            try #require(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0, context: nil, characters: text, charactersIgnoringModifiers: text, isARepeat: repeatKey, keyCode: code))
        }
        let normal = RecordingKeyboardContext(editingText: false, dialogOrPopup: false, waveformFocused: false)
        #expect(RecordingKeyboardRouter.action(for: try event("r"), context: normal) == .recordOrStop)
        #expect(RecordingKeyboardRouter.action(for: try event(" "), context: normal) == .playPause)
        #expect(RecordingKeyboardRouter.action(for: try event("s", flags: .command), context: normal) == .save(advance: false))
        #expect(RecordingKeyboardRouter.action(for: try event("\r", code: 36), context: normal) == .save(advance: true))
        #expect(RecordingKeyboardRouter.action(for: try event("r", repeatKey: true), context: normal) == nil)
        for context in [RecordingKeyboardContext(editingText: true, dialogOrPopup: false, waveformFocused: true), RecordingKeyboardContext(editingText: false, dialogOrPopup: true, waveformFocused: true)] {
            for text in ["r", " ", "s", "\r"] { #expect(RecordingKeyboardRouter.action(for: try event(text), context: context) == nil) }
        }
        #expect(RecordingKeyboardRouter.action(for: try event("", code: 123), context: normal) == nil)
        #expect(RecordingKeyboardRouter.action(for: try event("", code: 123), context: RecordingKeyboardContext(editingText: false, dialogOrPopup: false, waveformFocused: true)) == .seek(frames: -4800))
    }
    @Test @MainActor func hostedNativeMonitorLeavesActualFieldEditorAndPopupUntouched() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let model = RecordingWorkspaceModel(fixture: RecordingShellState())
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 100), styleMask: [.titled], backing: .buffered, defer: false)
        let view = RecordingWorkspaceKeyboardView(model: model); window.contentView = view
        let editor = NSTextView(frame: NSRect(x: 0, y: 0, width: 100, height: 20)); view.addSubview(editor); editor.isEditable = true
        let key = try #require(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber, context: nil, characters: "r", charactersIgnoringModifiers: "r", isARepeat: false, keyCode: 15))
        try #require(key.window === window)
        #expect(window.makeFirstResponder(editor)); #expect(view.handle(key) === key)
        editor.isEditable = false; editor.string = "selected script"; editor.setSelectedRange(NSRange(location: 0, length: 3))
        #expect(view.handle(key) === key)
        let popup = NSPopUpButton(frame: NSRect(x: 0, y: 30, width: 100, height: 20)); view.addSubview(popup)
        #expect(window.makeFirstResponder(popup)); #expect(view.handle(key) === key)
        let focus = RecordingWaveformFocusView(); view.addSubview(focus); #expect(window.makeFirstResponder(focus))
        NotificationCenter.default.post(name: NSMenu.didBeginTrackingNotification, object: NSMenu())
        #expect(view.handle(key) === key)
        NotificationCenter.default.post(name: NSMenu.didEndTrackingNotification, object: NSMenu())
        #expect(view.handle(key) == nil)
        view.uninstall(); window.contentView = nil
    }
    @Test @MainActor func staleDerivativeCannotChangeSelectedSourceAndEarlyNextRevisionIsBounded() async throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 0, count: 128)); defer { try? fixture.remove() }
        let model = RecordingWorkspaceModel(fixture: RecordingShellState())
        let prepared = try await model.review.prepare(fixture.source, processGeneration: "generation")
        try model.review.commit(prepared, waveform: model.waveform)
        model.state.selection = .object(["take_id": .string("synthetic")]); model.state.phase = .ready
        var raw = try #require(fixture.source.raw.object); raw["edit_revision"] = .integer(1)
        let recipe = String(repeating: "a", count: 64); raw["edit_recipe_sha256"] = .string(recipe)
        try model.review.updateAuthorization(RecordingReviewSource(.object(raw), assetRoot: fixture.root))
        var event: [String: RecordingJSON] = ["event": .string("derivative"), "take_id": .string("other-take"), "source_sha256": .string(fixture.source.playback.sourceSHA256), "edit_revision": .integer(1), "recipe_sha256": .string(recipe), "state": .string("ready")]
        model.handleDerivative(.object(event)); #expect(model.reviewDerivativeState == nil)
        event["take_id"] = .string("synthetic"); event["recipe_sha256"] = .string(String(repeating: "b", count: 64))
        model.handleDerivative(.object(event)); #expect(model.reviewDerivativeState == nil)
        event["recipe_sha256"] = .string(recipe)
        model.handleDerivative(.object(event)); #expect(model.reviewDerivativeState == "ready")
        model.state.phase = .saving; model.state.activeOperationID = "edit-next"
        event["edit_revision"] = .integer(2); model.handleDerivative(.object(event))
        #expect(model.deferredDerivativeEvent == .object(event))
        event["take_id"] = .string("stale-take"); model.handleDerivative(.object(event))
        #expect(model.deferredDerivativeEvent?.object?["take_id"] == .string("synthetic"))
        #expect(try fixture.unchanged()); try await model.review.playback.shutdown()
    }
    @Test @MainActor func closeRequiresExplicitUnsavedTrimChoiceAndStopsOutput() async throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 0, count: 128)); defer { try? fixture.remove() }
        let model = RecordingWorkspaceModel(fixture: RecordingShellState())
        try model.review.commit(try await model.review.prepare(fixture.source, processGeneration: "generation"), waveform: model.waveform)
        model.review.setStartText("1")
        var cancelled = false
        model.closeCancellationHandler = { cancelled = true }
        model.requestClose(); #expect(model.reviewConfirmation != nil)
        model.cancelReviewConfirmation(); #expect(cancelled && model.review.isDirty)
        let closed = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
        model.closeHandler = { closed.continuation.yield(()); closed.continuation.finish() }
        model.requestClose(); model.discardTrimAndContinue()
        for await _ in closed.stream { break }
        #expect(model.review.playback.phase == .stopped && !model.review.isDirty)
        #expect(try fixture.unchanged())
    }

    /// Fixture envelopes bypass only wire-shape validation; shared conformance suites own it.
    private static func envelope(_ data: RecordingJSON, requestID: String = "fixture-request", takeID: String? = "synthetic-take") throws -> RecordingWireEnvelope {
        let body: RecordingJSON = .object(["data": data, "error": .null,
            "meta": .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string(requestID),
                "process_generation": .string("synthetic-generation"), "state_revision": .integer(2), "event_sequence": .integer(3),
                "take_id": takeID.map(RecordingJSON.string) ?? .null])])
        return try RecordingWireEnvelope.decode(body.transportData(), schema: Data(#"{"type":"object"}"#.utf8))
    }
    @MainActor private final class RequestLog {
        var events: [String] = []
        private var waiters: [CheckedContinuation<Void, Never>] = []
        func record(_ event: String) { events.append(event) }
        /// Hold a scripted reply until the test releases it.
        func hold() async { await withCheckedContinuation { waiters.append($0) } }
        func release() { let resumed = waiters; waiters.removeAll(); for waiter in resumed { waiter.resume() } }
    }
    /// A sequencing step that never settles is a failure, not a hang.
    @MainActor private static func settle(_ what: String, until predicate: @MainActor () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while !predicate() {
            guard ContinuousClock.now < deadline else { throw RecordingContractError(code: "RECORDING.REVIEW_TIMEOUT", message: what) }
            try await Task.sleep(for: .milliseconds(2))
        }
    }

    /// P5 (§12 row 5): once the seal registration reply is applied, the review source is requested
    /// while the history page is still outstanding; the draft is reviewable and savable before that
    /// page lands, its review never autoplays, and the page still applies afterwards.
    @Test @MainActor func sealedReviewIsRequestedBeforeTheHistoryPageReplies() async throws {
        let fixture = try ReviewFixture(samples: Array(repeating: 1, count: 4800), takeID: "synthetic-take"); defer { try? fixture.remove() }
        let model = RecordingWorkspaceModel(fixture: .init(phase: .recording))
        model.fixtureAssetRoot = fixture.root
        model.startOperationID = "synthetic-start"; model.inputStartDispatched = true
        model.state.take = .object(["reservation": .object(["take_id": .string("synthetic-take"), "operation_id": .string("synthetic-start")]),
                                    "decision": .string("draft"), "analysis": .string("pending"), "seal": .null])
        let log = RequestLog()
        defer { log.release() }
        model.registerSealRequest = { receipt, operation, _ in
            log.record("register-seal")
            return try Self.envelope(.object(["event": .string("take"), "take": .object([
                "reservation": .object(["take_id": .string("synthetic-take"), "operation_id": .string("synthetic-start")]), "seal": receipt,
                "decision": .string("draft"), "decision_revision": .integer(0), "edit_revision": .integer(0), "analysis": .string("pending")])]), requestID: operation)
        }
        model.historyRequest = { _, _ in
            log.record("history-request")
            await log.hold()
            log.record("history-reply")
            return try Self.envelope(.object(["event": .string("history"), "catalog_sha256": .string(String(repeating: "a", count: 64)),
                "index_revision": .integer(7), "completeness": .string("complete"), "covered_prompt_count": .integer(1), "total_prompt_count": .integer(1),
                "takes": .array([]), "prompts": .array([.object(["prompt_id": .string("natural-reading"), "status": .string("draft")])]),
                "next_cursor": .null, "next_prompt_cursor": .null]), takeID: nil)
        }
        model.reviewRequest = { takeID, _, operation, _ in
            log.record("review-request")
            return try Self.envelope(.object(["event": .string("review"), "source": fixture.source.raw, "cursor": .object([
                "selection": .object(["catalog_sha256": .string(String(repeating: "a", count: 64)), "prompt_id": .string("natural-reading"),
                                      "take_id": .string(takeID), "selection_revision": .integer(3)]), "complete": .boolean(false)])]), requestID: operation)
        }
        model.stop()
        model.handleRecorder(try Self.envelope(.object(["event": .string("recorder-state"), "take_id": .string("synthetic-take"), "transport": .string("sealed"),
            "seal": .object(["take_id": .string("synthetic-take"), "original_sha256": .string(fixture.source.playback.sourceSHA256),
                             "audio": .object(["frames": .integer(4800)])])]), requestID: "stop-request"))
        try await Self.settle("review committed while the history page is outstanding") { model.selectedReview != nil && !model.review.loading }
        #expect(log.events == ["register-seal", "history-request", "review-request"])
        #expect(model.state.phase == .draftReview && model.canSave && model.state.historyRevision == 0 && model.historyRefreshInFlight)
        #expect(model.review.playback.phase == .stopped && model.selectedReview?.playback.takeID == "synthetic-take")
        log.release()
        try await Self.settle("history page applied") { model.state.historyRevision == 7 && !model.historyRefreshInFlight }
        #expect(log.events.last == "history-reply" && model.state.promptStatuses["natural-reading"] == "draft")
        #expect(model.state.phase == .draftReview && model.canSave && model.review.playback.phase == .stopped)
        try await model.review.playback.shutdown()
        #expect(try fixture.unchanged())
    }
}
