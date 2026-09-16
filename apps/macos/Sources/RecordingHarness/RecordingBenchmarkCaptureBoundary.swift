import Foundation
import RecordingContracts

enum RecordingBenchmarkCompletionPolicy: String, Sendable {
    case exactFrames = "exact-frames"
    case maximumDurationLimit = "maximum-duration-limit"
}

struct RecordingBenchmarkCaptureState: Sendable {
    let takeID: String?
    let generation: String
    let phase: String
    let canStop: Bool
    let writtenFrames: Int64

    var json: RecordingJSON {
        .object(["take_id": takeID.map(RecordingJSON.string) ?? .null,
            "generation": .string(generation), "phase": .string(phase),
            "can_stop": .boolean(canStop), "written_frames": .integer(writtenFrames)])
    }
}

struct RecordingBenchmarkStopAttempt: Sendable {
    let issued: UInt64?
    let before: RecordingBenchmarkCaptureState
    let atIssue: RecordingBenchmarkCaptureState?
    let afterCall: RecordingBenchmarkCaptureState?
    let modelStopCalled: Bool
    let modelStopAccepted: Bool
    let reason: String

    var json: RecordingJSON {
        .object(["issued_uptime_ns": issued.map { .integer(Int64($0)) } ?? .null,
            "before": before.json, "at_issue": atIssue?.json ?? .null,
            "after_call": afterCall?.json ?? .null,
            "model_stop_called": .boolean(modelStopCalled),
            "model_stop_accepted": .boolean(modelStopAccepted),
            "reason": .string(reason),
            // Local state transition is not a new wire/controller acknowledgment oracle.
            "controller_acknowledgment_proven": .boolean(false)])
    }
}

/// Harness-only retention. Every boundary keeps its whole reservation, so the independent
/// reservation_sha256 check still runs, but a group (or failing that, its catalog) exactly
/// identical to the last retained one shares that stored copy. The harness is the measured
/// client, so a parsed 1,102-prompt catalog kept per cycle would count as client memory.
struct RecordingBenchmarkReservationInterner {
    private var group: RecordingJSON?
    private var catalog: RecordingJSON?

    mutating func intern(_ reservation: [String: RecordingJSON]) -> [String: RecordingJSON] {
        guard case .object(var candidate)? = reservation["group"] else { return reservation }
        var interned = reservation
        if let group, Self.identical(group, .object(candidate)) {
            interned["group"] = group
            return interned
        }
        if let catalog, let current = candidate["catalog"], Self.identical(catalog, current) {
            candidate["catalog"] = catalog
        }
        let retained = RecordingJSON.object(candidate)
        interned["group"] = retained
        (group, catalog) = (retained, candidate["catalog"])
        return interned
    }

    /// Byte-exact JSON identity. RecordingJSON's == and dictionary lookup treat canonically
    /// equivalent Unicode as equal and -0.0 as 0.0; shared storage must never change a byte.
    static func identical(_ lhs: RecordingJSON, _ rhs: RecordingJSON) -> Bool {
        switch (lhs, rhs) {
        case let (.object(left), .object(right)):
            guard left.count == right.count else { return false }
            return left.allSatisfy { key, value in
                // Lookup may find an equivalent key; its stored UTF-8 bytes must also match.
                guard let index = right.index(forKey: key) else { return false }
                return right[index].key.utf8.elementsEqual(key.utf8) && identical(value, right[index].value)
            }
        case let (.array(left), .array(right)):
            return left.count == right.count && zip(left, right).allSatisfy { identical($0, $1) }
        case let (.string(left), .string(right)): return left.utf8.elementsEqual(right.utf8)
        case let (.integer(left), .integer(right)): return left == right
        case let (.number(left), .number(right)): return left.bitPattern == right.bitPattern
        case let (.boolean(left), .boolean(right)): return left == right
        case (.null, .null): return true
        default: return false
        }
    }
}

enum RecordingBenchmarkCaptureBoundary {
    static let evidenceVersion = "cole-voice-lab.recording-benchmark-capture/v2"

    static func reachedConfiguredFrames(expected: Int64, sealed: Int64) -> Bool {
        expected > 0 && sealed == expected
    }

    static func waitExit(expected: Int64, observed: Int64, timedOut: Bool) -> String {
        if timedOut { return "timeout" }
        return observed >= expected ? "frame_target_observed" : "terminal_before_target"
    }

    static func automaticLimitObservation(_ state: RecordingBenchmarkCaptureState) -> RecordingBenchmarkStopAttempt {
        RecordingBenchmarkStopAttempt(issued: nil, before: state, atIssue: nil, afterCall: nil,
            modelStopCalled: false, modelStopAccepted: false, reason: "maximum_duration_policy_no_stop")
    }

    /// A maximum-duration design point measures the unchanged hard limit, not a promised PCM length.
    static func completionSucceeded(policy: RecordingBenchmarkCompletionPolicy, expected: Int64,
                                    maximum: Int64, sealed: Int64, terminalReason: String?,
                                    timedOut: Bool, stop: RecordingBenchmarkStopAttempt) -> Bool {
        guard !timedOut, sealed > 0, sealed <= maximum else { return false }
        switch policy {
        case .exactFrames:
            return reachedConfiguredFrames(expected: expected, sealed: sealed) && stop.modelStopAccepted
        case .maximumDurationLimit:
            return expected == 28_800_000 && maximum == expected && terminalReason == "maximum-duration"
                && stop.issued == nil && !stop.modelStopCalled && !stop.modelStopAccepted
                && stop.reason == "maximum_duration_policy_no_stop"
        }
    }

    /// Excludes terminal/no-op calls from Stop windows without changing capture policy.
    @MainActor static func issueStop(
        takeID: String, generation: String,
        state: () -> RecordingBenchmarkCaptureState,
        issuedUptime: () async -> UInt64,
        dispatch: () -> Void
    ) async -> RecordingBenchmarkStopAttempt {
        let before = state()
        func matches(_ value: RecordingBenchmarkCaptureState) -> Bool {
            value.takeID == takeID && value.generation == generation
        }
        guard matches(before), before.canStop else {
            return RecordingBenchmarkStopAttempt(issued: nil, before: before,
                atIssue: nil, afterCall: nil, modelStopCalled: false, modelStopAccepted: false,
                reason: matches(before) ? "terminal_before_issue" : "source_changed_before_issue")
        }
        let issued = await issuedUptime()
        let atIssue = state()
        guard matches(atIssue), atIssue.canStop else {
            return RecordingBenchmarkStopAttempt(issued: issued, before: before,
                atIssue: atIssue, afterCall: nil, modelStopCalled: false, modelStopAccepted: false,
                reason: matches(atIssue) ? "terminal_during_clock_await" : "source_changed_during_clock_await")
        }
        dispatch()
        let afterCall = state()
        let accepted = matches(afterCall) && !afterCall.canStop && afterCall.phase == "stopping"
        return RecordingBenchmarkStopAttempt(issued: issued, before: before,
            atIssue: atIssue, afterCall: afterCall, modelStopCalled: true, modelStopAccepted: accepted,
            reason: accepted ? "model_stop_accepted" : "model_stop_did_not_transition")
    }
}
