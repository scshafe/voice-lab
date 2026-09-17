import AppKit
import ApplicationServices
import Combine
import RecordingCore
import Darwin
import Foundation
import RecordingContracts
import RecordingUI
import SwiftUI

struct ShellGeometryRecord: Codable {
    let width: Int
    let height: Int
    let scale: Int
    let appearance: String
    let phase: String
    let scriptSize: Int
    let focusMode: Bool
    let roomTone: Bool
    let regions: [String: [Double]]
}

@main
struct RecordingHarness {
    @MainActor
    static func main() async throws {
        umask(0o077)
        guard (3...6).contains(CommandLine.arguments.count) else {
            throw RecordingContractError(code: "RECORDING.HARNESS_ARGUMENTS", message: "Usage: RecordingHarness shell OUTPUT_DIRECTORY")
        }
        let suite = CommandLine.arguments[1]
        let directory = URL(fileURLWithPath: CommandLine.arguments.last!, isDirectory: true)
        guard ["shell", "workspace", "contracts", "transport", "review", "review-legacy", "recovery-zero-frame", "recovery-crash", "recovery-restart", "recovery-busy", "benchmark", "open", "early-capture"].contains(suite) else {
            throw RecordingContractError(code: "RECORDING.HARNESS_SUITE", message: "Unknown or unimplemented harness suite")
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        if suite == "early-capture" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Early capture requires configuration and output directory") }
            let status = try await earlyCaptureProbe(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
            Darwin.exit(status)
        }
        else if suite == "open" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Open requires configuration and output directory") }
            let status = try await openProbe(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
            Darwin.exit(status)
        }
        else if suite == "benchmark" {
            guard [5, 6].contains(CommandLine.arguments.count), let cycles = Int(CommandLine.arguments[3]),
                  (1...1000).contains(cycles) else {
                throw transportFailure("Benchmark requires configuration, cycle count, optional completion policy and output directory")
            }
            guard let completionPolicy = RecordingBenchmarkCompletionPolicy(rawValue:
                CommandLine.arguments.count == 6 ? CommandLine.arguments[4] : "exact-frames") else {
                throw transportFailure("Benchmark completion policy is not recognized")
            }
            do {
                try await benchmark(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory,
                    cycles: cycles, completionPolicy: completionPolicy)
            } catch {
                // Expected workflow failures already retain their partial report. Do not
                // turn a rejected command into Swift's top-level uncaught-error trap.
                let failure: RecordingJSON = .object([
                    "event": .string("benchmark_failed"), "error": .string(String(describing: error)),
                ])
                if let data = try? failure.transportData() { print(String(decoding: data, as: UTF8.self)); fflush(stdout) }
                Darwin.exit(1)
            }
        }
        else if suite == "recovery-busy" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Recovery requires configuration and output directory") }
            try await recoveryBusy(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
        }
        else if suite == "recovery-restart" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Recovery requires configuration and output directory") }
            try await recoveryRestart(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
        }
        else if suite == "recovery-zero-frame" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Recovery requires configuration and output directory") }
            try await recoveryZeroFrame(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
        }
        else if suite == "recovery-crash" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Recovery requires configuration and output directory") }
            try await recoveryCrash(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
        }
        else if suite == "review-legacy" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Legacy review requires configuration and output directory") }
            try await legacyReview(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
        }
        else if suite == "review" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Review requires configuration and output directory") }
            try await review(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
        }
        else if suite == "transport" {
            guard CommandLine.arguments.count == 4 else { throw transportFailure("Transport requires configuration and output directory") }
            try await transport(configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]), directory: directory)
        }
        else if suite == "shell" || suite == "workspace" { try shell(directory: directory) }
        else { try contracts(directory: directory) }
    }

    @MainActor
    private static func transport(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let configuration = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard configuration.recorder.arguments.contains("--synthetic-pcm") else {
            throw RecordingContractError(code: "RECORDING.HARNESS_ARGUMENTS", message: "Transport harness requires explicit synthetic helper PCM")
        }
        var startupGate: CheckedContinuation<Void, Never>?
        var firstStart = true
        let startupEntered = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
        let model = RecordingWorkspaceModel(configuration: configuration, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline), beforeCapture: {
            if firstStart {
                firstStart = false
                await withCheckedContinuation { startupGate = $0; startupEntered.continuation.yield(()) }
            }
        })
        defer { model.close() }
        let observer = RecordingTransportObserver(model: model)
        let fixtureRoot = configurationURL.deletingLastPathComponent()
        let delayed = configuration.recorder.arguments.contains("--synthetic-seal-barrier-directory")
        let fileEvents = try RecordingFixtureFileObserver(directory: fixtureRoot)
        defer { fileEvents.close() }
        // The configuration names no catalog; the fixture store's active version is the only source.
        let expectedReference = try RecordingJSON.parse(Data(contentsOf: fixtureRoot.appendingPathComponent("catalog-reference.json")))
        guard model.prompts.isEmpty, model.catalog == nil, model.catalogSHA256 == nil else { throw transportFailure("The Library must wait for the service's catalog") }
        await model.connect()
        try await observer.wait { model.state.canRecord && model.state.historyCompleteness == "complete" }
        guard let reference = model.catalogReference, reference == expectedReference,
              let digest = reference.object?["catalog_sha256"]?.string, model.catalogSHA256 == digest,
              model.catalog?.object?["suite_id"] == reference.object?["suite_id"],
              model.state.selection.object?["catalog_sha256"] == .string(digest) else {
            throw transportFailure("The workspace did not record against the catalog the service pinned from its store")
        }
        guard model.prompts.count == 208, model.state.promptCursor != nil else { throw transportFailure("Transport fixture must exercise bounded prompt metadata pagination") }
        await model.loadMorePromptStatuses()
        guard model.state.promptCursor == nil, model.state.promptStatuses.count == 208 else { throw transportFailure("Paged prompt coverage did not preserve a coherent index revision") }
        let target = model.prompts[1]
        model.selectPrompt(target.id)
        try await observer.wait { model.state.prompt?.id == target.id && !model.state.navigationPending }
        model.searchText = model.prompts[0].title
        guard model.currentPromptFilteredOut else { throw transportFailure("Filtered current target must remain explicitly pinned") }
        // SwiftUI publishes accessibility nodes only in a launched application; the window is still never shown.
        NSApplication.shared.finishLaunching()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 700), styleMask: [.titled, .resizable], backing: .buffered, defer: true)
        let hosted = NSHostingView(rootView: RecordingWorkspace(model: model))
        window.contentView = hosted
        hosted.frame = NSRect(x: 0, y: 0, width: 1060, height: 700)
        hosted.layoutSubtreeIfNeeded()
        let baseline = measuredRegions(view: hosted)
        try validateRegions(baseline, size: hosted.frame.size, focusMode: false, baseline: nil)
        func textField(_ view: NSView) -> NSTextField? {
            if let field = view as? NSTextField, field.isEditable { return field }
            for child in view.subviews { if let field = textField(child) { return field } }
            return nil
        }
        // §6.4: the first editable field is the search field (identifier `library.search`), and the
        // prominent Continue Missing button sits above it as a plain button, never an editable field.
        guard let search = textField(hosted) else { throw transportFailure("Hosted native search field is missing") }
        // SwiftUI publishes its own node one point inside the field's bezel, so the field must lie within it.
        let searchElement = accessibilityElement("library.search", in: hosted)
        guard let searchElement,
              searchElement.element === search || searchElement.frame.insetBy(dx: -2, dy: -2).contains(search.accessibilityFrame()) else {
            throw transportFailure("The first editable field is not the library search field: node \(searchElement.map { "\($0.frame)" } ?? "missing"), field \(search.accessibilityFrame())")
        }
        let continueMissing = accessibilityElement("library.continueMissing", in: hosted)
        // A null or empty frame must not satisfy the placement comparison vacuously.
        guard let continueMissing, !continueMissing.frame.isNull, continueMissing.frame.height > 0,
              continueMissing.frame.minY >= searchElement.frame.maxY - 1 else {
            throw transportFailure("Continue Missing is missing or not above the search field: button \(continueMissing.map { "\($0.frame)" } ?? "missing"), search \(searchElement.frame)")
        }
        guard window.makeFirstResponder(search) else { throw transportFailure("Hosted native search field did not accept focus") }
        let focused = window.firstResponder
        func readingScroll(_ view: NSView) -> NSScrollView? {
            if let scroll = view as? NSScrollView, scroll.bounds.width > 500,
               (scroll.documentView?.bounds.height ?? 0) > scroll.bounds.height + 120 { return scroll }
            for child in view.subviews { if let scroll = readingScroll(child) { return scroll } }
            return nil
        }
        guard let reading = readingScroll(hosted) else { throw transportFailure("Long script did not expose its native reading scroll region") }
        reading.contentView.scroll(to: NSPoint(x: 0, y: 120)); reading.reflectScrolledClipView(reading.contentView)
        let readingPosition = reading.contentView.bounds.origin
        model.record()
        var startupEvents = startupEntered.stream.makeAsyncIterator()
        _ = await startupEvents.next()
        guard model.state.phase == .starting && model.state.canStop else { throw transportFailure("Stop must remain available during startup") }
        model.stop()
        guard model.state.phase == .stopping && model.state.showsStop else { throw transportFailure("Stop lost its stable red stopping representation") }
        startupGate?.resume(); startupGate = nil
        try await observer.wait { model.state.phase == .ready }
        guard model.state.prompt?.id == target.id, model.state.takeID == nil else { throw transportFailure("Cancelled pre-input startup changed prompt or created a take") }
        let beforeRealCapture = model.transitions.count
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4_801 }
        guard model.state.prompt?.scriptBody == target.scriptBody else { throw transportFailure("Recording replaced the exact script") }
        model.stop()
        if delayed {
            try await fileEvents.wait(for: fixtureRoot.appendingPathComponent("seal-entered"))
            guard model.state.phase == .stopping, model.state.showsStop, !model.canSave,
                  model.state.prompt?.scriptBody == target.scriptBody, model.waveform.snapshot?.writtenFrames == 4_801 else { throw transportFailure("Blocked real seal changed content or enabled Save") }
            try RecordingFixtureFileObserver.release(fixtureRoot.appendingPathComponent("seal-release.fifo"), byte: 1)
        }
        try await observer.wait { model.canSave && model.state.promptStatuses[target.id] == "draft" }
        let activeTransitions = model.transitions.dropFirst(beforeRealCapture).prefix { $0.phase != "draftReview" }
        guard !activeTransitions.isEmpty, activeTransitions.allSatisfy({ ["starting", "recording", "stopping"].contains($0.phase) }) else { throw transportFailure("Active capture briefly lost its persistent red Stop representation") }
        guard model.state.prompt?.scriptBody == target.scriptBody, model.waveform.snapshot?.writtenFrames == 4_801 else { throw transportFailure("Stopped draft lost script or final waveform") }
        let draftTake = model.state.take
        hosted.layoutSubtreeIfNeeded()
        try validateRegions(measuredRegions(view: hosted), size: hosted.frame.size, focusMode: false, baseline: baseline)
        try render(view: hosted, size: hosted.frame.size, scale: 2, destination: directory.appendingPathComponent("transport-draft.png"))
        guard abs(reading.contentView.bounds.origin.y - readingPosition.y) <= 1 else { throw transportFailure("Start/Stop reset the long script's reading scroll position") }
        let beforeSave = model.transitions.count
        model.save(); model.save()
        if delayed {
            try await fileEvents.wait(for: fixtureRoot.appendingPathComponent("save-entered"))
            guard model.state.phase == .saving, model.state.prompt?.scriptBody == target.scriptBody,
                  model.waveform.snapshot?.writtenFrames == 4_801,
                  abs(reading.contentView.bounds.origin.y - readingPosition.y) <= 1 else { throw transportFailure("Blocked durable Save changed script, waveform or reading position") }
            try RecordingFixtureFileObserver.release(fixtureRoot.appendingPathComponent("save-release.fifo"), byte: 49)
        }
        guard model.state.phase == .saving, model.state.prompt?.id == target.id, model.waveform.snapshot?.writtenFrames == 4_801 else { throw transportFailure("Save changed content before durable acknowledgment") }
        try await observer.wait { model.state.phase == .ready && model.state.prompt?.id == model.prompts[2].id }
        let saveTransitions = Array(model.transitions.dropFirst(beforeSave))
        guard saveTransitions.filter({ $0.phase == "saving" }).allSatisfy({ $0.promptID == target.id && $0.promptText == target.scriptBody }),
              !saveTransitions.contains(where: { $0.promptID == model.prompts[0].id }),
              model.waveform.snapshot == nil else { throw transportFailure("Save published a provisional prompt or retained an old waveform on the new prompt") }
        if delayed {
            guard let capturedTakeID = draftTake.object?["reservation"]?.object?["take_id"]?.string else { throw transportFailure("Draft take identity was lost") }
            try await observer.wait { model.state.history.contains { $0.object?["take_id"] == .string(capturedTakeID) } }
            let pending = model.state.history.first { $0.object?["take_id"] == .string(capturedTakeID) }?.object?["analysis"]?.string
            guard pending == "pending" || pending == "running" else { throw transportFailure("Analysis bypassed its actual worker barrier") }
            try RecordingFixtureFileObserver.release(fixtureRoot.appendingPathComponent("analysis-release.fifo"), byte: 49)
            try await observer.wait {
                let status = model.state.history.first { $0.object?["take_id"] == .string(capturedTakeID) }?.object?["analysis"]?.string
                return status != nil && status != "pending" && status != "running"
            }
            guard model.state.prompt?.id == model.prompts[2].id, model.waveform.snapshot == nil else { throw transportFailure("Late actual analysis replaced the active prompt or waveform") }
        }
        let operations = Set(saveTransitions.filter { $0.phase == "saving" }.compactMap(\.operationID))
        guard operations.count == 1 else { throw transportFailure("Duplicate Save click created more than one operation") }
        hosted.layoutSubtreeIfNeeded()
        try validateRegions(measuredRegions(view: hosted), size: hosted.frame.size, focusMode: false, baseline: baseline)
        guard window.firstResponder === focused else { throw transportFailure("Transport processing stole native text field focus") }
        try render(view: hosted, size: hosted.frame.size, scale: 2, destination: directory.appendingPathComponent("transport-next.png"))
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(model.transitions).write(to: directory.appendingPathComponent("transport-transitions.json"), options: .atomic)
        try draftTake.transportData().write(to: directory.appendingPathComponent("draft-take.json"), options: .atomic)
        let report: [String: Any] = ["real_native_model": true, "real_service_process": true, "real_helper_process": true,
            "exact_frames": 4_801, "startup_cancelled_before_input": true, "duplicate_save_operation_count": operations.count,
            "prompt_timeline_coherent": true, "fixed_geometry": true, "native_focus_preserved": true, "reading_scroll_preserved": true, "paged_prompt_count": model.prompts.count,
            "first_editable_field": "library.search", "continue_missing_above_search": true,
            "delayed_seal_save_and_qc": delayed, "stop_representation_preserved": true, "draft_library_coherent": true,
            "catalog_source": "service", "catalog_sha256": model.catalogSHA256 ?? "",
            "catalog_id": model.catalogReference?.object?["catalog_id"]?.string ?? "",
            "catalog_version": model.catalogReference?.object?["catalog_version"]?.integer ?? 0,
            "microphone_opened": false, "window_shown": false, "next_prompt_id": model.state.prompt!.id]
        let reportData = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys, .prettyPrinted])
        try reportData.write(to: directory.appendingPathComponent("transport-result.json"), options: .atomic)
        print(String(decoding: reportData, as: UTF8.self))
    }

    private static func transportFailure(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.TRANSPORT_HARNESS", message: message)
    }

    private static func contracts(directory: URL) throws {
        struct Fixture: Codable {
            let file: String
            let model: String
            let valid: Bool
            let validation_owner: String
        }
        let decoder = JSONDecoder()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let fixtures = try decoder.decode([Fixture].self, from: Data(contentsOf: directory.appendingPathComponent("index.json")))
        let output = directory.appendingPathComponent("swift", isDirectory: true)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        var sharedCount = 0
        var pythonEvidenceCount = 0
        var nestedUnknownCount = 0
        var parserRejectedCount = 0
        var outgoing: [Fixture] = []
        for fixture in fixtures {
            let payload = try Data(contentsOf: directory.appendingPathComponent(fixture.file))
            let schema = try Data(contentsOf: directory.appendingPathComponent(fixture.model + ".schema.json"))
            let validator = try RecordingSchemaValidator(schema: schema)
            if fixture.validation_owner == "shared" {
                var accepted = false
                do { _ = try validator.validate(payload); accepted = true }
                catch { if fixture.valid { throw RecordingContractError(code: "RECORDING.CONFORMANCE_POSITIVE", message: "Native validation rejected a positive fixture: \(fixture.file): \(error)") } }
                guard accepted == fixture.valid else {
                    throw RecordingContractError(code: "RECORDING.CONFORMANCE_NEGATIVE", message: "Native validation accepted invalid fixture: \(fixture.file)")
                }
                if accepted && fixture.model.hasSuffix("request") || (accepted && ["recording-response", "recording-telemetry"].contains(fixture.model)) {
                    _ = try RecordingWireEnvelope.decode(payload, schema: schema)
                }
                sharedCount += 1
            } else if fixture.validation_owner == "python-evidence" {
                pythonEvidenceCount += 1
            } else {
                throw RecordingContractError(code: "RECORDING.CONFORMANCE_OWNER", message: "Unknown conformance validation owner")
            }
            let decoded: RecordingJSON
            do { decoded = try RecordingJSON.parse(payload) }
            catch {
                guard !fixture.valid else { throw error }
                parserRejectedCount += 1
                continue
            }
            try decoded.transportData().write(to: output.appendingPathComponent(fixture.file), options: .atomic)
            outgoing.append(fixture)
            if fixture.valid, var object = decoded.object,
               let key = object.keys.sorted().first(where: { object[$0]?.object != nil }),
               var nested = object[key]?.object {
                nested["native_unknown_field"] = .boolean(true)
                object[key] = .object(nested)
                let invalid = try RecordingJSON.object(object).transportData()
                var rejected = false
                do { _ = try validator.validate(invalid) } catch { rejected = true }
                guard rejected else { throw RecordingContractError(code: "RECORDING.CONFORMANCE_UNKNOWN", message: "Nested unknown field escaped validation") }
                let filename = "native-nested-unknown-" + fixture.file
                try invalid.write(to: output.appendingPathComponent(filename), options: .atomic)
                outgoing.append(Fixture(file: filename, model: fixture.model, valid: false, validation_owner: "shared"))
                nestedUnknownCount += 1
            }
        }
        try encoder.encode(outgoing).write(to: output.appendingPathComponent("index.json"), options: .atomic)
        print("{\"suite\":\"contracts\",\"shared_fixtures_checked\":\(sharedCount),\"native_nested_unknown_cases\":\(nestedUnknownCount),\"python_evidence_cases_deferred\":\(pythonEvidenceCount),\"parser_rejected_not_encodable\":\(parserRejectedCount),\"swift_encoded_fixtures\":\(outgoing.count)}")
    }

    /// Hosts real views without ordering a window or acquiring any audio device.
    @MainActor
    private static func shell(directory: URL) throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        registerLightAppearancePreference()
        var records: [ShellGeometryRecord] = []
        var imageCount = 0
        for size in [CGSize(width: 1060, height: 700), CGSize(width: 1440, height: 980)] {
            for scale in [1, 2] {
                for (appearanceName, appearance) in [("light", NSAppearance.Name.aqua), ("dark", NSAppearance.Name.darkAqua)] {
                    for scriptSize in [24, 32] {
                        for focusMode in [false, true] {
                            for roomTone in [false, true] {
                                var baseline: [String: CGRect]?
                                for phase in RecordingShellPhase.allCases {
                                    let state = RecordingShellState(phase: phase, scriptSize: CGFloat(scriptSize), focusMode: focusMode, roomTone: roomTone)
                                    let view = NSHostingView(rootView: RecordingWorkspace(state: state)
                                        .environment(\.colorScheme, appearanceName == "dark" ? .dark : .light)
                                        .environment(\.displayScale, CGFloat(scale)))
                                    view.appearance = NSAppearance(named: appearance)
                                    view.frame = NSRect(origin: .zero, size: size)
                                    view.layoutSubtreeIfNeeded()
                                    let regions = measuredRegions(view: view)
                                    try validateRegions(regions, size: size, focusMode: focusMode, baseline: baseline)
                                    baseline = regions
                                    records.append(ShellGeometryRecord(
                                        width: Int(size.width), height: Int(size.height), scale: scale,
                                        appearance: appearanceName, phase: phase.rawValue, scriptSize: scriptSize,
                                        focusMode: focusMode, roomTone: roomTone,
                                        regions: regions.mapValues { [Double($0.minX), Double($0.minY), Double($0.width), Double($0.height)] }
                                    ))
                                    // Acceptance needs a rendered file for every documented
                                    // state, not only the two that read well in a summary.
                                    // One canonical configuration carries the full inventory;
                                    // the ready and recording matrix stays as it was.
                                    let canonical = size.width == 1060 && scale == 2
                                        && scriptSize == 24 && !focusMode && !roomTone
                                    if canonical || (scriptSize == 24 && !focusMode && (phase == .ready || phase == .recording)) {
                                        let filename = "shell-\(Int(size.width))x\(Int(size.height))-\(scale)x-\(appearanceName)-\(phase.rawValue)-\(roomTone ? "silence" : "reading").png"
                                        let bitmap = try render(view: view, size: size, scale: scale, destination: directory.appendingPathComponent(filename))
                                        try requireRenderedAppearance(bitmap, dark: appearanceName == "dark", context: filename)
                                        imageCount += 1
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        try requireApplicationAppearanceUntouched()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(records).write(to: directory.appendingPathComponent("geometry.json"), options: .atomic)
        print("{\"suite\":\"shell\",\"geometry_cases\":\(records.count),\"snapshots\":\(imageCount),\"region_delta_tolerance_points\":1,\"microphone_opened\":false,\"window_shown\":false}")
    }

    @MainActor
    static func measuredRegions(view: NSView) -> [String: CGRect] {
        var result: [String: CGRect] = [:]
        func visit(_ node: NSView) {
            if let marker = node as? RecordingRegionMarker {
                result[marker.regionName] = marker.convert(marker.bounds, to: view)
            }
            for child in node.subviews { visit(child) }
        }
        visit(view)
        return result
    }

    static func validateRegions(
        _ regions: [String: CGRect], size: CGSize, focusMode: Bool, baseline: [String: CGRect]?
    ) throws {
        let expected = RecordingLayout.regions(size: size, focusMode: focusMode)
        guard Set(regions.keys) == Set(expected.keys) else {
            throw RecordingContractError(code: "RECORDING.GEOMETRY_MISSING", message: "Hosted region markers are missing")
        }
        let bounds = CGRect(origin: .zero, size: size)
        for (name, frame) in regions {
            guard bounds.insetBy(dx: -1, dy: -1).contains(frame), frame.width > 0, frame.height > 0 else {
                throw RecordingContractError(code: "RECORDING.GEOMETRY_CLIPPED", message: "Hosted region extends outside the window", context: ["region": name])
            }
            guard equalFrame(frame, expected[name]!) else {
                throw RecordingContractError(code: "RECORDING.GEOMETRY_UNEXPECTED", message: "Hosted region disagrees with reserved geometry", context: ["region": name, "actual": "\(frame)", "expected": "\(expected[name]!)"])
            }
            if let previous = baseline?[name], !equalFrame(frame, previous) {
                throw RecordingContractError(code: "RECORDING.GEOMETRY_SHIFT", message: "Transport state changed reserved geometry", context: ["region": name])
            }
            for (otherName, other) in regions where name < otherName {
                let intersection = frame.intersection(other)
                if !intersection.isNull && intersection.width > 1 && intersection.height > 1 {
                    throw RecordingContractError(code: "RECORDING.GEOMETRY_OVERLAP", message: "Reserved regions overlap", context: ["region": name, "other": otherName])
                }
            }
        }
        guard regions["waveform"]!.height >= 120 else {
            throw RecordingContractError(code: "RECORDING.GEOMETRY_WAVEFORM", message: "Waveform is shorter than its reserved minimum")
        }
    }

    private static func equalFrame(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
        abs(lhs.minX - rhs.minX) <= 1 && abs(lhs.minY - rhs.minY) <= 1 &&
        abs(lhs.width - rhs.width) <= 1 && abs(lhs.height - rhs.height) <= 1
    }

    /// The first in-process accessibility element carrying `identifier` under `root`, with its screen
    /// frame. An own-process AX request first makes SwiftUI publish its nodes; no window is ordered.
    @MainActor
    static func accessibilityElement(_ identifier: String, in root: NSView) -> (element: AnyObject, frame: CGRect)? {
        var role: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(AXUIElementCreateApplication(getpid()), kAXRoleAttribute as CFString, &role)
        // SwiftUI's accessibility nodes answer the informal NSAccessibility selectors without declaring the protocol.
        func visit(_ element: AnyObject, depth: Int) -> (element: AnyObject, frame: CGRect)? {
            guard depth < 40 else { return nil }
            if element.accessibilityIdentifier?() == identifier {
                let frame = (element as? NSObject)?.value(forKey: "accessibilityFrame") as? NSValue
                return (element, frame?.rectValue ?? .null)
            }
            for child in (element.accessibilityChildren?() ?? nil) ?? [] {
                if let found = visit(child as AnyObject, depth: depth + 1) { return found }
            }
            return nil
        }
        return visit(root, depth: 0)
    }

    /// TA1: a Light preference registered in this process's defaults (volatile, never written to disk) must
    /// not change a hosted workspace, whose appearance only the hosting view decides; the application scene
    /// alone applies the preference.
    static func registerLightAppearancePreference() {
        UserDefaults.standard.register(defaults: [RecordingAppearancePreference.defaultsKey: RecordingAppearancePreference.light.rawValue])
    }

    /// Only the application scene applies the preference; a hosted workspace must leave `NSApp.appearance` alone.
    @MainActor static func requireApplicationAppearanceUntouched() throws {
        guard NSApplication.shared.appearance == nil else {
            throw RecordingContractError(code: "RECORDING.SNAPSHOT_APPEARANCE", message: "Hosting the workspace applied an application appearance override")
        }
    }

    /// The sidebar corner and the opposite window corner are plain background in every state, so their
    /// rendered luminance proves which appearance the snapshot actually drew.
    static func requireRenderedAppearance(_ bitmap: NSBitmapImageRep, dark: Bool, context: String) throws {
        let corners = [(2, 2), (bitmap.pixelsWide - 3, bitmap.pixelsHigh - 3)]
        let luminances = corners.compactMap { RecordingPalette.renderedLuminance(bitmap, x: $0, y: $1) }
        guard luminances.count == corners.count, luminances.allSatisfy({ dark ? $0 < 0.5 : $0 > 0.5 }) else {
            throw RecordingContractError(code: "RECORDING.SNAPSHOT_APPEARANCE", message: "Rendered background does not match the forced \(dark ? "dark" : "light") appearance", context: ["snapshot": context, "luminance": "\(luminances)"])
        }
    }

    @MainActor @discardableResult
    static func render(view: NSView, size: CGSize, scale: Int, destination: URL) throws -> NSBitmapImageRep {
        guard let bitmap = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int(size.width) * scale,
            pixelsHigh: Int(size.height) * scale, bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ) else {
            throw RecordingContractError(code: "RECORDING.SNAPSHOT_ALLOCATION", message: "Failed to allocate the bounded snapshot bitmap")
        }
        bitmap.size = size
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw RecordingContractError(code: "RECORDING.SNAPSHOT_ENCODING", message: "Failed to encode the synthetic PNG snapshot")
        }
        try data.write(to: destination, options: .atomic)
        return bitmap
    }
}

/// Event-driven barriers observe actual client-bound state and telemetry; no polling sleeps.
@MainActor
private final class RecordingTransportObserver {
    private let model: RecordingWorkspaceModel
    private var subscription: AnyCancellable?
    private var continuation: CheckedContinuation<Void, Error>?
    private var predicate: (() -> Bool)?
    private var deadline: Task<Void, Never>?
    init(model: RecordingWorkspaceModel) {
        self.model = model
        subscription = model.$state.combineLatest(model.waveform.$snapshot).sink { [weak self] _ in
            Task { @MainActor in self?.changed() }
        }
    }
    func wait(timeout: Double = 30, until predicate: @escaping () -> Bool) async throws {
        if predicate() { return }
        if model.state.phase == .recoverableError { throw RecordingContractError(code: model.state.errorCode ?? "RECORDING.TRANSPORT_FAILURE", message: model.state.status) }
        try await withCheckedThrowingContinuation { continuation in
            precondition(self.continuation == nil)
            self.continuation = continuation; self.predicate = predicate
            deadline = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(timeout)) } catch { return }
                guard let self, let continuation = self.continuation else { return }
                self.continuation = nil; self.predicate = nil
                continuation.resume(throwing: RecordingContractError(code: "RECORDING.TRANSPORT_TIMEOUT", message: "Native model did not reach its event-driven barrier: \(model.state.status)"))
            }
        }
    }
    private func changed() {
        guard let continuation, let predicate else { return }
        if predicate() {
            self.continuation = nil; self.predicate = nil; deadline?.cancel(); deadline = nil
            continuation.resume()
        } else if model.state.phase == .recoverableError {
            self.continuation = nil; self.predicate = nil; deadline?.cancel(); deadline = nil
            continuation.resume(throwing: RecordingContractError(code: model.state.errorCode ?? "RECORDING.TRANSPORT_FAILURE", message: model.state.status))
        }
    }
}

@MainActor
final class RecordingFixtureFileObserver {
    private let source: DispatchSourceFileSystemObject
    private var target: URL?
    private var continuation: CheckedContinuation<Void, Error>?
    private var deadline: Task<Void, Never>?
    init(directory: URL) throws {
        let descriptor = open(directory.path, O_EVTONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw RecordingContractError(code: "RECORDING.FIXTURE_BARRIER", message: "Private fixture event directory could not open") }
        source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: descriptor, eventMask: .write, queue: .global(qos: .userInitiated))
        source.setCancelHandler { @Sendable in Darwin.close(descriptor) }
        source.setEventHandler { @Sendable [weak self] in Task { @MainActor in self?.changed() } }
        source.activate()
    }
    func wait(for file: URL, timeout: Double = 2) async throws {
        if FileManager.default.fileExists(atPath: file.path) { return }
        try await withCheckedThrowingContinuation { continuation in
            self.target = file; self.continuation = continuation
            deadline = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(timeout)) } catch { return }
                guard let self, let continuation = self.continuation else { return }
                self.continuation = nil; self.target = nil
                continuation.resume(throwing: RecordingContractError(code: "RECORDING.FIXTURE_TIMEOUT", message: "Actual process did not enter its private delay barrier"))
            }
        }
    }
    private func changed() {
        guard let target, FileManager.default.fileExists(atPath: target.path), let continuation else { return }
        self.target = nil; self.continuation = nil; deadline?.cancel(); deadline = nil
        continuation.resume()
    }
    func close() { deadline?.cancel(); source.cancel() }
    static func release(_ file: URL, byte: UInt8) throws {
        let descriptor = open(file.path, O_WRONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw RecordingContractError(code: "RECORDING.FIXTURE_BARRIER", message: "Actual process fixture FIFO could not open") }
        defer { Darwin.close(descriptor) }
        var info = stat(); var value = byte
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFIFO, info.st_mode & 0o077 == 0,
              write(descriptor, &value, 1) == 1 else { throw RecordingContractError(code: "RECORDING.FIXTURE_BARRIER", message: "Actual process fixture FIFO could not release") }
    }
}
