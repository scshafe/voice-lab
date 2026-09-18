import Foundation
import RecordingContracts
@testable import RecordingCore
@testable import RecordingUI
import Testing

/// Pure-model coverage for the library-vault line: every reply and event is a real wire shape
/// (checked against the committed response schema) delivered through a scripted request or the
/// service stream handler. No process is created, no audio is opened and no vault is reached.
/// Mirrors tests/test_library_replication_service.py.
@Suite(.serialized)
struct RecordingVaultTests {
    static let supervisorFailure = "the library vault supervisor stopped; copies resume when the app is relaunched"

    private static let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()

    /// Scripted `vault-status` requests: records each one and answers with the current reply.
    @MainActor final class ScriptedVault {
        var operations: [String] = []
        var reply: (String) throws -> RecordingWireEnvelope = {
            try RecordingVaultTests.envelope(RecordingVaultTests.status($0))
        }

        private func answer(_ operation: String) async throws -> RecordingWireEnvelope {
            operations.append(operation)
            return try reply(operation)
        }

        var requests: RecordingVaultRequests {
            RecordingVaultRequests(status: { [self] operation in try await answer(operation) })
        }
    }

    // MARK: wire shapes

    /// A service response that passed the Python-owned response schema, as the channel delivers it.
    static func envelope(_ data: RecordingJSON, error: RecordingJSON = .null,
                         requestID: String = "fixture-request") throws -> RecordingWireEnvelope {
        let body: RecordingJSON = .object(["data": data, "error": error, "meta": .object([
            "protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string(requestID),
            "process_generation": .string("synthetic-generation"), "state_revision": .integer(2),
            "event_sequence": .integer(3), "take_id": .null])])
        return try RecordingWireEnvelope.decode(body.transportData(), schema: Data(contentsOf:
            repository.appendingPathComponent("schemas/recording-response.schema.json")))
    }

    static func failure(_ code: String, _ message: String, retryable: Bool = false) -> RecordingJSON {
        .object(["code": .string(code), "message": .string(message), "retryable": .boolean(retryable)])
    }

    static func refusal(_ code: String, _ message: String, retryable: Bool = false) throws -> RecordingWireEnvelope {
        try envelope(.null, error: failure(code, message, retryable: retryable))
    }

    /// A vault up to date at generation 7, with `changes` applied.
    static func status(_ operation: String = "op-vault", _ changes: [String: RecordingJSON] = [:]) -> RecordingJSON {
        .object(["event": .string("vault-status"), "operation_id": .string(operation),
                 "state": .string("up-to-date"), "waiting_files": .integer(0),
                 "unreachable_since": .null, "last_pushed_at": .string("2026-09-17T13:55:00Z"),
                 "generation": .integer(7), "code": .null, "message": .null]
            .merging(changes) { $1 })
    }

    static func off(_ operation: String = "op-vault") -> RecordingJSON {
        status(operation, ["state": .string("off"), "last_pushed_at": .null, "generation": .integer(0)])
    }

    static func waiting(_ operation: String = "op-vault", files: Int64 = 12) -> RecordingJSON {
        status(operation, ["state": .string("waiting"), "waiting_files": .integer(files)])
    }

    static func unreachable(_ operation: String = "op-vault") -> RecordingJSON {
        status(operation, ["state": .string("unreachable"), "unreachable_since": .string("2026-09-17T14:02:00Z"),
                           "code": .string("STORAGE.UNAVAILABLE"), "message": .string("the vault did not answer")])
    }

    // MARK: the lane the app routes by

    @Test @MainActor func theBackgroundRequestIDsAreTheOnesThePythonServiceSends() {
        // Pinned on both sides: tests/test_library_replication_service.py asserts the same two
        // literals against library_replication_service.VAULT_*_REQUEST_ID.
        #expect(RecordingWorkspaceModel.vaultStatusRequestID == "vault-status")
        #expect(RecordingWorkspaceModel.vaultSupervisorRequestID == "vault-supervisor")
    }

    // MARK: the reply

    @Test @MainActor func aStatusReplyBecomesTheLineTheAppShows() async throws {
        let service = ScriptedVault()
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        model.vaultRequests = service.requests
        #expect(!model.vault.known && model.vault.state == nil)

        await model.requestVaultStatus()

        #expect(model.vault.known && model.vault.state == "up-to-date")
        #expect(model.vault.generation == 7 && model.vault.waitingFiles == 0)
        #expect(model.vault.lastPushedAt == "2026-09-17T13:55:00Z")
        #expect(model.vault.unreachableSince == nil && model.vault.code == nil)
        #expect(service.operations.count == 1)
        let operation = try #require(service.operations.first)
        #expect(operation.hasPrefix("vault-status-"))
    }

    @Test @MainActor func everyStateTheServiceCanReportIsCarriedWithItsOwnEvidence() async throws {
        let service = ScriptedVault()
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        model.vaultRequests = service.requests

        service.reply = { try Self.envelope(Self.off($0)) }
        await model.requestVaultStatus()
        #expect(model.vault.state == "off" && model.vault.generation == 0 && model.vault.lastPushedAt == nil)

        service.reply = { try Self.envelope(Self.waiting($0)) }
        await model.requestVaultStatus()
        #expect(model.vault.state == "waiting" && model.vault.waitingFiles == 12)

        service.reply = { try Self.envelope(Self.status($0, ["state": .string("copying")])) }
        await model.requestVaultStatus()
        #expect(model.vault.state == "copying" && model.vault.message == nil)

        service.reply = { try Self.envelope(Self.unreachable($0)) }
        await model.requestVaultStatus()
        #expect(model.vault.state == "unreachable" && model.vault.unreachableSince == "2026-09-17T14:02:00Z")
        #expect(model.vault.code == "STORAGE.UNAVAILABLE" && model.vault.message == "the vault did not answer")

        service.reply = { try Self.envelope(Self.status($0, ["state": .string("failed"), "last_pushed_at": .null,
                                                             "generation": .integer(0),
                                                             "code": .string("STORAGE.NOT_APPROVED"),
                                                             "message": .string("the destination approval expired")])) }
        await model.requestVaultStatus()
        #expect(model.vault.state == "failed" && model.vault.code == "STORAGE.NOT_APPROVED")
        #expect(model.vault.message == "the destination approval expired")
        // A failure clears the earlier outage: the fields always describe one status.
        #expect(model.vault.unreachableSince == nil)
    }

    @Test @MainActor func aServiceThatCannotAnswerLeavesTheLineUnknownButSaysWhy() async throws {
        let service = ScriptedVault()
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        model.vaultRequests = service.requests
        service.reply = { _ in try Self.refusal("RECORDING.INVALID_MESSAGE", "this service does not know vault-status") }

        await model.requestVaultStatus()

        #expect(!model.vault.known)
        #expect(model.vault.message == "this service does not know vault-status")

        // Once a status is known, a later refusal does not overwrite it.
        service.reply = { try Self.envelope(Self.waiting($0)) }
        await model.requestVaultStatus()
        service.reply = { _ in try Self.refusal("RECORDING.INVALID_MESSAGE", "a later refusal") }
        await model.requestVaultStatus()
        #expect(model.vault.state == "waiting" && model.vault.message == nil)
    }

    @Test @MainActor func noRequestIsMadeWithoutATransportOrOverAClosedChannel() async throws {
        let unconfigured = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        await unconfigured.requestVaultStatus()
        #expect(!unconfigured.vault.known && unconfigured.vault.message == nil)

        let service = ScriptedVault()
        let closed = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        closed.vaultRequests = service.requests
        closed.processChannelFailed = true

        await closed.requestVaultStatus()

        #expect(service.operations.isEmpty && !closed.vault.known)
    }

    // MARK: the background stream

    @Test @MainActor func aBackgroundUpdateAppliesTheSameWayAndApplyingItAgainChangesNothing() async throws {
        let service = ScriptedVault()
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        model.vaultRequests = service.requests
        service.reply = { try Self.envelope(Self.waiting($0)) }
        await model.requestVaultStatus()
        #expect(model.vault.state == "waiting")

        let update = try Self.envelope(Self.status("op-vault-background"),
                                       requestID: RecordingWorkspaceModel.vaultStatusRequestID)
        model.handleService(update)
        let applied = model.vault
        #expect(applied.state == "up-to-date" && applied.generation == 7)

        // The reply and the stream deliver the same event; the second application is inert.
        model.handleService(update)
        #expect(model.vault == applied)
    }

    @Test @MainActor func anEnvelopeThatIsNotAVaultStatusLeavesTheLineAlone() async throws {
        let service = ScriptedVault()
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        model.vaultRequests = service.requests
        service.reply = { try Self.envelope(Self.waiting($0)) }
        await model.requestVaultStatus()
        let applied = model.vault

        // Another background lane, a refused reply's copy, and a shape that is no status at all.
        model.handleService(try Self.envelope(.object(["event": .string("fine-tune-progress"),
                                                       "operation_id": .string("fine-tune-job-one"),
                                                       "state": .string("queued")]),
                                              requestID: "fine-tune-job-one"))
        model.handleService(try Self.refusal("RECORDING.WRITE_FAILED", "a copy of a refused reply"))
        model.applyVaultStatus(.object(["event": .string("vault-status")]))  // no state
        model.applyVaultStatus(.string("vault-status"))
        model.applyVaultStatus(.null)

        #expect(model.vault == applied)
        #expect(model.state.phase == .ready, "recording is unaffected by the vault line")
    }

    // MARK: the supervisor's own lane

    @Test @MainActor func aSupervisorFailureReachesTheAppAndOutlivesLaterStatuses() async throws {
        let service = ScriptedVault()
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        model.vaultRequests = service.requests
        service.reply = { try Self.envelope(Self.waiting($0)) }
        await model.requestVaultStatus()

        // An error-only envelope of another lane belongs to that lane, not to the vault.
        model.handleService(try Self.envelope(.null, error: Self.failure("RECORDING.WRITE_FAILED", "recovery stopped", retryable: true),
                                              requestID: "recovery-supervisor"))
        #expect(model.vault.supervisorFailure == nil)

        model.handleService(try Self.envelope(.null, error: Self.failure("RECORDING.WRITE_FAILED", Self.supervisorFailure, retryable: true),
                                              requestID: RecordingWorkspaceModel.vaultSupervisorRequestID))
        #expect(model.vault.supervisorFailure == Self.supervisorFailure)
        #expect(model.vault.state == "waiting", "the last status it managed to publish still stands")

        // Only a relaunch clears it: a later status, from the stream or a reply, does not.
        model.handleService(try Self.envelope(Self.status("op-vault-later"),
                                              requestID: RecordingWorkspaceModel.vaultStatusRequestID))
        #expect(model.vault.state == "up-to-date" && model.vault.supervisorFailure == Self.supervisorFailure)
        service.reply = { try Self.envelope(Self.off($0)) }
        await model.requestVaultStatus()
        #expect(model.vault.state == "off" && model.vault.supervisorFailure == Self.supervisorFailure)
        #expect(model.state.phase == .ready && model.state.errorCode == nil, "recording is unaffected")
    }

    @Test @MainActor func aSupervisorEnvelopeWithoutAMessageIsIgnored() {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))

        model.applyVaultSupervisorError(.object(["code": .string("RECORDING.WRITE_FAILED")]))
        model.applyVaultSupervisorError(.null)

        #expect(model.vault.supervisorFailure == nil && !model.vault.known)
    }
}
