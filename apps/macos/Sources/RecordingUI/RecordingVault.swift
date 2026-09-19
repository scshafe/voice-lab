import Foundation
import RecordingContracts
import RecordingCore

/// Where the library vault stands, as the service last said. It lives beside the recording
/// state, never in it: no vault event changes the selection, the take, Save or navigation.
public struct RecordingVaultStatus: Equatable, Sendable {
    /// off, up-to-date, waiting, copying, unreachable or failed; nil until the service answers.
    public var state: String?
    public var waitingFiles: Int64 = 0
    /// When the vault first failed to answer (ISO-8601 with an explicit zone).
    public var unreachableSince: String?
    public var lastPushedAt: String?
    public var generation: Int64 = 0
    /// The STORAGE code behind an unreachable or failed vault.
    public var code: String?
    /// What is wrong, in the service's words.
    public var message: String?
    /// The service's error envelope when it stopped supervising copies.
    public var supervisorFailure: String?

    public init() {}

    /// Whether the app has heard anything about the vault yet.
    public var known: Bool { state != nil }

    /// The sidebar's one-line status. The CLI's `_vault_status_line` uses the same words.
    public var statusLine: String { statusLine(timeZone: .current) }

    /// Format an outage in local 24-hour time; a timezone seam keeps the wording testable.
    public func statusLine(timeZone: TimeZone) -> String {
        if let supervisorFailure { return "Vault · \(supervisorFailure)" }
        switch state {
        case "off": return "Vault · off"
        case "up-to-date": return "Vault · up to date"
        case "copying": return "Vault · copying"
        case "waiting":
            if waitingFiles == 0 { return "Vault · waiting to copy" }
            return "Vault · \(waitingFiles) \(waitingFiles == 1 ? "file" : "files") waiting"
        case "unreachable":
            let parser = ISO8601DateFormatter()
            parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            var date = unreachableSince.flatMap { parser.date(from: $0) }
            if date == nil {
                parser.formatOptions = [.withInternetDateTime]
                date = unreachableSince.flatMap { parser.date(from: $0) }
            }
            guard let date else { return "Vault unreachable" }
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = timeZone
            formatter.dateFormat = "HH:mm"
            return "Vault unreachable since \(formatter.string(from: date))"
        case "failed": return "Vault · \(message ?? "copying stopped")"
        default: return message.map { "Vault · \($0)" } ?? "Vault · checking"
        }
    }

    /// The service's `VaultStatusEvent`, already validated on the wire; nil for any other shape.
    public init?(_ json: RecordingJSON) {
        guard let object = json.object, object["event"]?.string == "vault-status",
              let state = object["state"]?.string else { return nil }
        self.state = state
        self.waitingFiles = object["waiting_files"]?.integer ?? 0
        self.unreachableSince = object["unreachable_since"]?.string
        self.lastPushedAt = object["last_pushed_at"]?.string
        self.generation = object["generation"]?.integer ?? 0
        self.code = object["code"]?.string
        self.message = object["message"]?.string
    }
}

/// The vault's transport seam, so a fixture can answer without a service (mirrors
/// `RecordingFineTuneRequests`).
public struct RecordingVaultRequests: Sendable {
    public var status: @Sendable (String) async throws -> RecordingWireEnvelope

    public init(status: @escaping @Sendable (String) async throws -> RecordingWireEnvelope) {
        self.status = status
    }
}

/// Background library copies, as the workspace sees them: one request after the service's
/// handshake, then unsolicited updates whenever the state changes.
@MainActor
extension RecordingWorkspaceModel {
    /// The request id of the service's error envelope when vault supervision stops.
    public static let vaultSupervisorRequestID = "vault-supervisor"
    /// The request id of every background vault update.
    public static let vaultStatusRequestID = "vault-status"

    func vaultTransport() -> RecordingVaultRequests? {
        if let vaultRequests { return vaultRequests }
        guard let service else { return nil }
        return RecordingVaultRequests(status: { try await service.vaultStatus(operationID: $0) })
    }

    /// Ask the service where the vault stands without waiting for it: after the service's
    /// handshake, and after an audio reconnect reopens a context.
    func scheduleVaultStatus() {
        vaultStatusTask = Task { [weak self] in await self?.requestVaultStatus() }
    }

    /// Learn the vault's state. A service that copies nowhere answers `off`, which is not a
    /// failure; a service too old to know the command leaves the line unknown.
    public func requestVaultStatus() async {
        guard !processChannelFailed, let requests = vaultTransport() else { return }
        do {
            let response = try result(await requests.status(newOperation("vault-status")))
            applyVaultStatus(response.data)
        } catch {
            if !vault.known { vault.message = errorMessage(error) }
        }
    }

    /// Apply a vault status from a reply or the service stream; each arrives twice, and
    /// applying one again changes nothing.
    public func applyVaultStatus(_ event: RecordingJSON) {
        guard var applied = RecordingVaultStatus(event) else { return }
        applied.supervisorFailure = vault.supervisorFailure
        vault = applied
    }

    /// The service's error envelope when it stopped copying; no request awaits it.
    func applyVaultSupervisorError(_ error: RecordingJSON) {
        guard let message = error.object?["message"]?.string else { return }
        vault.supervisorFailure = message
    }
}
