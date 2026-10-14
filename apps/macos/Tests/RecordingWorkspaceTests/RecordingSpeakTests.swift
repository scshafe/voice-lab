import AppKit
import ApplicationServices
import CryptoKit
import Darwin
import Foundation
import RecordingContracts
@testable import RecordingCore
@testable import RecordingUI
import SwiftUI
import Testing

/// A playback backend that never opens audio and counts what it was asked to do.
private actor QuietBackend: RecordingPlaybackBackend {
    var plays = 0
    var pauses = 0
    private var handler: (@Sendable (RecordingPlaybackBackendEvent) async -> Void)?
    func setEventHandler(_ handler: @escaping @Sendable (RecordingPlaybackBackendEvent) async -> Void) { self.handler = handler }
    func load(_ source: RecordingPlaybackSource, generation: UInt64) {}
    func play(from frame: Int64, through endFrame: Int64, generation: UInt64) { plays += 1 }
    func pause() -> Int64? { pauses += 1; return 120 }
    func position() -> Int64? { 120 }
    func stop() {}
    func recover() {}
}

/// The Speak page as hosted views: its controls and their words, the page switch, Save As, and
/// the Record keyboard monitor. Windows are never ordered; no process or audio device is opened.
@Suite(.serialized)
struct RecordingSpeakTests {
    typealias Script = RecordingSpeakModelTests.ScriptedSpeak
    static let speakIdentifiers = ["speak.text", "speak.model", "speak.reference", "speak.generate", "speak.cancel",
                                   "speak.play-pause", "speak.position", "speak.clips", "speak.save-as", "speak.status"]
    static let size = CGSize(width: 1060, height: 700)

    // MARK: fixtures

    /// A workspace whose Speak and fine-tune requests are scripted, with three finished runs.
    @MainActor private func workspace(_ speak: Script, runs: [RecordingJSON] = RecordingSpeakModelTests.threeRuns,
                                      phase: RecordingShellPhase = .ready,
                                      reviewBackend: any RecordingPlaybackBackend = AVFoundationRecordingPlaybackBackend(mode: .offline),
                                      speechBackend: any RecordingPlaybackBackend = QuietBackend()) async -> RecordingWorkspaceModel {
        let model = RecordingWorkspaceModel(fixture: .init(phase: phase), catalog: RecordingSpeakModelTests.catalog,
                                            reviewBackend: reviewBackend, speechBackend: speechBackend)
        model.speak.requests = speak.requests
        let fineTune = RecordingFineTuneTests.ScriptedFineTune()
        fineTune.statusReply = { try RecordingFineTuneTests.envelope(RecordingSpeakModelTests.status($0, runs: runs)) }
        model.fineTuneRequests = fineTune.requests
        await model.requestFineTuneStatus()
        return model
    }

    @MainActor private func waitUntil(_ condition: @MainActor () -> Bool, sourceLocation: SourceLocation = #_sourceLocation) async {
        for _ in 0..<5_000 {
            if condition() { return }
            await Task.yield()
        }
        #expect(condition(), "the model did not settle", sourceLocation: sourceLocation)
    }

    /// Speaks `text` and finishes the request with a clip named by `sha256` and `frames`.
    @MainActor private func finishedClip(_ model: RecordingWorkspaceModel, _ service: Script, text: String = RecordingSpeakModelTests.text,
                                         frames: Int64 = 48_000, sha256: String = RecordingSpeakModelTests.clipSHA256) async throws -> String {
        model.speak.text = text
        let answered = service.answered["synthesize", default: 0]
        model.speak.speak()
        await waitUntil { service.answered["synthesize", default: 0] == answered + 1 }
        let operation = try #require(model.speak.request?.operationID)
        model.handleService(try RecordingSpeakModelTests.stream(RecordingSpeakModelTests.synthesis(
            operation, "finished", text: text, frames: frames, sha256: sha256)))
        return operation
    }

    // MARK: hosting

    struct Node { let identifier: String; let label: String; let texts: [String]; let enabled: Bool }

    @MainActor private static var launched = false

    /// Hosts `view` in a window that is never ordered and hands the live host to `inspect`.
    @MainActor private static func hosting<V: View, R>(_ view: V, size: CGSize = size,
                                                       inspect: @MainActor (NSHostingView<V>) async throws -> R) async throws -> R {
        NSApplication.shared.setActivationPolicy(.prohibited)
        if !launched { NSApplication.shared.finishLaunching(); launched = true }
        let host = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: CGRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        window.contentView = host; host.frame = CGRect(origin: .zero, size: size); host.layoutSubtreeIfNeeded()
        defer { window.contentView = nil; window.close() }
        return try await inspect(host)
    }

    /// Lets SwiftUI apply a model change to a hosted view.
    @MainActor private static func settle(_ host: NSView) async {
        for _ in 0..<5 {
            await Task.yield()
            spin()
            host.layoutSubtreeIfNeeded()
        }
    }

    /// One short turn of the main run loop, where SwiftUI applies pending updates.
    @MainActor private static func spin() {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }

    /// Every identified accessibility element under `host`, with its label, texts and enablement.
    @MainActor private static func nodes(_ host: NSView) -> [Node] {
        var role: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(AXUIElementCreateApplication(getpid()), kAXRoleAttribute as CFString, &role)
        var nodes: [Node] = []
        @discardableResult func visit(_ element: AnyObject, depth: Int) -> [String] {
            guard depth < 40 else { return [] }
            let label = accessibilityText(element, #selector(NSAccessibilityElement.accessibilityLabel))
            let value = accessibilityText(element, #selector(NSAccessibilityElement.accessibilityValue))
            var texts = [label, accessibilityText(element, #selector(NSAccessibilityElement.accessibilityTitle)), value].filter { !$0.isEmpty }
            let identifier = accessibilityText(element, #selector(NSAccessibilityElement.accessibilityIdentifier))
            let object = element as? NSObject
            let enabled = object?.responds(to: NSSelectorFromString("isAccessibilityEnabled")) == true
                ? (object?.value(forKey: "accessibilityEnabled") as? Bool ?? true) : true
            let index = nodes.count
            if !identifier.isEmpty { nodes.append(Node(identifier: identifier, label: label, texts: [], enabled: enabled)) }
            for child in (element.accessibilityChildren?() ?? nil) ?? [] { texts += visit(child as AnyObject, depth: depth + 1) }
            if !identifier.isEmpty { nodes[index] = Node(identifier: identifier, label: label, texts: texts, enabled: enabled) }
            return texts
        }
        visit(host, depth: 0)
        return nodes
    }

    @MainActor private static func views<T: NSView>(_ type: T.Type, in root: NSView) -> [T] {
        ((root as? T).map { [$0] } ?? []) + root.subviews.flatMap { views(type, in: $0) }
    }

    // MARK: the page

    @Test @MainActor func theSpeakPageNamesEveryControlAndHoldsNoRecordMachinery() async throws {
        let service = Script()
        let model = await workspace(service)
        model.fixtureAssetRoot = URL(fileURLWithPath: "/private/tmp/synthetic-speech-root")
        let operation = try await finishedClip(model, service)
        try await Self.hosting(RecordingSpeakView(model: model)) { host in
            await Self.settle(host)
            let nodes = Self.nodes(host)
            let identifiers = Set(nodes.map(\.identifier))
            for required in Self.speakIdentifiers { #expect(identifiers.contains(required), "\(required) is missing") }
            func node(_ identifier: String) throws -> Node { try #require(nodes.first { $0.identifier == identifier }, "\(identifier)") }
            #expect(try node("speak.text").label == "Text to speak")
            #expect(try node("speak.model").label == "Model")
            #expect(try node("speak.generate").label == "Speak" && node("speak.generate").enabled)
            #expect(try node("speak.cancel").label == "Cancel speech generation" && !node("speak.cancel").enabled)
            #expect(try node("speak.play-pause").label == "Play speech")
            #expect(try node("speak.position").label == "Playback position")
            #expect(try node("speak.save-as").label == "Save the selected clip as")
            #expect(try node("speak.reference").label == "Reference clip")
            let modelPicker = try node("speak.model"), referencePicker = try node("speak.reference")
            #expect(modelPicker.texts.contains { $0.contains("Latest · \(RecordingSpeakModelTests.runID)") }, "\(modelPicker)")
            #expect(referencePicker.texts.contains { $0.contains("Reference Neutral 01") }, "\(referencePicker)")
            let status = try node("speak.status")
            #expect(status.label == "Speak status" && status.texts.contains("Spoken · 0:02.0 · Latest · \(RecordingSpeakModelTests.runID)"), "\(status)")
            let clip = try node("speak.clip." + operation)
            #expect(clip.label == RecordingSpeakModelTests.text + ", Latest · \(RecordingSpeakModelTests.runID), 0:02.0")
            #expect(try node("speak.clips").label == "Clips spoken this session")
            #expect(try node("speak.time").texts.contains("0:00.0 / 0:02.0"))
            // Nothing of the Record page is here: no regions to measure and no key monitor.
            #expect(!identifiers.contains("transport.record-stop") && !identifiers.contains("recording.status"))
            #expect(Self.views(RecordingRegionMarker.self, in: host).isEmpty)
            #expect(Self.views(RecordingWorkspaceKeyboardView.self, in: host).isEmpty)
        }
    }

    @Test @MainActor func thePlaceholderAndTheCountFollowTheText() async throws {
        let model = await workspace(Script())
        try await Self.hosting(RecordingSpeakView(model: model)) { host in
            await Self.settle(host)
            var nodes = Self.nodes(host)
            let placeholder = try #require(nodes.first { $0.identifier == "speak.placeholder" })
            #expect(placeholder.texts.contains(RecordingSpeakView.placeholder))
            // Static text carries its spoken words as its value.
            #expect(nodes.first { $0.identifier == "speak.count" }?.texts.contains("0 of 2,000 characters") == true)
            #expect(nodes.first { $0.identifier == "speak.reason" }?.texts.contains(RecordingSpeakModel.emptyTextReason) == true)
            #expect(nodes.first { $0.identifier == "speak.generate" }?.enabled == false)
            #expect(nodes.first { $0.identifier == "speak.status" }?.texts.contains(RecordingSpeakView.idleStatus) == true)
            model.speak.text = RecordingSpeakModelTests.text
            await Self.settle(host)
            nodes = Self.nodes(host)
            #expect(!nodes.contains { $0.identifier == "speak.placeholder" })
            #expect(nodes.first { $0.identifier == "speak.count" }?.texts.contains("30 of 2,000 characters") == true)
            #expect(nodes.first { $0.identifier == "speak.generate" }?.enabled == true)
        }
        #expect(RecordingSpeakView.countText(30) == "30 / 2,000" && RecordingSpeakView.countText(2_001) == "2,001 / 2,000")
        #expect(RecordingSpeakView.spokenCount(1) == "1 of 2,000 characters")
    }

    @Test @MainActor func theSpeakAndCancelButtonsFollowTheRequest() async throws {
        // No finished run: Speak is off, with the reason beside it.
        let empty = await workspace(Script(), runs: [])
        empty.speak.text = RecordingSpeakModelTests.text
        try await Self.hosting(RecordingSpeakView(model: empty)) { host in
            await Self.settle(host)
            let nodes = Self.nodes(host)
            #expect(nodes.first { $0.identifier == "speak.generate" }?.enabled == false)
            #expect(nodes.first { $0.identifier == "speak.reason" }?.texts.contains(RecordingSpeakModel.noFinishedRunReason) == true)
            #expect(nodes.first { $0.identifier == "speak.model" }?.enabled == false)
            #expect(nodes.first { $0.identifier == "speak.model" }?.texts.contains { $0.contains("No fine-tuned model yet") } == true)
            #expect(nodes.first { $0.identifier == "speak.reference" }?.texts.contains { $0.contains("Chosen by the service") } == true)
            #expect(nodes.first { $0.identifier == "speak.save-as" }?.enabled == false)
            #expect(nodes.first { $0.identifier == "speak.play-pause" }?.enabled == false)
        }

        // Generating: the button says so and Cancel is on; a failure is shown in its own words.
        let service = Script()
        service.holding = ["synthesize"]
        let model = await workspace(service)
        model.speak.text = RecordingSpeakModelTests.text
        model.speak.speak()
        try await Self.hosting(RecordingSpeakView(model: model)) { host in
            await Self.settle(host)
            var nodes = Self.nodes(host)
            let generate = try #require(nodes.first { $0.identifier == "speak.generate" })
            #expect(generate.label == "Generating speech" && !generate.enabled, "\(generate)")
            #expect(nodes.first { $0.identifier == "speak.cancel" }?.enabled == true)
            #expect(nodes.first { $0.identifier == "speak.model" }?.enabled == false)
            #expect(nodes.first { $0.identifier == "speak.status" }?.texts.contains("Generating · waiting to start") == true)
            let operation = try #require(model.speak.request?.operationID)
            model.handleService(try RecordingSpeakModelTests.stream(RecordingSpeakModelTests.synthesis(operation, "failed",
                error: RecordingFineTuneTests.failure("FINETUNE.SYNTHESIS_DEADLINE", RecordingSpeakModelTests.deadline, retryable: true))))
            await Self.settle(host)
            nodes = Self.nodes(host)
            #expect(nodes.first { $0.identifier == "speak.status" }?.texts.contains("Warning: " + RecordingSpeakModelTests.deadline) == true,
                    "\(nodes.first { $0.identifier == "speak.status" }.map(\.texts) ?? [])")
            #expect(nodes.first { $0.identifier == "speak.generate" }.map { $0.enabled && $0.label == "Speak" } == true)
        }
        service.release("synthesize")
        await waitUntil { service.answered["synthesize"] == 1 }
    }

    @Test @MainActor func generationChangesAreAnnouncedOnceEach() async throws {
        #expect(RecordingSpeakModel.readyAnnouncement(seconds: 2.46) == "Speech ready, 2.5 seconds")
        #expect(RecordingSpeakModel.readyAnnouncement(seconds: 2) == "Speech ready, 2 seconds")
        #expect(RecordingSpeakModel.readyAnnouncement(seconds: 1.02) == "Speech ready, 1 second")
        #expect(RecordingSpeakModel.readyAnnouncement(seconds: 0.04) == "Speech ready, under 0.1 seconds")
        #expect(RecordingSpeakModel.readyAnnouncement(seconds: 0.05) == "Speech ready, 0.1 seconds")
        let service = Script()
        let model = await workspace(service)
        #expect(model.speak.announcement == nil)
        let operation = try await finishedClip(model, service)
        #expect(model.speak.announcement?.text == "Speech ready, 2 seconds")
        let finished = model.speak.announcement
        model.handleService(try RecordingSpeakModelTests.stream(RecordingSpeakModelTests.synthesis(operation, "finished")))
        #expect(model.speak.announcement == finished, "a duplicate is not announced again")
        model.speak.text = "Another sentence."
        model.speak.speak()
        #expect(model.speak.announcement?.text == RecordingSpeakModel.generatingAnnouncement)
        #expect(model.speak.announcement.map(\.sequence) == finished.map { $0.sequence + 1 })
        await waitUntil { service.answered["synthesize"] == 2 }
        let second = try #require(model.speak.request?.operationID)
        model.handleService(try RecordingSpeakModelTests.stream(RecordingSpeakModelTests.synthesis(second, "failed",
            error: RecordingFineTuneTests.failure("FINETUNE.SYNTHESIS_DEADLINE", RecordingSpeakModelTests.deadline, retryable: true))))
        #expect(model.speak.announcement?.text == RecordingSpeakModelTests.deadline)
        // A cancel is not announced; quitting while generating simply ends the request.
        model.speak.speak()
        let third = model.speak.announcement
        model.close()
        #expect(model.speak.request?.phase == .cancelled && model.speak.announcement == third)
    }

    // MARK: Save As

    @Test @MainActor func saveAsCopiesTheExactClipAndNeverKeepsAChangedOne() async throws {
        let library = try PrivateFolder("cole-speak-library")
        defer { library.remove() }
        let exports = try PrivateFolder("cole-speak-exports")
        defer { exports.remove() }
        let service = Script()
        let model = await workspace(service)
        #expect(model.speak.saveUnavailableReason == RecordingSpeakModel.noClipReason)
        model.fixtureAssetRoot = library.url
        let text = "The meeting moved to Thursday, at 10:30!"
        model.speak.text = text
        model.speak.speak()
        await waitUntil { service.answered["synthesize"] == 1 }
        let operation = try #require(model.speak.request?.operationID)
        let clip = try library.writeClip(operation: operation, frames: 2_400)
        model.handleService(try RecordingSpeakModelTests.stream(RecordingSpeakModelTests.synthesis(
            operation, "finished", text: text, frames: 2_400, sha256: clip.sha256)))
        let selected = try #require(model.speak.selectedClip)
        #expect(RecordingSpeakModel.suggestedFileName(for: selected) == "the-meeting-moved-to-thursday-at-10-30.wav")
        #expect(model.speak.canSave)

        // A new file, then a file that already exists: both hold exactly the clip's bytes.
        for existing in [false, true] {
            let destination = exports.url.appendingPathComponent("speech.wav")
            if existing { try Data("older".utf8).write(to: destination) }
            model.speak.saveClip(operation, to: destination)
            #expect(model.speak.savingClipID == operation && model.speak.saveUnavailableReason == RecordingSpeakModel.clipSavingReason)
            await model.speak.saveTask?.value
            #expect(model.speak.savingClipID == nil && !model.speak.saveFailed)
            #expect(model.speak.saveNotice == "Saved speech.wav")
            #expect(try Data(contentsOf: destination) == clip.bytes)
            #expect(try exports.names() == ["speech.wav"], "no temporary file is left")
        }
        var metadata = stat()
        #expect(lstat(library.url.appendingPathComponent(clip.relativePath).path, &metadata) == 0 && metadata.st_nlink == 1, "never a hard link")
        #expect(lstat(exports.url.appendingPathComponent("speech.wav").path, &metadata) == 0 && metadata.st_nlink == 1)

        // Inside the recording library: refused, nothing written there.
        model.speak.saveClip(operation, to: library.url.appendingPathComponent("derived/speech/copy.wav"))
        await model.speak.saveTask?.value
        #expect(model.speak.saveFailed && model.speak.saveNotice == RecordingSpeakExport.insideLibraryMessage)
        #expect(!FileManager.default.fileExists(atPath: library.url.appendingPathComponent("derived/speech/copy.wav").path))

        // A folder that does not exist.
        model.speak.saveClip(operation, to: exports.url.appendingPathComponent("missing/speech.wav"))
        await model.speak.saveTask?.value
        #expect(model.speak.saveFailed && model.speak.saveNotice?.hasPrefix("The clip could not be saved as speech.wav: ") == true, "\(model.speak.saveNotice ?? "")")

        // The clip changed after Speak wrote it: refused, and the earlier copy is untouched.
        var altered = clip.bytes
        altered[altered.count - 1] ^= 0x01
        let clipURL = library.url.appendingPathComponent(clip.relativePath)
        try altered.write(to: clipURL)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: clipURL.path)
        model.speak.saveClip(operation, to: exports.url.appendingPathComponent("speech.wav"))
        await model.speak.saveTask?.value
        #expect(model.speak.saveFailed && model.speak.saveNotice == RecordingSpeakExport.changedMessage)
        #expect(try Data(contentsOf: exports.url.appendingPathComponent("speech.wav")) == clip.bytes)
        model.speak.saveClip(operation, to: exports.url.appendingPathComponent("changed.wav"))
        await model.speak.saveTask?.value
        #expect(model.speak.saveNotice == RecordingSpeakExport.changedMessage)
        #expect(try exports.names() == ["speech.wav"])

        // A clip replaced by a link, or readable by others, is not the clip Speak wrote.
        try FileManager.default.removeItem(at: clipURL)
        try FileManager.default.createSymbolicLink(at: clipURL, withDestinationURL: exports.url.appendingPathComponent("speech.wav"))
        #expect(throws: RecordingContractError.self) {
            try RecordingSpeakExport.copy(assetRoot: library.url, relativePath: clip.relativePath, sha256: clip.sha256,
                                          maximumBytes: RecordingPlaybackFormat.speech.maximumFileBytes(frameCount: 2_400),
                                          to: exports.url.appendingPathComponent("linked.wav"))
        }
        try FileManager.default.removeItem(at: clipURL)
        try clip.bytes.write(to: clipURL)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: clipURL.path)
        do {
            try RecordingSpeakExport.copy(assetRoot: library.url, relativePath: clip.relativePath, sha256: clip.sha256,
                                          maximumBytes: RecordingPlaybackFormat.speech.maximumFileBytes(frameCount: 2_400),
                                          to: exports.url.appendingPathComponent("shared.wav"))
            Issue.record("A clip readable by others was copied")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.SOURCE_CHANGED" && error.message == RecordingSpeakExport.changedMessage)
        }
        #expect(try exports.names() == ["speech.wav"])
        // Too small a bound refuses too; so does a destination that is not a file path.
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: clipURL.path)
        #expect(throws: RecordingContractError.self) {
            try RecordingSpeakExport.copy(assetRoot: library.url, relativePath: clip.relativePath, sha256: clip.sha256,
                                          maximumBytes: 100, to: exports.url.appendingPathComponent("bounded.wav"))
        }
        #expect(throws: RecordingContractError.self) {
            try RecordingSpeakExport.copy(assetRoot: library.url, relativePath: clip.relativePath, sha256: clip.sha256,
                                          maximumBytes: 1_000_000, to: URL(string: "https://example.invalid/speech.wav")!)
        }
        #expect(try exports.names() == ["speech.wav"])
    }

    @Test func suggestedNamesAreShortAndPlain() throws {
        func name(_ text: String) throws -> String {
            let event = RecordingSpeakModelTests.synthesis("speak-one", "finished", text: text)
            return RecordingSpeakModel.suggestedFileName(for: try #require(RecordingSpeakClip(finished: event.object ?? [:])))
        }
        #expect(try name("Hello") == "hello.wav")
        #expect(try name("…!?") == "speech.wav")
        #expect(try name("Ça va? Très bien.") == "ça-va-très-bien.wav")
        let long = try name(String(repeating: "word ", count: 40))
        #expect(long.count <= 52 && long.hasSuffix(".wav") && !long.hasPrefix("-"), "\(long)")
    }

    // MARK: the page switch

    @Test @MainActor func switchingPagesPausesThePageLeftAndWaitsForRecordingAndSaving() async throws {
        let reviewBackend = QuietBackend(), speechBackend = QuietBackend()
        let service = Script()
        let model = await workspace(service, reviewBackend: reviewBackend, speechBackend: speechBackend)
        model.fixtureAssetRoot = URL(fileURLWithPath: "/private/tmp/synthetic-speech-root")
        #expect(model.page == .record && model.canSwitchPage && model.pageSwitchUnavailableReason == nil)
        let take = try RecordingPlaybackSource(takeID: "take-one", sourceSHA256: String(repeating: "a", count: 64),
                                               assetRoot: URL(fileURLWithPath: "/private/tmp/synthetic-review-root"),
                                               relativePath: "take-one.wav", frameCount: 4_800)
        try await model.review.playback.select(take, retainedRange: 0..<4_800)
        try await model.review.playback.playPause()
        #expect(model.review.playback.phase == .playing)

        model.showPage(.speak)
        #expect(model.page == .speak)
        await model.pageSwitchTask?.value
        #expect(model.review.playback.phase == .paused && model.review.playback.cursorFrame == 120)
        #expect(await reviewBackend.pauses == 1)
        model.showPage(.speak)
        #expect(await reviewBackend.pauses == 1, "showing the shown page changes nothing")

        _ = try await finishedClip(model, service)
        await waitUntil { model.speak.canPlay }
        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .playing }
        model.showPage(.record)
        await model.pageSwitchTask?.value
        #expect(model.page == .record && model.speak.playback.phase == .paused)
        #expect(await speechBackend.pauses == 1)

        // Speak starting pauses the review output first (and the review transport pauses Speak's).
        try await model.review.playback.playPause()
        #expect(model.review.playback.phase == .playing && model.speak.playback.phase == .paused)
        model.speak.playPause()
        await waitUntil { model.speak.playback.phase == .playing && model.review.playback.phase == .paused }
        #expect(await reviewBackend.pauses == 2)

        // Recording, a take being registered or saved, or an open question hold the page.
        for (phase, reason) in [(RecordingShellPhase.starting, RecordingWorkspaceModel.pageSwitchRecordingReason),
                                (.recording, RecordingWorkspaceModel.pageSwitchRecordingReason),
                                (.stopping, RecordingWorkspaceModel.pageSwitchRecordingReason),
                                (.saving, RecordingWorkspaceModel.pageSwitchSavingReason)] {
            model.state.phase = phase
            #expect(!model.canSwitchPage && model.pageSwitchUnavailableReason == reason, "\(phase)")
            model.showPage(.speak)
            #expect(model.page == .record, "\(phase)")
        }
        model.state.phase = .ready
        model.reviewConfirmation = .quitDuringFineTune
        #expect(model.pageSwitchUnavailableReason == RecordingWorkspaceModel.pageSwitchQuestionReason)
        model.showPage(.speak)
        #expect(model.page == .record)
        model.reviewConfirmation = nil
        for phase in [RecordingShellPhase.ready, .draftReview, .recoverableError] {
            model.state.phase = phase
            #expect(model.canSwitchPage, "\(phase)")
        }
        #expect(RecordingPage.allCases.map(\.title) == ["Record", "Speak"])
        #expect(RecordingPage.allCases.map(\.identifier) == ["page.record", "page.speak"])
        #expect(RecordingPage.allCases.map(\.menuTitle) == ["Record Page", "Speak Page"])
        #expect(RecordingPage.allCases.map(\.shortcut.character) == ["1", "2"])
    }

    @Test @MainActor func theRecordKeyMonitorAndRegionsExistOnlyOnTheRecordPage() async throws {
        let model = await workspace(Script())
        try await Self.hosting(RecordingPageContainer(model: model)) { host in
            await Self.settle(host)
            let monitors = Self.views(RecordingWorkspaceKeyboardView.self, in: host)
            #expect(monitors.count == 1 && monitors.allSatisfy(\.isMonitoring))
            let regions = Self.regions(host)
            let expected = RecordingLayout.regions(size: Self.size, focusMode: false)
            #expect(Set(regions.keys) == Set(expected.keys), "\(regions)")
            for (name, frame) in expected {
                let actual = regions[name] ?? .null
                #expect(abs(actual.minX - frame.minX) <= 1 && abs(actual.minY - frame.minY) <= 1
                        && abs(actual.width - frame.width) <= 1 && abs(actual.height - frame.height) <= 1, "\(name): \(actual) vs \(frame)")
            }

            model.showPage(.speak)
            await Self.settle(host)
            #expect(Self.views(RecordingWorkspaceKeyboardView.self, in: host).isEmpty)
            #expect(monitors.allSatisfy { !$0.isMonitoring }, "the Record monitor is removed with its page")
            #expect(Self.regions(host).isEmpty)
            #expect(Set(Self.nodes(host).map(\.identifier)).isSuperset(of: Self.speakIdentifiers))

            model.showPage(.record)
            await Self.settle(host)
            let restored = Self.views(RecordingWorkspaceKeyboardView.self, in: host)
            #expect(restored.count == 1 && restored.allSatisfy(\.isMonitoring))
            #expect(Self.regions(host) == regions, "the Record page comes back with the same geometry")
            #expect(Set(Self.nodes(host).map(\.identifier)).isDisjoint(with: Self.speakIdentifiers))
        }
    }

    @MainActor private static func regions(_ host: NSView) -> [String: CGRect] {
        Dictionary(uniqueKeysWithValues: views(RecordingRegionMarker.self, in: host).map {
            ($0.regionName, $0.convert($0.bounds, to: host).integral)
        })
    }

    @Test @MainActor func thePagePickerNamesBothPagesAndFollowsTheirAvailability() async throws {
        let model = await workspace(Script())
        try await Self.hosting(RecordingPagePicker(model: model), size: CGSize(width: 240, height: 40)) { host in
            await Self.settle(host)
            var nodes = Self.nodes(host)
            let picker = try #require(nodes.first { $0.identifier == "page.picker" })
            #expect(picker.enabled && picker.label == "Page", "\(nodes)")
            // Each segment is its own element, named and identified.
            #expect(nodes.first { $0.identifier == "page.record" }?.label == "Record page")
            #expect(nodes.first { $0.identifier == "page.speak" }?.label == "Speak page")
            model.state.phase = .recording
            model.objectWillChange.send()
            await Self.settle(host)
            nodes = Self.nodes(host)
            #expect(nodes.first { $0.identifier == "page.picker" }?.enabled == false)
        }
        #expect(RecordingPagePicker.help == "Switch between recording takes and speaking text (⌘1, ⌘2)")
    }
}

/// A private temporary folder (0700) under the resolved temporary directory.
private struct PrivateFolder {
    let url: URL

    init(_ prefix: String) throws {
        let temporary = try #require(realpath(FileManager.default.temporaryDirectory.path, nil))
        defer { free(temporary) }
        url = URL(fileURLWithPath: String(cString: temporary), isDirectory: true)
            .appendingPathComponent(prefix + "-" + UUID().uuidString.lowercased(), isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    }

    func names() throws -> [String] { try FileManager.default.contentsOfDirectory(atPath: url.path).sorted() }

    /// A clip as the service writes it: 24 kHz, 16-bit, mono, 0600.
    func writeClip(operation: String, frames: Int) throws -> (relativePath: String, sha256: String, bytes: Data) {
        let relativePath = "derived/speech/\(operation)/speech-01.wav"
        let file = url.appendingPathComponent(relativePath)
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        var wav = Data("RIFF".utf8)
        func u32(_ value: UInt32) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        func u16(_ value: UInt16) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        u32(UInt32(36 + frames * 2)); wav.append(Data("WAVEfmt ".utf8)); u32(16)
        u16(1); u16(1); u32(24_000); u32(48_000); u16(2); u16(16)
        wav.append(Data("data".utf8)); u32(UInt32(frames * 2))
        for index in 0..<frames { u16(UInt16(truncatingIfNeeded: index * 37)) }
        try wav.write(to: file, options: .withoutOverwriting)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        return (relativePath, SHA256.hash(data: wav).map { String(format: "%02x", $0) }.joined(), wav)
    }

    func remove() { try? FileManager.default.removeItem(at: url) }
}
