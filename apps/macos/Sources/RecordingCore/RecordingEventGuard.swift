import RecordingContracts

/// Reliable sequence/revision checks; a gap requires an authoritative snapshot.
public struct RecordingEventGuard: Sendable {
    public enum Disposition: Equatable, Sendable { case apply, duplicate, stale, snapshotRequired }
    private let generation: String
    private var sequence: Int64?
    private var revision: Int64 = 0

    public init(generation: String) { self.generation = generation }

    public mutating func receive(_ meta: RecordingMessageMeta, snapshot: Bool) throws -> Disposition {
        guard meta.process_generation == generation else {
            throw RecordingContractError(code: "RECORDING.STALE_GENERATION", message: "Peer event belongs to another process generation")
        }
        if snapshot {
            guard meta.state_revision >= revision, sequence.map({ meta.event_sequence >= $0 }) ?? true else { return .stale }
            sequence = meta.event_sequence
            revision = meta.state_revision
            return .apply
        }
        if let sequence {
            if meta.event_sequence == sequence { return .duplicate }
            if meta.event_sequence < sequence { return .stale }
            guard meta.event_sequence == sequence + 1 else { return .snapshotRequired }
        }
        guard meta.state_revision >= revision else { return .stale }
        sequence = meta.event_sequence
        revision = meta.state_revision
        return .apply
    }
}
