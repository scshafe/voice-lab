import AppKit
import Combine
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

struct RecordingBenchmarkNavigationState {
    var canContinueMissing: Bool
    var completeness: String
    var historyRevision: Int64
    var navigationPending: Bool
    var activeOperationID: String?

    var available: Bool { canContinueMissing && completeness == "complete" && !navigationPending }
}

struct RecordingBenchmarkNavigationMeasurement {
    let issued: UInt64
    let acknowledged: UInt64
    let operationID: String
}

/// Harness-only dispatch proof. Availability is outside the command window; once
/// dispatched, every request/ACK wait and every command error belongs to that window.
/// A disabled control or an early-returning model method can never become a fast sample.
@MainActor enum RecordingBenchmarkNavigationProbe {
    nonisolated static let availabilityBudgetNanoseconds: UInt64 = 120_000_000_000

    static func measure(
        round: Int, availabilityBudget: UInt64 = availabilityBudgetNanoseconds,
        state: @MainActor () -> RecordingBenchmarkNavigationState,
        now: @MainActor () -> UInt64,
        issuedUptime: @MainActor () async -> UInt64,
        waitForAvailability: @MainActor (UInt64) async throws -> Void,
        beforeIssue: @MainActor () -> Void,
        dispatch: @MainActor () -> Void,
        waitForAcknowledgment: @MainActor (String) async throws -> Void,
        retainAvailability: @MainActor (RecordingJSON) -> Void
    ) async throws -> RecordingBenchmarkNavigationMeasurement {
        let availabilityStarted = now()
        let deadline = availabilityStarted + availabilityBudget
        let initial = state()
        var commandIssued: UInt64?
        var commandFinished: UInt64?
        var operationID: String?
        var dispatchState: RecordingBenchmarkNavigationState?
        var lostEligibility = 0
        var outcome = "availability-or-dispatch-failed"
        defer {
            let ended = commandIssued ?? now()
            let final = state()
            // One bounded record per lookup, including failure; no per-poll history.
            retainAvailability(.object([
                "operation": .string("next_missing_availability"), "round": .integer(Int64(round)),
                "started_uptime_ns": .integer(Int64(availabilityStarted)),
                "ended_uptime_ns": .integer(Int64(ended)),
                "duration_ns": .integer(Int64(ended - availabilityStarted)),
                "budget_ns": .integer(Int64(availabilityBudget)),
                "initial_completeness": .string(initial.completeness),
                "initial_history_revision": .integer(initial.historyRevision),
                "final_completeness": .string(final.completeness),
                "final_history_revision": .integer(final.historyRevision),
                "eligibility_lost_before_dispatch": .integer(Int64(lostEligibility)),
                "command_dispatched": .boolean(commandIssued != nil),
                "request_started_uptime_ns": commandIssued.map { .integer(Int64($0)) } ?? .null,
                "request_finished_uptime_ns": commandFinished.map { .integer(Int64($0)) } ?? .null,
                "operation_id": operationID.map(RecordingJSON.string) ?? .null,
                "dispatch_completeness": dispatchState.map { .string($0.completeness) } ?? .null,
                "dispatch_history_revision": dispatchState.map { .integer($0.historyRevision) } ?? .null,
                "outcome": .string(outcome),
            ]))
        }
        func requireTime() throws {
            guard now() < deadline else {
                throw RecordingContractError(code: "RECORDING.BENCHMARK_AVAILABILITY_TIMEOUT",
                    message: "Continue Missing did not become dispatchable within its availability budget")
            }
        }
        while true {
            try requireTime()
            if !state().available { try await waitForAvailability(deadline) }
            try requireTime()
            beforeIssue()
            let issued = await issuedUptime()
            let atClick = state()
            try requireTime()
            // No suspension between this authoritative-state recheck and the call.
            guard atClick.available else { lostEligibility += 1; continue }
            dispatch()
            let dispatched = state()
            guard dispatched.navigationPending, let newOperation = dispatched.activeOperationID,
                  newOperation.hasPrefix("navigate-"), newOperation != atClick.activeOperationID else {
                throw RecordingContractError(code: "RECORDING.BENCHMARK_NAVIGATION_NOT_DISPATCHED",
                    message: "Continue Missing did not begin a fresh pending navigation operation")
            }
            commandIssued = issued
            operationID = newOperation
            dispatchState = atClick
            let acknowledgedTime: UInt64
            do {
                try await waitForAcknowledgment(newOperation)
                acknowledgedTime = now()
                commandFinished = acknowledgedTime
                let acknowledged = state()
                guard !acknowledged.navigationPending, acknowledged.activeOperationID == nil else {
                    throw RecordingContractError(code: "RECORDING.BENCHMARK_NAVIGATION_NOT_ACKNOWLEDGED",
                        message: "The actual navigation operation did not reach its acknowledgment")
                }
            } catch {
                commandFinished = now()
                outcome = "dispatched-failed"
                throw error // In particular, dispatched HISTORY_UNKNOWN is never retried.
            }
            outcome = "acknowledged"
            return RecordingBenchmarkNavigationMeasurement(
                issued: issued, acknowledged: acknowledgedTime, operationID: newOperation)
        }
    }
}

struct RecordingBenchmarkMetadataMeasurement {
    let issued: UInt64
    let applied: UInt64
    let requestID: String
    let traceID: String
}

/// A metadata refresh has no synchronous operation token. Its actual service span and
/// matching application event prove dispatch and completion, including very fast replies.
@MainActor enum RecordingBenchmarkMetadataProbe {
    static func measure(
        round: Int, availabilityBudget: UInt64 = RecordingBenchmarkNavigationProbe.availabilityBudgetNanoseconds,
        refreshInFlight: @MainActor () -> Bool,
        now: @MainActor () -> UInt64, issuedUptime: @MainActor () async -> UInt64,
        waitForIdle: @MainActor (UInt64) async throws -> Void,
        beforeIssue: @MainActor () -> Void,
        refresh: @MainActor (RecordingDiagnosticContext) async -> Void,
        events: @MainActor (String) -> [RecordingDiagnosticEvent],
        retainAvailability: @MainActor (RecordingJSON) -> Void
    ) async throws -> RecordingBenchmarkMetadataMeasurement {
        let started = now()
        let deadline = started + availabilityBudget
        let traceID = "benchmark-history-\(round)-\(UUID().uuidString.lowercased())"
        var issued: UInt64?
        var lostReadiness = 0
        var acknowledged = false
        defer {
            let ended = issued ?? now()
            retainAvailability(.object([
                "operation": .string("metadata_refresh_availability"), "round": .integer(Int64(round)),
                "started_uptime_ns": .integer(Int64(started)), "ended_uptime_ns": .integer(Int64(ended)),
                "duration_ns": .integer(Int64(ended - started)), "budget_ns": .integer(Int64(availabilityBudget)),
                "readiness_lost_before_dispatch": .integer(Int64(lostReadiness)),
                "refresh_invoked": .boolean(issued != nil), "acknowledged": .boolean(acknowledged),
                "trace_id": .string(traceID),
            ]))
        }
        func requireTime() throws {
            guard now() < deadline else {
                throw RecordingContractError(code: "RECORDING.BENCHMARK_AVAILABILITY_TIMEOUT", message: "Metadata refresh did not become idle within its availability budget")
            }
        }
        while true {
            try requireTime()
            if refreshInFlight() { try await waitForIdle(deadline) }
            try requireTime()
            let nearEnd = await issuedUptime()
            try requireTime()
            guard !refreshInFlight() else { lostReadiness += 1; continue }
            beforeIssue()
            issued = nearEnd
            await refresh(RecordingDiagnosticContext(traceID: traceID))
            let farEnd = now()
            let evidence = events(traceID)
            let refreshes = evidence.filter { $0.name == "history.refresh" && $0.phase == .end }
            let requests = evidence.filter { $0.name == "service.request.history" && $0.phase == .end }
            let applications = evidence.filter { $0.name == "history.application" && $0.phase == .end }
            guard refreshes.count == 1, refreshes[0].outcome == .success,
                  requests.count == 1, requests[0].outcome == .success,
                  let requestID = requests[0].context.requestID,
                  applications.count == 1, applications[0].outcome == .success,
                  applications[0].context.requestID == requestID,
                  refreshes[0].context.requestID == requestID,
                  requests[0].uptimeNanoseconds <= applications[0].uptimeNanoseconds,
                  applications[0].uptimeNanoseconds <= refreshes[0].uptimeNanoseconds,
                  evidence.allSatisfy({ $0.uptimeNanoseconds >= nearEnd && $0.uptimeNanoseconds <= farEnd }),
                  [refreshes[0], requests[0], applications[0]].allSatisfy({ end in
                      evidence.contains { $0.spanID == end.spanID && $0.phase == .begin && $0.name == end.name }
                  }) else {
                throw RecordingContractError(code: "RECORDING.BENCHMARK_METADATA_NOT_APPLIED", message: "Metadata refresh lacks one actual request and its applied correlated response")
            }
            acknowledged = true
            return RecordingBenchmarkMetadataMeasurement(issued: nearEnd, applied: farEnd, requestID: requestID, traceID: traceID)
        }
    }
}

/// Fixed command-issue authorization; a later UI selection cannot rewrite its scope.
struct RecordingBenchmarkSourceExemption: Codable, Sendable, Equatable {
    let kind: String
    let takeID: String
    let sourcePath: String
    let sourceSHA256: String?
    let operationID: String?
    let device: Int64?
    let inode: UInt64?

    static func selected(_ source: RecordingPlaybackSource, kind: String = "selected_at_issue") -> Self {
        Self(kind: kind, takeID: source.takeID,
             sourcePath: source.assetRoot.appendingPathComponent(source.relativePath).path,
             sourceSHA256: source.sourceSHA256, operationID: nil,
             device: source.fileIdentity?.object?["device"]?.integer,
             inode: source.fileIdentity?.object?["inode"]?.integer.flatMap { UInt64(exactly: $0) })
    }

    static func reserved(takeID: String, sourcePath: String, operationID: String) -> Self {
        Self(kind: "reserved_take", takeID: takeID, sourcePath: sourcePath,
             sourceSHA256: nil, operationID: operationID, device: nil, inode: nil)
    }
}

struct RecordingBenchmarkDiagnosticSnapshot: Sendable {
    let events: [RecordingDiagnosticEvent]
    let capacity: Int
    let droppedEventCount: Int

    func requireInspectionEvidence(for sources: [RecordingBenchmarkSourceExemption]) throws {
        guard droppedEventCount == 0 else {
            throw RecordingContractError(code: "RECORDING.BENCHMARK_AUDIT_OVERFLOW", message: "Native diagnostic events exceeded their fixed bound")
        }
        guard !sources.isEmpty else { throw missingControl("No captured source establishes instrumentation") }
        let grouped = Dictionary(grouping: events, by: \.spanID)
        func completed(_ name: String, parent: String? = nil,
                       source: RecordingBenchmarkSourceExemption,
                       outcome: RecordingDiagnosticEvent.Outcome = .success) -> [RecordingDiagnosticEvent] {
            events.filter { event in
                event.name == name && event.phase == .end && event.outcome == outcome &&
                    event.context.takeID == source.takeID && event.context.sourcePath == source.sourcePath &&
                    event.context.sourceSHA256 == source.sourceSHA256 &&
                    (parent == nil || event.context.parentSpanID == parent) &&
                    (grouped[event.spanID] ?? []).contains { begin in
                        begin.phase == .begin && begin.name == name && begin.context == event.context &&
                            begin.uptimeNanoseconds <= event.uptimeNanoseconds
                    }
            }
        }
        func lease(_ parent: String, source: RecordingBenchmarkSourceExemption, contents: Bool) -> Bool {
            completed("private_file.open", parent: parent, source: source).contains { opened in
                guard opened.device != nil, opened.inode != nil, (opened.byteCount ?? 0) > 0,
                      source.device == nil || opened.device == source.device,
                      source.inode == nil || opened.inode == source.inode else { return false }
                guard completed("private_file.validate_identity", parent: opened.spanID, source: source).contains(where: {
                    $0.device == opened.device && $0.inode == opened.inode
                }) else { return false }
                guard contents else { return true }
                return ["private_file.read", "private_file.sha256"].allSatisfy { name in
                    completed(name, parent: opened.spanID, source: source).contains {
                        $0.device == opened.device && $0.inode == opened.inode && $0.byteCount == opened.byteCount
                    }
                }
            }
        }
        for source in sources {
            let playback = completed("playback.prepare", source: source).contains { prepared in
                lease(prepared.spanID, source: source, contents: true) &&
                    !completed("playback.av_audio_file_open", parent: prepared.spanID, source: source).isEmpty
            }
            let waveform = completed("waveform.prepare", source: source).contains { prepared in
                guard lease(prepared.spanID, source: source, contents: false) else { return false }
                if !completed("waveform.cache_load", parent: prepared.spanID, source: source, outcome: .cacheHit).isEmpty { return true }
                return !completed("waveform.cache_load", parent: prepared.spanID, source: source, outcome: .cacheMiss).isEmpty &&
                    lease(prepared.spanID, source: source, contents: true) &&
                    !completed("waveform.av_audio_file_open", parent: prepared.spanID, source: source).isEmpty &&
                    completed("waveform.decode", parent: prepared.spanID, source: source).contains { ($0.byteCount ?? 0) > 0 }
            }
            guard playback && waveform else { throw missingControl("A captured source lacks playback/lease-read or waveform branch controls") }
        }
    }

    private func missingControl(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.BENCHMARK_AUDIT_MISSING_CONTROL", message: message)
    }

    func json() throws -> RecordingJSON {
        .object([
            "schema_version": .string("cole-voice-lab.recording-benchmark-diagnostics/v1"),
            "clock": .string("mach_uptime_ns"),
            "event_capacity": .integer(Int64(capacity)),
            "retained_event_count": .integer(Int64(events.count)),
            "footprint_includes_observer_storage": .boolean(true),
            "dropped_event_count": .integer(Int64(droppedEventCount)),
            "events": try recordingBenchmarkJSON(events),
            "waveform_cache_hit_count": .integer(Int64(events.filter { $0.name == "waveform.cache_load" && $0.phase == .end && $0.outcome == .cacheHit }.count)),
            "waveform_cache_miss_count": .integer(Int64(events.filter { $0.name == "waveform.cache_load" && $0.phase == .end && $0.outcome == .cacheMiss }.count)),
        ])
    }
}

/// One bounded sink shared by actor and detached-reader events; no I/O or encoding here.
final class RecordingBenchmarkDiagnosticCollector: @unchecked Sendable {
    private let lock = NSLock()
    private let capacity: Int
    private var events: [RecordingDiagnosticEvent] = []
    private var dropped = 0

    init(capacity: Int = 32_768) {
        precondition(capacity > 0)
        self.capacity = capacity
        events.reserveCapacity(capacity)
    }

    func record(_ event: RecordingDiagnosticEvent) {
        lock.lock(); defer { lock.unlock() }
        guard events.count < capacity else { dropped += 1; return }
        events.append(event)
    }

    func snapshot() -> RecordingBenchmarkDiagnosticSnapshot {
        lock.lock(); defer { lock.unlock() }
        return RecordingBenchmarkDiagnosticSnapshot(events: events, capacity: capacity, droppedEventCount: dropped)
    }

    func events(traceID: String) -> [RecordingDiagnosticEvent] {
        lock.lock(); defer { lock.unlock() }
        return events.filter { $0.context.traceID == traceID }
    }
}

private func recordingBenchmarkJSON(_ value: some Encodable) throws -> RecordingJSON {
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    return try RecordingJSON.parse(encoder.encode(value))
}

/// Physical footprint is attributed by process role; analysis workers are not recorder memory.
struct RecordingFootprintReading: Sendable {
    var client: Int64 = 0
    var recorder: Int64 = 0
    var service: Int64 = 0
    var other: Int64 = 0

    func combinedWith(_ other: RecordingFootprintReading) -> RecordingFootprintReading {
        RecordingFootprintReading(
            client: max(client, other.client), recorder: max(recorder, other.recorder),
            service: max(service, other.service), other: max(self.other, other.other))
    }

    var json: RecordingJSON {
        .object([
            "client_bytes": .integer(client), "recorder_bytes": .integer(recorder),
            "service_bytes": .integer(service), "other_bytes": .integer(other),
        ])
    }
}

final class RecordingFootprintSampler: @unchecked Sendable {
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "cole.voice.benchmark.footprint")
    private let recorderPath: String
    private let servicePath: String
    private var timer: DispatchSourceTimer?
    private var maximum = RecordingFootprintReading()
    private var count = 0
    private var window = RecordingFootprintReading()

    init(recorderPath: String, servicePath: String) {
        self.recorderPath = recorderPath
        self.servicePath = servicePath
    }

    /// Four times MAXPATHLEN, the documented bound for this call; the macro that names it
    /// is not importable, so the bound is stated here rather than guessed at.
    static let pathBound = 4 * Int(MAXPATHLEN)

    static func executablePath(_ pid: pid_t) -> String {
        let buffer = UnsafeMutablePointer<CChar>.allocate(capacity: pathBound)
        defer { buffer.deallocate() }
        guard proc_pidpath(pid, buffer, UInt32(pathBound)) > 0 else { return "" }
        return String(cString: buffer)
    }

    static func footprint(_ pid: pid_t) -> Int64 {
        var info = rusage_info_v4()
        let code = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_V4, $0)
            }
        }
        return code == 0 ? Int64(bitPattern: info.ri_phys_footprint) : 0
    }

    static func childrenByParent() -> [pid_t: [pid_t]] {
        var name: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
        var length = 0
        guard sysctl(&name, 4, nil, &length, nil, 0) == 0, length > 0 else { return [:] }
        var buffer = [kinfo_proc](
            repeating: kinfo_proc(), count: length / MemoryLayout<kinfo_proc>.stride + 64)
        length = buffer.count * MemoryLayout<kinfo_proc>.stride
        guard sysctl(&name, 4, &buffer, &length, nil, 0) == 0 else { return [:] }
        var children: [pid_t: [pid_t]] = [:]
        for index in 0..<(length / MemoryLayout<kinfo_proc>.stride) {
            children[buffer[index].kp_eproc.e_ppid, default: []].append(buffer[index].kp_proc.p_pid)
        }
        return children
    }

    /// The client spawns exactly two direct children, the recorder helper and the service.
    /// Anything deeper is an analysis worker doing background work. Classifying by process
    /// shape rather than by executable path survives an interpreter resolved through a shim.
    func treeFootprint() -> RecordingFootprintReading {
        let root = getpid()
        var reading = RecordingFootprintReading(client: RecordingFootprintSampler.footprint(root))
        let children = RecordingFootprintSampler.childrenByParent()
        for child in children[root] ?? [] {
            let bytes = RecordingFootprintSampler.footprint(child)
            let path = RecordingFootprintSampler.executablePath(child)
            if path == recorderPath { reading.recorder += bytes }
            else if path == servicePath || !path.isEmpty { reading.service += bytes }
            var pending = children[child] ?? []
            while let descendant = pending.popLast() {
                reading.other += RecordingFootprintSampler.footprint(descendant)
                pending.append(contentsOf: children[descendant] ?? [])
            }
        }
        return reading
    }

    /// Sample often enough that a transient recorder peak inside one take is observed.
    func start(intervalMilliseconds: Int = 200) {
        let source = DispatchSource.makeTimerSource(queue: queue)
        source.schedule(deadline: .now(), repeating: .milliseconds(intervalMilliseconds))
        source.setEventHandler { [weak self] in
            guard let self else { return }
            let value = treeFootprint()
            lock.lock()
            maximum = maximum.combinedWith(value)
            window = window.combinedWith(value)
            count += 1
            lock.unlock()
        }
        lock.lock(); timer = source; lock.unlock()
        source.resume()
    }

    func stop() {
        lock.lock(); let source = timer; timer = nil; lock.unlock()
        source?.cancel()
    }

    /// Reset the windowed peak so one take's capture peak is not another take's.
    func beginWindow() { lock.lock(); window = RecordingFootprintReading(); lock.unlock() }

    var windowPeak: RecordingFootprintReading {
        lock.lock(); defer { lock.unlock() }; return window
    }
    var peak: RecordingFootprintReading { lock.lock(); defer { lock.unlock() }; return maximum }
    var samples: Int { lock.lock(); defer { lock.unlock() }; return count }
}

extension RecordingHarness {
    /// Measure the real configured release stack for the acceptance rows whose own boundary
    /// exists without a display. Every emitted sample names the exact near and far end it
    /// timed and carries absolute uptime, so a separate cross-process audit can attribute
    /// historical audio reads to the command that was running. Rows needing a presented
    /// frame, a device or a person are never emitted here.
    @MainActor static func benchmark(configurationURL: URL, directory: URL, cycles: Int,
                                     completionPolicy: RecordingBenchmarkCompletionPolicy = .exactFrames) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else {
            throw benchmarkError("Benchmark requires explicit synthetic PCM")
        }
        let assetRoot = config.assetRoot.path
        // Synthetic emission has a declared frame target; the production maximum can
        // stop first. Keep target completion separate from transport termination.
        guard let totalIndex = config.recorder.arguments.firstIndex(of: "--synthetic-total-frames"),
              totalIndex + 1 < config.recorder.arguments.count,
              let expectedFrames = Int64(config.recorder.arguments[totalIndex + 1]) else {
            throw benchmarkError("Benchmark requires an explicit synthetic take length")
        }
        guard completionPolicy != .maximumDurationLimit || expectedFrames == 28_800_000 else {
            throw benchmarkError("Maximum-duration policy requires the explicit 600-second design point")
        }
        let diagnostics = RecordingBenchmarkDiagnosticCollector()
        let diagnosticSink: RecordingDiagnosticSink = { diagnostics.record($0) }
        let model = RecordingWorkspaceModel(
            configuration: config, permissionStatus: { .granted }, permissionRequest: { false },
            reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline, diagnosticSink: diagnosticSink),
            diagnosticSink: diagnosticSink)
        defer { model.close() }
        // A ten-minute take plus a thousand-take history does not fit an interactive budget.
        let observer = ReviewObserver(model, barrierSeconds: 1800)
        let sampler = RecordingFootprintSampler(
            recorderPath: config.recorder.executable.resolvingSymlinksInPath().path,
            servicePath: config.service.executable.resolvingSymlinksInPath().path)
        do {
            await model.connect()
            try await observer.wait { model.canRecord && model.state.historyCompleteness == "complete" }
        } catch {
            let partial: RecordingJSON = .object([
                "suite": .string("benchmark"), "failure": .string(String(describing: error)),
                "failure_stage": .string("connect-or-initial-coverage"), "samples": .array([]),
                "failure_checkpoint": ReviewObserver.checkpoint(model, label: "initial-coverage-failure"),
            ])
            try partial.transportData().write(to: directory.appendingPathComponent("benchmark-report.json"), options: .atomic)
            throw error
        }

        // The hosted workspace is realized but never ordered on screen: region markers give
        // real measured geometry, which is the only part of layout stability that exists
        // without a display server. No presented frame is claimed anywhere in this report.
        let host = NSHostingView(rootView: RecordingWorkspace(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
        host.appearance = NSAppearance(named: .aqua)
        host.layoutSubtreeIfNeeded()
        let baseline = measuredRegions(view: host)
        try validateRegions(baseline, size: host.frame.size, focusMode: false, baseline: nil)

        // Raw descriptor holdings are a separate lifetime diagnostic, never open events.
        var clientHeldPaths: Set<String> = []
        var drift = 0.0
        var layoutMeasurements = 0
        // A region that stops being measured is a layout failure, not an infinite drift:
        // JSONEncoder rejects a non-finite number, so a sentinel here would lose the whole
        // case at report time instead of reporting the one thing that went wrong.
        var missingRegions: Set<String> = []
        var observingLayout = true
        // Laying out an offscreen hosting view on every cycle would put the harness's own
        // SwiftUI work inside the memory and latency it is measuring. Layout stability is
        // a property of the states, not of how many times they are measured, so it is
        // observed over the opening cycles and again at the end.
        func observeLayout() {
            guard observingLayout else { return }
            host.layoutSubtreeIfNeeded()
            let current = measuredRegions(view: host)
            layoutMeasurements += 1
            for (name, frame) in current {
                guard let previous = baseline[name] else { missingRegions.insert(name); continue }
                drift = max(
                    drift,
                    max(
                        abs(frame.minX - previous.minX), abs(frame.minY - previous.minY),
                        abs(frame.width - previous.width), abs(frame.height - previous.height)))
            }
        }

        // Warmed idle: connected, history complete, hosted and laid out, nothing recorded.
        sampler.start()
        try await Task.sleep(for: .seconds(1))
        let warmIdle = sampler.peak
        var samples: [RecordingJSON] = []
        var agreements: [RecordingJSON] = []
        var captureBoundaries: [RecordingJSON] = []
        var reservationInterner = RecordingBenchmarkReservationInterner()
        var capturedSources: [RecordingBenchmarkSourceExemption] = []
        var checkpoints: [RecordingJSON] = []
        var navigationAvailability: [RecordingJSON] = []
        var metadataAvailability: [RecordingJSON] = []
        func checkpoint(_ label: String) {
            checkpoints.append(ReviewObserver.checkpoint(model, label: label))
            // Keep the failure neighborhood without making the diagnostic itself grow
            // throughout a long memory benchmark. Full command samples remain separate.
            if checkpoints.count > 64 { checkpoints.removeFirst(checkpoints.count - 64) }
        }

        /// Endpoint FD sampling cannot establish an open or a read; preserve it unfiltered.
        func heldWAVPaths() -> Set<String> {
            let resolved = UnsafeMutablePointer<CChar>.allocate(capacity: Int(MAXPATHLEN))
            defer { resolved.deallocate() }
            var held: Set<String> = []
            for candidate in 0..<1024 {
                guard fcntl(Int32(candidate), F_GETPATH, resolved) >= 0 else { continue }
                let path = String(cString: resolved)
                guard path.hasSuffix(".wav"), path.hasPrefix(assetRoot + "/") else { continue }
                held.insert(path)
            }
            clientHeldPaths.formUnion(held)
            return held
        }

        func selectedExemptions() -> [RecordingBenchmarkSourceExemption] {
            model.selectedReview.map { [.selected($0.playback)] } ?? []
        }

        func sample(_ operation: String, _ nearEnd: UInt64, _ farEnd: UInt64, boundary: String,
                    cycle: Int, frames: Int64, held: Set<String> = [], original: String? = nil,
                    exemptions: [RecordingBenchmarkSourceExemption] = [],
                    advanceIntent: Bool? = nil, completenessAtClick: String? = nil,
                    navigationOperationID: String? = nil, requestID: String? = nil,
                    diagnosticTraceID: String? = nil) throws {
            samples.append(.object([
                "operation": .string(operation),
                "started_uptime_ns": .integer(Int64(bitPattern: nearEnd)),
                "ended_uptime_ns": .integer(Int64(bitPattern: farEnd)),
                "duration_ns": .integer(Int64(bitPattern: farEnd &- nearEnd)),
                "measured_boundary": .string(boundary),
                "cycle": .integer(Int64(cycle)),
                "written_frames": .integer(frames),
                "client_held_wav_paths_before": .array(held.sorted().map(RecordingJSON.string)),
                "client_held_wav_paths_after": .array(heldWAVPaths().sorted().map(RecordingJSON.string)),
                "source_exemptions": try recordingBenchmarkJSON(exemptions),
                "take_original_path": original.map(RecordingJSON.string) ?? .null,
                "advance_intent": advanceIntent.map(RecordingJSON.boolean) ?? .null,
                "history_completeness_at_click": completenessAtClick.map(RecordingJSON.string) ?? .null,
                "navigation_operation_id": navigationOperationID.map(RecordingJSON.string) ?? .null,
                "request_id": requestID.map(RecordingJSON.string) ?? .null,
                "diagnostic_trace_id": diagnosticTraceID.map(RecordingJSON.string) ?? .null,
                "status": .string("ok"),
            ]))
        }

        let totalCycles = max(1, cycles)
        var completedCycles = 0
        var saveNextEligible = 0
        var plainSaveEligible = 0
        var saveNextDispatched = 0
        var plainSaveDispatched = 0
        var saveNextAcknowledged = 0
        var plainSaveAcknowledged = 0
        // A session that fails closed mid-run still measured everything before it failed.
        // That evidence is what says which cycle and which command the failure arrived on,
        // so it is written out rather than discarded with the process.
        var failure: String?
        do {
        for cycle in 0..<totalCycles {
            observingLayout = cycle < 3 || cycle == totalCycles - 1
            sampler.beginWindow()
            // Record: the command call through to the first accepted confirmed frames. The
            // device half of this row is absent by construction under synthetic capture, so
            // it is emitted under the substitute name the report declares, never the row's.
            let beforeRecord = heldWAVPaths()
            checkpoint("cycle-\(cycle)-before-record")
            let previousTakeID = model.state.takeID
            let previousOperationID = model.state.activeOperationID
            let recordIssued = await issuedUptime()
            guard model.canRecord else { throw benchmarkError("Cycle Record is unavailable") }
            let recordSelected = selectedExemptions()
            model.record()
            guard model.state.phase == .starting, let recordOperationID = model.state.activeOperationID,
                  recordOperationID != previousOperationID, model.waveform.snapshot == nil else {
                throw benchmarkError("Record did not dispatch a fresh operation and clear the prior waveform")
            }
            try await observer.wait {
                guard let snapshot = model.waveform.snapshot, let selectedTakeID = model.state.takeID else { return false }
                return selectedTakeID != previousTakeID && snapshot.takeID == selectedTakeID &&
                    snapshot.processGeneration == config.recorder.processGeneration && snapshot.writtenFrames > 0
            }
            let firstFrames = DispatchTime.now().uptimeNanoseconds
            guard let takeID = model.state.takeID,
                  let reservation = model.state.take.object?["reservation"]?.object,
                  reservation["take_id"] == .string(takeID), reservation["operation_id"] == .string(recordOperationID),
                  let relativeOriginal = reservation["original_path"]?.string else {
                throw benchmarkError("First frames lack their exact Record reservation source")
            }
            let original = assetRoot + "/" + relativeOriginal
            let reservedSource = RecordingBenchmarkSourceExemption.reserved(
                takeID: takeID, sourcePath: original, operationID: recordOperationID)
            let captureExemptions = recordSelected + [reservedSource]
            try sample("record_to_first_synthetic_confirmed_frame", recordIssued, firstFrames,
                   boundary: "record() call to the first accepted telemetry carrying frames",
                   cycle: cycle, frames: model.waveform.snapshot?.writtenFrames ?? 0,
                   held: beforeRecord, original: original, exemptions: captureExemptions)
            observeLayout()

            try await observer.wait { model.state.canStop }
            // The helper emits synthetic frames on a real-time timer, so the take reaches its
            // configured length here; this wait is the fixture's own duration, not latency.
            // Its barrier is the fixture's own length plus a bounded allowance: a take that
            // comes up short is recorded as short and stopped, never waited on indefinitely.
            let takeBarrier = ReviewObserver(
                model, barrierSeconds: Int(expectedFrames / 48_000) + 30)
            var captureWaitTimedOut = false
            do {
                try await takeBarrier.wait {
                    !model.state.canStop || (completionPolicy == .exactFrames
                        && (model.waveform.snapshot?.writtenFrames ?? 0) >= expectedFrames)
                }
            } catch let error as RecordingContractError where error.code == "RECORDING.REVIEW_TIMEOUT" {
                captureWaitTimedOut = true
            }
            observeLayout()
            // Polling is continuous telemetry rather than a discrete command, so the whole
            // capture window is the poll window for the purpose of historical audio reads.
            try sample("capture_poll_window", recordIssued, DispatchTime.now().uptimeNanoseconds,
                   boundary: "the whole capture window, during which telemetry is polled",
                   cycle: cycle, frames: model.waveform.snapshot?.writtenFrames ?? 0,
                   held: beforeRecord, original: original, exemptions: captureExemptions)
            let capturePeak = sampler.windowPeak
            // Retain the original pre-issue observation; never replace it with final frames.
            let timerFrames = model.waveform.snapshot?.writtenFrames ?? 0
            let waitExit = RecordingBenchmarkCaptureBoundary.waitExit(
                expected: expectedFrames, observed: timerFrames, timedOut: captureWaitTimedOut)

            let beforeStop = heldWAVPaths()
            checkpoint("cycle-\(cycle)-capture-completion-boundary")
            var stopExemptions: [RecordingBenchmarkSourceExemption] = []
            func captureState() -> RecordingBenchmarkCaptureState {
                RecordingBenchmarkCaptureState(takeID: model.state.takeID,
                    generation: model.waveform.processGeneration, phase: model.state.phase.rawValue,
                    canStop: model.state.canStop, writtenFrames: model.waveform.snapshot?.writtenFrames ?? 0)
            }
            let terminalObserved = DispatchTime.now().uptimeNanoseconds
            let stopAttempt: RecordingBenchmarkStopAttempt
            if completionPolicy == .maximumDurationLimit {
                stopAttempt = RecordingBenchmarkCaptureBoundary.automaticLimitObservation(captureState())
            } else {
                stopAttempt = await RecordingBenchmarkCaptureBoundary.issueStop(
                    takeID: takeID, generation: config.recorder.processGeneration, state: captureState,
                    issuedUptime: { await issuedUptime() },
                    dispatch: { stopExemptions = selectedExemptions() + [reservedSource]; model.stop() })
            }
            var captureBoundary: [String: RecordingJSON] = [
                "cycle": .integer(Int64(cycle)), "expected_frames": .integer(expectedFrames),
                "completion_policy": .string(completionPolicy.rawValue),
                "reservation": .object(reservation),
                "reservation_sha256": model.state.take.object?["reservation_sha256"] ?? .null,
                "capture_started_uptime_ns": .integer(Int64(recordIssued)),
                "terminal_observed_uptime_ns": .integer(Int64(terminalObserved)),
                "wait_timed_out": .boolean(captureWaitTimedOut),
                "timer_frames_before_issue": .integer(timerFrames), "wait_exit": .string(waitExit),
                "stop_attempt": stopAttempt.json, "sealed_frames": .null,
                "terminal_reason": model.state.take.object?["seal"]?.object?["continuity"]?.object?["stop_reason"] ?? .null]
            let captureBoundaryIndex = captureBoundaries.count
            captureBoundaries.append(.object(captureBoundary))
            guard !captureWaitTimedOut else {
                throw benchmarkError("Capture wait timed out before its declared completion boundary")
            }
            guard model.state.takeID == takeID, model.waveform.processGeneration == config.recorder.processGeneration else {
                throw benchmarkError("Capture identity changed before Stop; no Stop latency measured")
            }
            if let stopIssued = stopAttempt.issued, stopAttempt.modelStopAccepted {
                try await observer.wait { !model.state.canStop }
                let stopAcknowledged = DispatchTime.now().uptimeNanoseconds
                try sample("stop_model_transition", stopIssued, stopAcknowledged,
                       boundary: "model.stop() call to its local stopping-state publication; no helper acknowledgment measured",
                       cycle: cycle, frames: timerFrames, held: beforeStop, original: original,
                       exemptions: stopExemptions)
            }

            try await observer.wait {
                model.canSave && model.selectedReview?.playback.takeID == takeID &&
                    model.waveform.snapshot?.takeID == takeID && !model.review.loading
            }
            let sealed = DispatchTime.now().uptimeNanoseconds
            if let stopIssued = stopAttempt.issued, stopAttempt.modelStopAccepted {
                try sample("stop_to_sealed_review", stopIssued, sealed,
                       boundary: "stop() call to a sealed take with its review source loaded",
                       cycle: cycle, frames: model.selectedReview?.playback.frameCount ?? 0,
                       held: beforeStop, original: original, exemptions: stopExemptions)
            }
            guard let selectedSource = model.selectedReview?.playback, selectedSource.takeID == takeID,
                  selectedSource.assetRoot.appendingPathComponent(selectedSource.relativePath).path == original else {
                throw benchmarkError("Sealed review does not bind its captured source")
            }
            capturedSources.append(.selected(selectedSource, kind: "captured_take"))
            checkpoint("cycle-\(cycle)-review-ready")
            observeLayout()

            // Initial review's duration uses the same frame projection as the real timer.
            // Retain the pre-issue live snapshot independently; Stop may drain more frames.
            let sealedFrames = selectedSource.frameCount
            let finalWaveformFrames = model.waveform.snapshot?.writtenFrames ?? -1
            let reviewing = model.selectedReview != nil && !model.state.showsStop
            let finalTimerFrames = RecordingMediaTimer.durationFrames(reviewing: reviewing,
                writtenFrames: finalWaveformFrames, retainedRange: model.review.trimRange)
            let reachedConfiguredFrames = RecordingBenchmarkCaptureBoundary.reachedConfiguredFrames(
                expected: expectedFrames, sealed: sealedFrames)
            let seal = model.state.take.object?["seal"] ?? .null
            let independentSealFrames = seal.object?["audio"]?.object?["frames"]?.integer ?? -1
            let terminalReason = seal.object?["continuity"]?.object?["stop_reason"]?.string
            let exact = reviewing && independentSealFrames > 0 && finalTimerFrames == independentSealFrames
                && finalWaveformFrames == independentSealFrames && sealedFrames == independentSealFrames
                && model.review.trimRange == 0..<independentSealFrames
            captureBoundary["seal"] = seal
            captureBoundary["reservation_sha256"] = seal.object?["reservation_sha256"] ?? .null
            captureBoundary["sealed_frames"] = .integer(independentSealFrames)
            captureBoundary["final_waveform_frames"] = .integer(finalWaveformFrames)
            captureBoundary["terminal_reason"] = terminalReason.map(RecordingJSON.string) ?? .null
            // Outside every latency window: the retained record shares an identical catalog.
            captureBoundary["reservation"] = .object(reservationInterner.intern(reservation))
            captureBoundaries[captureBoundaryIndex] = .object(captureBoundary)
            agreements.append(.object([
                "cycle": .integer(Int64(cycle)),
                "timer_frames": .integer(timerFrames),
                "timer_frames_before_issue": .integer(timerFrames),
                "final_timer_frames": .integer(finalTimerFrames),
                "final_timer_mode": .string(reviewing ? "initial-review-retained-duration" : "capture-written-frames"),
                "final_timer_source": .string("RecordingMediaTimer.durationFrames"),
                "final_timer_observed_uptime_ns": .integer(Int64(DispatchTime.now().uptimeNanoseconds)),
                "final_waveform_frames": .integer(finalWaveformFrames),
                "final_waveform_take_id": model.waveform.snapshot.map { .string($0.takeID) } ?? .null,
                "final_waveform_generation": model.waveform.snapshot.map { .string($0.processGeneration) } ?? .null,
                "selected_source_frames": .integer(sealedFrames),
                "selected_take_id": .string(selectedSource.takeID),
                "selected_source_sha256": .string(selectedSource.sourceSHA256),
                "selected_source_path": .string(selectedSource.relativePath),
                "selected_range_start_frame": .integer(model.review.trimRange.lowerBound),
                "selected_range_end_frame": .integer(model.review.trimRange.upperBound),
                "sealed_frames": .integer(independentSealFrames),
                "exact": .boolean(exact),
                "reached_configured_frames": .boolean(reachedConfiguredFrames),
                "reached_exact_configured_frames": .boolean(reachedConfiguredFrames),
                "expected_frames": .integer(expectedFrames),
                "capture_peak_footprint": capturePeak.json,
            ]))

            guard RecordingBenchmarkCaptureBoundary.completionSucceeded(policy: completionPolicy,
                expected: expectedFrames, maximum: reservation["maximum_frames"]?.integer ?? -1,
                sealed: independentSealFrames, terminalReason: terminalReason,
                timedOut: captureWaitTimedOut, stop: stopAttempt) else {
                throw benchmarkError("Capture completion violates \(completionPolicy.rawValue): sealed \(independentSealFrames), configured \(expectedFrames), wait \(waitExit), Stop \(stopAttempt.reason)")
            }
            guard exact else { throw benchmarkError("Initial review timer, waveform, source range and sealed frames disagree") }

            // Drive the actual Save button's intent in the same actor turn as the call.
            // Plain Save remains a separate diagnostic; it cannot satisfy Save & Next.
            let beforeSave = heldWAVPaths()
            checkpoint("cycle-\(cycle)-before-save")
            let saveIssued = await issuedUptime()
            guard model.canSave, let savedPromptID = model.state.prompt?.id else {
                throw benchmarkError("Cycle Save is unavailable")
            }
            let advances = model.saveAdvances
            let completenessAtSave = model.state.historyCompleteness
            let saveExemptions = selectedExemptions()
            if advances { saveNextEligible += 1 } else { plainSaveEligible += 1 }
            model.save()
            guard model.state.phase == .saving, model.state.activeOperationID != nil else {
                throw benchmarkError("The actual Save button did not dispatch its selected intent")
            }
            if advances { saveNextDispatched += 1 } else { plainSaveDispatched += 1 }
            try await observer.wait { model.state.phase == .ready && !model.state.navigationPending }
            let acknowledged = DispatchTime.now().uptimeNanoseconds
            if advances {
                guard model.state.takeID != takeID, model.state.prompt?.id != savedPromptID else {
                    throw benchmarkError("Save and Next did not select its resolved next prompt")
                }
                saveNextAcknowledged += 1
            } else {
                guard model.state.takeID == takeID, model.state.prompt?.id == savedPromptID,
                      model.state.decision == "kept" else {
                    throw benchmarkError("Plain Save did not retain its exact kept take and prompt")
                }
                plainSaveAcknowledged += 1
            }
            let saveMetric = advances ? "save_next_durable_acknowledgment" : "plain_save_durable_acknowledgment"
            try sample(saveMetric, saveIssued, acknowledged,
                   boundary: advances ? "actual Save and Next call to durable acknowledgment and resolved next prompt" :
                       "actual plain Save call to durable kept acknowledgment with unchanged prompt and take",
                   cycle: cycle, frames: sealedFrames, held: beforeSave, original: original,
                   exemptions: saveExemptions,
                   advanceIntent: advances, completenessAtClick: completenessAtSave)
            checkpoint("cycle-\(cycle)-save-acknowledged")
            observeLayout()
            completedCycles += 1
            // A stalled run must name the cycle it stalled in rather than time out silently.
            print(String(decoding: try JSONEncoder().encode(RecordingJSON.object([
                "event": .string("cycle_completed"),
                "cycle": .integer(Int64(cycle)),
                "of": .integer(Int64(totalCycles)),
                "written_frames": .integer(timerFrames),
                "reached_configured_frames": .boolean(reachedConfiguredFrames),
                "client_footprint_bytes": .integer(sampler.peak.client),
            ])), as: UTF8.self))
            fflush(stdout)
        }
        observingLayout = true

        // Metadata snapshot and next-missing lookup. Repeated enough times that the row's
        // p95 is an empirical quantile rather than a single observation read as a tail.
        for round in 0..<max(20, cycles) {
            var beforeSnapshot: Set<String> = []
            var snapshotExemptions: [RecordingBenchmarkSourceExemption] = []
            let measured = try await RecordingBenchmarkMetadataProbe.measure(
                round: round, refreshInFlight: { model.historyRefreshInFlight },
                now: { DispatchTime.now().uptimeNanoseconds }, issuedUptime: { await issuedUptime() },
                waitForIdle: { deadline in
                    let now = DispatchTime.now().uptimeNanoseconds
                    guard now < deadline else { return }
                    let remaining = Int((deadline - now + 999_999_999) / 1_000_000_000)
                    let idleObserver = ReviewObserver(model, barrierSeconds: remaining)
                    try await idleObserver.wait { !model.historyRefreshInFlight }
                },
                beforeIssue: { beforeSnapshot = heldWAVPaths(); snapshotExemptions = selectedExemptions() },
                refresh: { await model.refreshHistory(diagnosticContext: $0) },
                events: { diagnostics.events(traceID: $0) },
                retainAvailability: { metadataAvailability.append($0) })
            try sample("metadata_snapshot_at_1000", measured.issued, measured.applied,
                   boundary: "history refresh request to an applied indexed projection",
                   cycle: round, frames: Int64(model.state.history.count), held: beforeSnapshot,
                   exemptions: snapshotExemptions, requestID: measured.requestID,
                   diagnosticTraceID: measured.traceID)
        }
        for round in 0..<max(20, cycles) {
            var beforeLookup: Set<String> = []
            var lookupExemptions: [RecordingBenchmarkSourceExemption] = []
            let measured = try await RecordingBenchmarkNavigationProbe.measure(
                round: round,
                state: { RecordingBenchmarkNavigationState(
                    canContinueMissing: model.canContinueMissing,
                    completeness: model.state.historyCompleteness,
                    historyRevision: model.state.historyRevision,
                    navigationPending: model.state.navigationPending,
                    activeOperationID: model.state.activeOperationID) },
                now: { DispatchTime.now().uptimeNanoseconds },
                issuedUptime: { await issuedUptime() },
                waitForAvailability: { deadline in
                    let now = DispatchTime.now().uptimeNanoseconds
                    guard now < deadline else { return }
                    let remaining = Int((deadline - now + 999_999_999) / 1_000_000_000)
                    let availabilityObserver = ReviewObserver(model, barrierSeconds: remaining)
                    try await availabilityObserver.wait {
                        model.canContinueMissing && model.state.historyCompleteness == "complete"
                    }
                },
                beforeIssue: { beforeLookup = heldWAVPaths() },
                dispatch: { lookupExemptions = selectedExemptions(); model.continueMissing() },
                waitForAcknowledgment: { operation in
                    try await observer.wait {
                        !model.state.navigationPending || model.state.activeOperationID != operation
                    }
                },
                retainAvailability: { navigationAvailability.append($0) })
            try sample("next_missing_lookup", measured.issued, measured.acknowledged,
                   boundary: "actual continue-missing dispatch to its acknowledged resolved selection",
                   cycle: round, frames: Int64(model.state.history.count), held: beforeLookup,
                   exemptions: lookupExemptions,
                   navigationOperationID: measured.operationID)
        }
        observeLayout()
        } catch {
            failure = "\(error)"
            checkpoint("command-failure")
        }
        sampler.stop()
        let diagnosticSnapshot = diagnostics.snapshot()
        var diagnosticFailure: String?
        do { try diagnosticSnapshot.requireInspectionEvidence(for: capturedSources) }
        catch { diagnosticFailure = String(describing: error); if failure == nil { failure = diagnosticFailure } }

        let report: RecordingJSON = .object([
            "failure": failure.map(RecordingJSON.string) ?? .null,
            "diagnostic_failure": diagnosticFailure.map(RecordingJSON.string) ?? .null,
            "checkpoints": .array(checkpoints),
            "final_checkpoint": ReviewObserver.checkpoint(model, label: "report"),
            "transitions": .array(model.transitions.map { transition in .object([
                "uptime_ns": .integer(Int64(transition.monotonicNanoseconds)), "phase": .string(transition.phase),
                "take_id": transition.takeID.map(RecordingJSON.string) ?? .null,
                "operation_id": transition.operationID.map(RecordingJSON.string) ?? .null,
                "status": .string(transition.status)]) }),
            "suite": .string("benchmark"),
            "capture_evidence_version": .string(RecordingBenchmarkCaptureBoundary.evidenceVersion),
            "completion_policy": .string(completionPolicy.rawValue),
            "recorder_build": config.recorder.expectedPeerBuild,
            "cycles": .integer(Int64(max(1, cycles))),
            "save_action_counts": .object([
                "planned_cycles": .integer(Int64(totalCycles)), "completed_cycles": .integer(Int64(completedCycles)),
                "save_next_eligible": .integer(Int64(saveNextEligible)), "plain_save_eligible": .integer(Int64(plainSaveEligible)),
                "save_next_dispatched": .integer(Int64(saveNextDispatched)), "plain_save_dispatched": .integer(Int64(plainSaveDispatched)),
                "save_next_acknowledged": .integer(Int64(saveNextAcknowledged)), "plain_save_acknowledged": .integer(Int64(plainSaveAcknowledged)),
                "save_cycles_not_reached": .integer(Int64(totalCycles - saveNextEligible - plainSaveEligible)),
                "save_next_eligible_unacknowledged": .integer(Int64(saveNextEligible - saveNextAcknowledged)),
                "save_next_not_eligible_cycles": .integer(Int64(plainSaveEligible)),
            ]),
            "clock": .string("mach_uptime_ns"),
            "samples": .array(samples),
            "navigation_availability": .array(navigationAvailability),
            "metadata_availability": .array(metadataAvailability),
            "diagnostics": try diagnosticSnapshot.json(),
            "captured_sources": try recordingBenchmarkJSON(capturedSources),
            "timer_frame_agreement": .array(agreements),
            "capture_boundaries": .array(captureBoundaries),
            "warm_idle_footprint": warmIdle.json,
            "peak_footprint": sampler.peak.json,
            "footprint_sample_count": .integer(Int64(sampler.samples)),
            "layout_max_drift_points": .number(drift.isFinite ? drift : 0),
            "layout_missing_regions": .array(missingRegions.sorted().map(RecordingJSON.string)),
            "layout_measurement_count": .integer(Int64(layoutMeasurements)),
            "client_held_wav_paths": .array(clientHeldPaths.sorted().map(RecordingJSON.string)),
            "history_count": .integer(Int64(model.state.history.count)),
            "history_completeness": .string(model.state.historyCompleteness),
            "fixed_geometry": .boolean(true),
            "microphone_opened": .boolean(false),
            "window_shown": .boolean(false),
            "installed_app_restarted": .boolean(false),
        ])
        try JSONEncoder().encode(report)
            .write(to: directory.appendingPathComponent("benchmark-report.json"), options: .atomic)
        print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
        if let failure { throw benchmarkError(failure) }
    }

    /// Read the near end off the main actor. The resume is then queued on the main actor
    /// exactly as an arriving click or key event would be, so a busy main actor counts
    /// against the command's own latency instead of being invisible to a caller that is
    /// already running there. Every command row below is issued through this.
    nonisolated static func issuedUptime() async -> UInt64 {
        await Task.detached { DispatchTime.now().uptimeNanoseconds }.value
    }

    private static func benchmarkError(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.BENCHMARK_HARNESS", message: message)
    }
}
