import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingHarness
import Testing

@MainActor private final class MetadataFixture {
    var clock: UInt64 = 1_000
    var inFlight = false
    var clockCalls = 0
    var idleWaits = 0
    var dispatches = 0
    var evidence: [RecordingDiagnosticEvent] = []
    var availability: [RecordingJSON] = []
    var onClock: (@MainActor () -> Void)?
    var onIdle: (@MainActor (UInt64) -> Void)?
    var changeEvidence: (@MainActor ([RecordingDiagnosticEvent]) -> [RecordingDiagnosticEvent])?

    func measure() async throws -> RecordingBenchmarkMetadataMeasurement {
        try await RecordingBenchmarkMetadataProbe.measure(
            round: 0, availabilityBudget: 1_000,
            refreshInFlight: { self.inFlight }, now: { self.clock },
            issuedUptime: {
                self.clockCalls += 1; self.clock += 10
                self.onClock?()
                return self.clock
            },
            waitForIdle: { deadline in
                self.idleWaits += 1
                if let action = self.onIdle { action(deadline) }
                else { self.inFlight = false; self.clock += 100 }
            }, beforeIssue: {},
            refresh: { context in
                self.dispatches += 1
                let requestID = "actual-auto-id"
                for (offset, name) in ["history.refresh", "service.request.history", "history.application"].enumerated() {
                    self.evidence.append(RecordingDiagnosticEvent(spanID: name, name: name,
                        phase: .begin, uptimeNanoseconds: self.clock + UInt64(offset), context: context))
                    self.evidence.append(RecordingDiagnosticEvent(spanID: name, name: name,
                        phase: .end, uptimeNanoseconds: self.clock + UInt64(offset == 0 ? 20 : 10 + offset),
                        context: context.request(requestID), outcome: .success))
                }
                self.clock += 100
                if let change = self.changeEvidence { self.evidence = change(self.evidence) }
            }, events: { trace in self.evidence.filter { $0.context.traceID == trace } },
            retainAvailability: { self.availability.append($0) })
    }
}

@Suite(.serialized)
struct RecordingBenchmarkMetadataTests {
    @Test @MainActor func actualFastResponseIsMeasuredWithoutATransientBooleanOrSleep() async throws {
        let fixture = MetadataFixture()
        let measured = try await fixture.measure()
        #expect(measured.issued == 1_010)
        #expect(measured.applied == 1_110)
        #expect(measured.requestID == "actual-auto-id")
        #expect(fixture.dispatches == 1)
        #expect(fixture.idleWaits == 0)
        #expect(fixture.availability[0].object?["acknowledged"] == .boolean(true))
    }

    @Test @MainActor func pendingRefreshAndReadinessLostAtClockAreWaitedOutsideCommand() async throws {
        let fixture = MetadataFixture()
        fixture.inFlight = true
        fixture.onClock = { if fixture.clockCalls == 1 { fixture.inFlight = true } }
        let measured = try await fixture.measure()
        #expect(fixture.idleWaits == 2)
        #expect(fixture.clockCalls == 2)
        #expect(fixture.dispatches == 1)
        #expect(measured.applied - measured.issued == 100)
        #expect(fixture.availability[0].object?["readiness_lost_before_dispatch"] == .integer(1))
    }

    @Test @MainActor func indefinitelyPendingRefreshHasNoCommandSample() async throws {
        let fixture = MetadataFixture()
        fixture.inFlight = true
        fixture.onIdle = { fixture.clock = $0 }
        do {
            _ = try await fixture.measure()
            Issue.record("Pending history emitted a fast metadata measurement")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.BENCHMARK_AVAILABILITY_TIMEOUT")
        }
        #expect(fixture.dispatches == 0)
        #expect(fixture.availability[0].object?["refresh_invoked"] == .boolean(false))
    }

    @Test @MainActor func missingRequestApplicationOrBeginCannotSupplyAZeroDurationSample() async {
        for removed in ["service.request.history", "history.application", "history.refresh", "begin"] {
            let fixture = MetadataFixture()
            fixture.changeEvidence = { events in
                events.filter { removed == "begin" ? $0.phase != .begin : $0.name != removed }
            }
            await #expect(throws: RecordingContractError.self) { try await fixture.measure() }
            #expect(fixture.dispatches == 1)
        }
    }

    @Test @MainActor func coalescedSupersededMismatchedOrDuplicateResponsesAreRejected() async {
        for mutation in ["coalesced", "superseded", "different-request", "duplicate", "outside-window"] {
            let fixture = MetadataFixture()
            fixture.changeEvidence = { events in
                if mutation == "duplicate" { return events + events.filter { $0.name == "service.request.history" } }
                return events.map { event in
                    guard event.phase == .end,
                          event.name == (mutation == "coalesced" ? "history.refresh" : "history.application") else { return event }
                    return RecordingDiagnosticEvent(spanID: event.spanID, name: event.name, phase: event.phase,
                        uptimeNanoseconds: mutation == "outside-window" ? 1 : event.uptimeNanoseconds,
                        context: mutation == "different-request" ? event.context.request("unrelated") : event.context,
                        outcome: mutation == "coalesced" ? .coalesced : mutation == "superseded" ? .superseded : event.outcome)
                }
            }
            await #expect(throws: RecordingContractError.self) { try await fixture.measure() }
            #expect(fixture.dispatches == 1) // Dispatched failure is never retried to find a faster sample.
        }
    }
}
