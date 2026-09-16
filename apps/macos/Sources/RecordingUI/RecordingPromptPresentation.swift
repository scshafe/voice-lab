import Foundation
import RecordingContracts

/// Exact catalog prompt projection; UI text is never reconstructed from sorting or history.
/// There is deliberately no raw `text` property: spoken prompts and silent instructions are
/// separate derived fields, so a silent kind can never render as read-aloud text.
public struct RecordingPromptPresentation: Identifiable, Equatable, Sendable {
    public enum Kind: String, Equatable, Sendable { case spoken, roomTone = "room-tone", procedure }

    public let id: String
    public let category: String
    public let guidance: String
    public let expectedSeconds: Double
    public let labels: [String]
    public let referenceCandidate: Bool
    public let holdout: Bool
    /// The purpose of the catalog the prompt came from: in a configured workspace, the one the
    /// service delivered (the configuration names none).
    public let catalogPurpose: String
    public let kind: Kind
    /// Read-aloud text; nil for room tone and procedure steps in both catalog versions.
    public let displayPrompt: String?
    /// Silent instructions; a v1 room tone's text is shown here rather than as a Prompt.
    public let displayInstructions: String?
    public var isRoomTone: Bool { kind == .roomTone }
    public var title: String { id.replacingOccurrences(of: "-", with: " ").capitalized }
    public var effectiveLabels: [String] {
        [category] + (catalogPurpose == "physical-test" ? ["test"] : []) + labels.filter { $0 != "test" }
            + (referenceCandidate ? ["reference-candidate"] : []) + (holdout ? ["holdout"] : [])
    }
    /// Kind banner above the script sections, chosen by kind and never by text presence. A spoken
    /// prompt has none: its Prompt heading carries READ ALOUD.
    public var kindBanner: String? {
        switch kind {
        case .spoken: nil
        case .roomTone: "SILENT ROOM TONE · DO NOT SPEAK"
        case .procedure: "TEST STEP · FOLLOW INSTRUCTIONS"
        }
    }
    public var librarySymbol: String {
        switch kind {
        case .spoken: "text.alignleft"
        case .roomTone: "waveform"
        case .procedure: "checklist"
        }
    }
    /// The category chip and, in a physical-test catalog, the purpose-derived Test chip never overflow.
    public var pinnedLabelCount: Int { catalogPurpose == "physical-test" ? 2 : 1 }
    /// Library search fields (§6.4): title, id, Prompt, Instructions, guidance and every label title.
    public var searchHaystack: [String] {
        [title, id, displayPrompt, displayInstructions, guidance].compactMap { $0 } + effectiveLabels.map(Self.labelTitle)
    }
    /// Exact catalog content (Prompt, else Instructions); transport identity, never a rendered section.
    public let scriptBody: String

    public static func labelTitle(_ token: String) -> String {
        token.replacingOccurrences(of: "-", with: " ").capitalized
    }

    /// Chip rows from widest to narrowest: each hides one more trailing label into `+N`, and the
    /// last keeps only the pinned chips. The header shows the first row that fits.
    public static func chipRows(_ tokens: [String], pinned: Int) -> [[String]] {
        stride(from: tokens.count, through: min(max(pinned, 0), tokens.count), by: -1).map { shown in
            tokens.prefix(shown).map(labelTitle) + (shown < tokens.count ? ["+\(tokens.count - shown)"] : [])
        }
    }

    public init(_ value: RecordingJSON, purpose: String) throws {
        guard let object = value.object, let id = object["prompt_id"]?.string,
              let category = object["category"]?.string, let guidance = object["style"]?.string else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Catalog prompt is incomplete")
        }
        let seconds: Double
        switch object["expected_seconds"] {
        case .number(let value): seconds = value
        case .integer(let value): seconds = Double(value)
        default: throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Prompt duration is missing")
        }
        // Absent and null text/instructions are the same; schema normalization inserts null.
        let text = object["text"]?.string
        let instructions = object["instructions"]?.string
        let kind: Kind = switch category {
        case "room-tone": .roomTone
        case "procedure": .procedure
        default: .spoken
        }
        let displayPrompt = kind == .spoken ? text : nil
        let displayInstructions = instructions ?? (kind == .spoken ? nil : text)
        guard let body = displayPrompt ?? displayInstructions else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Catalog prompt has no prompt or instructions")
        }
        self.id = id; self.category = category
        self.guidance = guidance; self.expectedSeconds = seconds
        self.labels = (object["labels"]?.array ?? []).compactMap(\.string)
        self.referenceCandidate = object["reference_candidate"] == .boolean(true)
        self.holdout = object["holdout"] == .boolean(true)
        self.catalogPurpose = purpose; self.kind = kind
        self.displayPrompt = displayPrompt; self.displayInstructions = displayInstructions
        self.scriptBody = body
    }
}
