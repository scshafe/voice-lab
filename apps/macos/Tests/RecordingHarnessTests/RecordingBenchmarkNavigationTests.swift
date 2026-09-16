import Foundation
import RecordingContracts
@testable import RecordingHarness
@testable import RecordingUI
import Testing

@MainActor private final class NavigationFixture {
    var state = RecordingBenchmarkNavigationState(
        canContinueMissing: true, completeness: "complete", historyRevision: 10,
        navigationPending: false, activeOperationID: nil)
    var clock: UInt64 = 1_000
    var issuedCount = 0
    var dispatchCount = 0
    var availabilityWaits = 0
    var availability: [RecordingJSON] = []
    var onIssued: (@MainActor () async -> Void)?
    var onAvailability: (@MainActor (UInt64) async throws -> Void)?
    var onDispatch: (@MainActor () -> Void)?
    var onAcknowledgment: (@MainActor (String) async throws -> Void)?

    func measure() async throws -> RecordingBenchmarkNavigationMeasurement {
        try await RecordingBenchmarkNavigationProbe.measure(
            round: 0, availabilityBudget: 1_000,
            state: { self.state }, now: { self.clock },
            issuedUptime: {
                self.issuedCount += 1
                self.clock += 10
                await self.onIssued?()
                return self.clock
            },
            waitForAvailability: { deadline in
                self.availabilityWaits += 1
                if let action = self.onAvailability { try await action(deadline) }
                else { self.clock = deadline }
            }, beforeIssue: {},
            dispatch: {
                self.dispatchCount += 1
                if let action = self.onDispatch { action() }
                else {
                    self.state.navigationPending = true
                    self.state.activeOperationID = "navigate-\(self.dispatchCount)"
                }
            },
            waitForAcknowledgment: { operation in
                if let action = self.onAcknowledgment { try await action(operation) }
                else {
                    self.clock += 100
                    self.state.navigationPending = false
                    self.state.activeOperationID = nil
                }
            }, retainAvailability: { self.availability.append($0) })
    }
}

@Suite(.serialized)
struct RecordingBenchmarkNavigationTests {
    @Test @MainActor func disabledNavigationWaitsBoundedlyAndNeverDispatches() async throws {
        let fixture = NavigationFixture()
        fixture.state.canContinueMissing = false
        fixture.state.completeness = "building"
        do {
            _ = try await fixture.measure()
            Issue.record("Disabled Continue Missing emitted a command measurement")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.BENCHMARK_AVAILABILITY_TIMEOUT")
        }
        #expect(fixture.dispatchCount == 0)
        #expect(fixture.issuedCount == 0)
        #expect(fixture.availability.count == 1)
        #expect(fixture.availability[0].object?["command_dispatched"] == .boolean(false))
        #expect(fixture.availability[0].object?["duration_ns"] == .integer(1_000))
    }

    @Test @MainActor func availabilityLostDuringIssuedClockAwaitProducesNoPhantomSample() async throws {
        let fixture = NavigationFixture()
        fixture.onIssued = {
            if fixture.issuedCount == 1 {
                await Task.yield()
                fixture.state.canContinueMissing = false
                fixture.state.completeness = "building"
            }
        }
        fixture.onAvailability = { _ in
            fixture.clock += 200
            fixture.state.canContinueMissing = true
            fixture.state.completeness = "complete"
            fixture.state.historyRevision = 11
        }
        let measured = try await fixture.measure()
        #expect(fixture.issuedCount == 2)
        #expect(fixture.dispatchCount == 1)
        #expect(fixture.availabilityWaits == 1)
        #expect(measured.issued == 1_220)
        #expect(measured.acknowledged == 1_320)
        #expect(fixture.availability[0].object?["eligibility_lost_before_dispatch"] == .integer(1))
        #expect(fixture.availability[0].object?["duration_ns"] == .integer(220))
    }

    @Test @MainActor func noOpAndReusedOperationFailTheFreshPendingNavigationGuard() async throws {
        for reusedOperation in [false, true] {
            let fixture = NavigationFixture()
            fixture.state.activeOperationID = "navigate-old"
            fixture.onDispatch = {
                if reusedOperation { fixture.state.navigationPending = true }
            }
            do {
                _ = try await fixture.measure()
                Issue.record("An early return or reused operation emitted navigation latency")
            } catch let error as RecordingContractError {
                #expect(error.code == "RECORDING.BENCHMARK_NAVIGATION_NOT_DISPATCHED")
            }
            #expect(fixture.dispatchCount == 1)
            #expect(fixture.availability[0].object?["command_dispatched"] == .boolean(false))
        }
    }

    @Test @MainActor func wholeHeldRequestThroughAcknowledgmentStaysInsideTheWindow() async throws {
        let fixture = NavigationFixture()
        var release: CheckedContinuation<Void, Never>?
        fixture.onAcknowledgment = { _ in
            await withCheckedContinuation { release = $0 }
            fixture.state.navigationPending = false
            fixture.state.activeOperationID = nil
        }
        let measuring = Task { @MainActor in try await fixture.measure() }
        for _ in 0..<20 {
            if release != nil { break }
            await Task.yield()
        }
        let held = try #require(release)
        #expect(fixture.state.navigationPending)
        #expect(fixture.availability.isEmpty)
        fixture.clock += 500
        held.resume()
        let measured = try await measuring.value
        #expect(measured.issued == 1_010)
        #expect(measured.acknowledged == 1_510)
        #expect(fixture.availability[0].object?["duration_ns"] == .integer(10))
    }

    @Test @MainActor func dispatchedHistoryUnknownKeepsPartialEvidenceAndIsNotRetried() async throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        var initial = model.state
        initial.contextReady = true
        initial.historyCompleteness = "complete"
        model.publish(initial)
        let observer = ReviewObserver(model, barrierSeconds: 1)
        var records: [RecordingJSON] = []
        var dispatches = 0
        var clock: UInt64 = 100
        do {
            _ = try await RecordingBenchmarkNavigationProbe.measure(
                round: 0, availabilityBudget: 1_000,
                state: { RecordingBenchmarkNavigationState(
                    canContinueMissing: model.canContinueMissing,
                    completeness: model.state.historyCompleteness,
                    historyRevision: model.state.historyRevision,
                    navigationPending: model.state.navigationPending,
                    activeOperationID: model.state.activeOperationID) },
                now: { clock }, issuedUptime: { clock += 10; return clock },
                waitForAvailability: { _ in Issue.record("A dispatched failure retried availability") },
                beforeIssue: {}, dispatch: {
                    dispatches += 1
                    var state = model.state
                    state.navigationPending = true
                    state.activeOperationID = "navigate-dispatched"
                    model.publish(state)
                }, waitForAcknowledgment: { _ in
                    clock += 700
                    var state = model.state
                    state.navigationPending = false
                    state.historyCompleteness = "building"
                    state.errorCode = "RECORDING.HISTORY_UNKNOWN"
                    state.status = "History became incomplete during the actual command"
                    model.publish(state)
                    try await observer.wait { !model.state.navigationPending }
                }, retainAvailability: { records.append($0) })
            Issue.record("The dispatched HISTORY_UNKNOWN became a passing sample")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.HISTORY_UNKNOWN")
        }
        #expect(dispatches == 1)
        let retained = try RecordingJSON.parse(RecordingJSON.array(records).transportData())
        let failed = try #require(retained.array?.first?.object)
        #expect(failed["command_dispatched"] == .boolean(true))
        #expect(failed["operation_id"] == .string("navigate-dispatched"))
        #expect(failed["outcome"] == .string("dispatched-failed"))
        #expect(failed["request_started_uptime_ns"] == .integer(110))
        #expect(failed["request_finished_uptime_ns"] == .integer(810))
        #expect(model.state.errorCode == "RECORDING.HISTORY_UNKNOWN")
    }
}
