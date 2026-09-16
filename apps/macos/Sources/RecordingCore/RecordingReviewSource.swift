import Foundation
import RecordingContracts

/// Typed projection of the strictly validated Python review authorization.
public struct RecordingReviewSource: Sendable {
    public let raw: RecordingJSON
    public let playback: RecordingPlaybackSource
    public let retainedRange: Range<Int64>
    public let decision: String
    public let decisionRevision: Int64
    public let editRevision: Int64
    public let analysis: String
    public let recordedAt: String?
    public let datePrecision: String
    public let originalKind: String
    public let eligibilityMessages: [String]

    public init(_ value: RecordingJSON, assetRoot: URL) throws {
        guard let object = value.object, let takeID = object["take_id"]?.string,
              let digest = object["source_sha256"]?.string, let path = object["source_path"]?.string,
              let frames = object["source_frames"]?.integer, let range = object["frame_range"]?.object,
              range["source_frames"]?.integer == frames, let start = range["start_frame"]?.integer,
              let end = range["end_frame"]?.integer, start >= 0, start < end, end <= frames,
              let decision = object["decision"]?.string, let decisionRevision = object["decision_revision"]?.integer,
              let editRevision = object["edit_revision"]?.integer, decisionRevision >= 0, editRevision >= 0,
              let analysis = object["analysis"]?.string, let datePrecision = object["date_precision"]?.string,
              let originalKind = object["original_kind"]?.string, let identity = object["source_identity"] else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Review source lacks its bounded original and decision identities")
        }
        raw = value
        playback = try RecordingPlaybackSource(takeID: takeID, sourceSHA256: digest, assetRoot: assetRoot,
            relativePath: path, frameCount: frames, fileIdentity: identity)
        retainedRange = start..<end
        self.decision = decision; self.decisionRevision = decisionRevision; self.editRevision = editRevision
        self.analysis = analysis; self.datePrecision = datePrecision; self.originalKind = originalKind
        recordedAt = object["recorded_at"]?.string
        eligibilityMessages = object["eligibility_messages"]?.array?.compactMap(\.string) ?? []
    }

    public func frameRange(_ preview: Range<Int64>) -> RecordingJSON {
        .object(["source_frames": .integer(playback.frameCount), "start_frame": .integer(preview.lowerBound), "end_frame": .integer(preview.upperBound)])
    }
}
