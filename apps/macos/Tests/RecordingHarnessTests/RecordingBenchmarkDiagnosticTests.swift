import CryptoKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingHarness
import Testing

private let captured = RecordingBenchmarkSourceExemption(
    kind: "captured_take", takeID: "take-a", sourcePath: "/private/synthetic/a.wav",
    sourceSHA256: String(repeating: "a", count: 64), operationID: nil, device: 1, inode: 2)

private func inspectionEvents(cacheMiss: Bool = false) -> [RecordingDiagnosticEvent] {
    var events: [RecordingDiagnosticEvent] = []
    func pair(_ name: String, id: String, parent: String? = nil,
              outcome: RecordingDiagnosticEvent.Outcome = .success,
              bytes: Int64? = nil, identity: Bool = false) {
        let context = RecordingDiagnosticContext(traceID: "trace", preparationID: "preparation",
            takeID: captured.takeID, sourcePath: captured.sourcePath,
            sourceSHA256: captured.sourceSHA256, parentSpanID: parent)
        events.append(RecordingDiagnosticEvent(spanID: id, name: name, phase: .begin,
            uptimeNanoseconds: 100, context: context))
        events.append(RecordingDiagnosticEvent(spanID: id, name: name, phase: .end,
            uptimeNanoseconds: 200, context: context, outcome: outcome,
            byteCount: bytes, device: identity ? 1 : nil, inode: identity ? 2 : nil))
    }
    func lease(_ branch: String, contents: Bool) {
        pair("private_file.open", id: "\(branch)-lease", parent: branch, bytes: 144_044, identity: true)
        pair("private_file.validate_identity", id: "\(branch)-identity", parent: "\(branch)-lease", identity: true)
        if contents {
            for name in ["private_file.read", "private_file.sha256"] {
                pair(name, id: "\(branch)-\(name)", parent: "\(branch)-lease", bytes: 144_044, identity: true)
            }
        }
    }
    pair("playback.prepare", id: "playback")
    lease("playback", contents: true)
    pair("playback.av_audio_file_open", id: "playback-av", parent: "playback")
    pair("waveform.prepare", id: "waveform")
    lease("waveform", contents: cacheMiss)
    pair("waveform.cache_load", id: "cache", parent: "waveform", outcome: cacheMiss ? .cacheMiss : .cacheHit)
    if cacheMiss {
        pair("waveform.av_audio_file_open", id: "waveform-av", parent: "waveform")
        pair("waveform.decode", id: "decode", parent: "waveform", bytes: 144_000)
    }
    return events
}

struct RecordingBenchmarkDiagnosticTests {
    @Test @MainActor func privateTemporaryRootPreservesSelectedAndCapturedSourcePaths() async throws {
        // Foundation's standardization rewrites /private/tmp to /tmp. The already
        // authorized path must stay identical to the real reader's diagnostic path.
        let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("cole-path-control-\(UUID().uuidString.lowercased())", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700])
        let path = root.appendingPathComponent("original.wav").path
        var wav = Data("RIFF".utf8)
        func integer<T: FixedWidthInteger>(_ value: T) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) }
        }
        integer(UInt32(36 + 384)); wav.append(Data("WAVEfmt ".utf8)); integer(UInt32(16))
        integer(UInt16(1)); integer(UInt16(1)); integer(UInt32(48_000)); integer(UInt32(144_000))
        integer(UInt16(3)); integer(UInt16(24)); wav.append(Data("data".utf8)); integer(UInt32(384))
        wav.append(Data(repeating: 0, count: 384))
        try wav.write(to: URL(fileURLWithPath: path), options: .withoutOverwriting)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
        var metadata = stat()
        #expect(lstat(path, &metadata) == 0)
        let identity: RecordingJSON = .object([
            "relative_path": .string("original.wav"), "device": .integer(Int64(metadata.st_dev)),
            "inode": .integer(Int64(metadata.st_ino)), "size_bytes": .integer(metadata.st_size),
            "mode": .integer(Int64(metadata.st_mode)),
            "mtime_ns": .integer(Int64(metadata.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(metadata.st_mtimespec.tv_nsec)),
            "ctime_ns": .integer(Int64(metadata.st_ctimespec.tv_sec) * 1_000_000_000 + Int64(metadata.st_ctimespec.tv_nsec)),
        ])
        let source = try RecordingPlaybackSource(takeID: "path-control",
            sourceSHA256: SHA256.hash(data: wav).map { String(format: "%02x", $0) }.joined(),
            assetRoot: root, relativePath: "original.wav", frameCount: 128, fileIdentity: identity)
        let collector = RecordingBenchmarkDiagnosticCollector(capacity: 100)
        let sink: RecordingDiagnosticSink = { collector.record($0) }
        _ = try RecordingWaveformSelectionLoader.load(source, processGeneration: "path-control", diagnosticSink: sink)
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline, diagnosticSink: sink)
        try await backend.load(source, generation: 1)
        try await backend.recover()
        let snapshot = collector.snapshot()
        #expect(path.hasPrefix("/private/tmp/"))
        #expect(snapshot.events.filter { $0.name == "private_file.open" }.allSatisfy { $0.context.sourcePath == path })
        for kind in ["selected_at_issue", "captured_take"] {
            let exemption = RecordingBenchmarkSourceExemption.selected(source, kind: kind)
            #expect(exemption.sourcePath == path)
            #expect(exemption.device == Int64(metadata.st_dev) && exemption.inode == metadata.st_ino)
            try snapshot.requireInspectionEvidence(for: [exemption])
            let wrongPath = RecordingBenchmarkSourceExemption(kind: kind, takeID: source.takeID,
                sourcePath: "/tmp/" + path.dropFirst("/private/tmp/".count), sourceSHA256: source.sourceSHA256,
                operationID: nil, device: exemption.device, inode: exemption.inode)
            #expect(throws: RecordingContractError.self) { try snapshot.requireInspectionEvidence(for: [wrongPath]) }
            let missingRead = RecordingBenchmarkDiagnosticSnapshot(
                events: snapshot.events.filter { $0.name != "private_file.read" }, capacity: 100, droppedEventCount: 0)
            #expect(throws: RecordingContractError.self) { try missingRead.requireInspectionEvidence(for: [exemption]) }
        }
        #expect(try Data(contentsOf: URL(fileURLWithPath: path)) == wav)
        // Retain this explicitly synthetic diagnostic root, including failed mutants.
        print("path_alias_synthetic_fixture=\(root.path)")
    }

    @Test func eachWaveformBranchProvesOnlyItsActualInspectionPaths() throws {
        for miss in [false, true] {
            let events = inspectionEvents(cacheMiss: miss)
            let snapshot = RecordingBenchmarkDiagnosticSnapshot(events: events, capacity: 100, droppedEventCount: 0)
            try snapshot.requireInspectionEvidence(for: [captured])
            let json = try snapshot.json().object
            #expect(json?["retained_event_count"] == .integer(Int64(events.count)))
            #expect(json?["waveform_cache_miss_count"] == .integer(miss ? 1 : 0))
            #expect(json?["footprint_includes_observer_storage"] == .boolean(true))
        }
    }

    @Test func removingAnyPlaybackReadHookOrWaveformMissHookInvalidatesZero() {
        for name in ["private_file.open", "private_file.read", "private_file.sha256",
                     "private_file.validate_identity", "playback.prepare", "playback.av_audio_file_open",
                     "waveform.prepare", "waveform.cache_load", "waveform.av_audio_file_open", "waveform.decode"] {
            let events = inspectionEvents(cacheMiss: true).filter { $0.name != name }
            let snapshot = RecordingBenchmarkDiagnosticSnapshot(events: events, capacity: 100, droppedEventCount: 0)
            #expect(throws: RecordingContractError.self) { try snapshot.requireInspectionEvidence(for: [captured]) }
        }
    }

    @Test func waveformCannotSupplyPlaybackControlsAndAnUnrelatedTakeCannotSupplyEither() {
        let withoutPlayback = inspectionEvents().filter {
            !$0.spanID.hasPrefix("playback")
        }
        #expect(throws: RecordingContractError.self) {
            try RecordingBenchmarkDiagnosticSnapshot(events: withoutPlayback, capacity: 100, droppedEventCount: 0)
                .requireInspectionEvidence(for: [captured])
        }
        let unrelated = RecordingBenchmarkSourceExemption(kind: "captured_take", takeID: "take-b",
            sourcePath: captured.sourcePath, sourceSHA256: captured.sourceSHA256, operationID: nil, device: 1, inode: 2)
        #expect(throws: RecordingContractError.self) {
            try RecordingBenchmarkDiagnosticSnapshot(events: inspectionEvents(), capacity: 100, droppedEventCount: 0)
                .requireInspectionEvidence(for: [unrelated])
        }
    }

    @Test func missingBeginAndDifferentLeaseIdentityCannotProveARead() {
        let original = inspectionEvents()
        let missingBegin = original.filter { !($0.name == "private_file.read" && $0.phase == .begin) }
        let wrongIdentity = original.map { event in
            guard event.name == "private_file.read", event.phase == .end else { return event }
            return RecordingDiagnosticEvent(spanID: event.spanID, name: event.name, phase: event.phase,
                uptimeNanoseconds: event.uptimeNanoseconds, context: event.context, outcome: event.outcome,
                byteCount: event.byteCount, device: 99, inode: 99)
        }
        for events in [missingBegin, wrongIdentity] {
            #expect(throws: RecordingContractError.self) {
                try RecordingBenchmarkDiagnosticSnapshot(events: events, capacity: 100, droppedEventCount: 0)
                    .requireInspectionEvidence(for: [captured])
            }
        }
    }

    @Test func boundedCollectorFailsExplicitlyOnOverflowAndRetainsOriginalEvents() throws {
        let collector = RecordingBenchmarkDiagnosticCollector(capacity: 2)
        let events = inspectionEvents()
        events.prefix(3).forEach(collector.record)
        let snapshot = collector.snapshot()
        #expect(snapshot.events == Array(events.prefix(2)))
        #expect(snapshot.droppedEventCount == 1)
        #expect(collector.events(traceID: "trace").count == 2)
        do {
            try snapshot.requireInspectionEvidence(for: [captured])
            Issue.record("Truncated inspection audit was accepted")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.BENCHMARK_AUDIT_OVERFLOW")
        }
    }
}
