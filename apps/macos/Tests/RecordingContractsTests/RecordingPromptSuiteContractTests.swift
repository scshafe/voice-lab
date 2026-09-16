import Foundation
@testable import RecordingContracts
import Testing

/// TS1: prompt-suite v1 exactness and v2 rules against the committed generated schema.
@Suite struct RecordingPromptSuiteContractTests {
    private let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()

    private func validator() throws -> RecordingSchemaValidator {
        try RecordingSchemaValidator(preparing: Data(contentsOf: repository.appendingPathComponent("schemas/prompt-suite.schema.json")))
    }

    private func suite(_ prompts: [[String: RecordingJSON]], version: String = "cole-voice-lab.prompt-suite/v2",
                       suiteID: String = "synthetic-v2", purpose: String = "capture-pilot") throws -> Data {
        try RecordingJSON.object(["schema_version": .string(version), "suite_id": .string(suiteID), "purpose": .string(purpose),
                                  "prompts": .array(prompts.map(RecordingJSON.object))]).transportData()
    }

    private func spoken(_ changes: [String: RecordingJSON] = [:]) -> [String: RecordingJSON] {
        ["prompt_id": .string("spoken"), "text": .string("Read these synthetic words."), "category": .string("neutral"),
         "style": .string("Natural pace"), "expected_seconds": .number(4)].merging(changes) { $1 }
    }

    private func roomTone(_ changes: [String: RecordingJSON] = [:]) -> [String: RecordingJSON] {
        ["prompt_id": .string("room"), "instructions": .string("Remain still and silent."), "category": .string("room-tone"),
         "style": .string("Silence"), "expected_seconds": .number(15)].merging(changes) { $1 }
    }

    private func procedure(_ changes: [String: RecordingJSON] = [:]) -> [String: RecordingJSON] {
        ["prompt_id": .string("step"), "instructions": .string("Follow the synthetic step."), "category": .string("procedure"),
         "style": .string("Follow the steps"), "expected_seconds": .number(120), "labels": .array([.string("test")])].merging(changes) { $1 }
    }

    @Test func builtInAndSyntheticCatalogsValidate() throws {
        let validator = try validator()
        // TS2: every released built-in, v1 and v2, validates as a PromptSuite.
        for resource in ["pilot-v1.json", "evaluation-v1.json", "pilot-v2.json", "physical-test-v1.json"] {
            _ = try validator.validate(Data(contentsOf: repository.appendingPathComponent("src/cole_voice_lab/resources/" + resource)))
        }
        _ = try validator.validate(suite([spoken(["labels": .array([.string("warm-up")])]), roomTone()]))
        _ = try validator.validate(suite([roomTone(["labels": .array([.string("test")])]), procedure()], suiteID: "cole-physical-test-synthetic", purpose: "physical-test"))
        _ = try validator.validate(suite([spoken(), roomTone(["text": .string("Remain silent."), "instructions": .null])], version: "cole-voice-lab.prompt-suite/v1"))
        // Normalization inserts labels: [], which S1 must not mistake for present labels.
        _ = try validator.validate(suite([spoken(["labels": .array([])])], version: "cole-voice-lab.prompt-suite/v1"))
    }

    @Test func absentAndNullTextAreEquivalent() throws {
        let validator = try validator()
        var explicit = roomTone(); explicit["text"] = .null; explicit["labels"] = .array([])
        #expect(try validator.validate(suite([spoken(), roomTone()])) == validator.validate(suite([spoken(), explicit])))
    }

    @Test func v1ExactnessAndV2RulesFailClosed() throws {
        let validator = try validator()
        let v1 = "cole-voice-lab.prompt-suite/v1"
        let refused: [(String, Data)] = [
            ("v1 labels", try suite([spoken(["labels": .array([.string("warm-up")])])], version: v1)),
            ("v1 instructions", try suite([spoken(["instructions": .string("Extra")])], version: v1)),
            ("v1 absent text", try suite([roomTone()], version: v1)),
            ("v1 procedure", try suite([procedure(["text": .string("Words")])], version: v1)),
            ("v1 physical-test purpose", try suite([spoken()], version: v1, suiteID: "cole-physical-test-v1", purpose: "physical-test")),
            ("S2 room tone text", try suite([roomTone(["text": .string("Remain silent.")])])),
            ("S2 room tone without instructions", try suite([roomTone(["instructions": .null])])),
            ("P2 spoken without text", try suite([spoken(["text": .null])])),
            ("P3 procedure text", try suite([procedure(["text": .string("Words")])], suiteID: "cole-physical-test-synthetic", purpose: "physical-test")),
            ("P4 reserved label", try suite([spoken(["labels": .array([.string("missing")])])])),
            ("P4 duplicate label", try suite([spoken(["labels": .array([.string("warm-up"), .string("warm-up")])])])),
            ("S4 purpose without prefix", try suite([procedure()], purpose: "physical-test")),
            ("S4 prefix without purpose", try suite([spoken()], suiteID: "cole-physical-test-synthetic")),
            ("S4 missing test label", try suite([procedure(["labels": .array([])])], suiteID: "cole-physical-test-synthetic", purpose: "physical-test")),
            ("S4 test reference", try suite([spoken(["labels": .array([.string("test")]), "reference_candidate": .boolean(true)]), procedure()], suiteID: "cole-physical-test-synthetic", purpose: "physical-test")),
            ("S4 test label outside physical test", try suite([spoken(["labels": .array([.string("test")])])])),
            ("S4 procedure outside physical test", try suite([procedure(["labels": .array([])])])),
            ("S7 zero width", try suite([spoken(["text": .string("Read\u{200B} this.")])])),
            ("S7 bidi override", try suite([roomTone(["instructions": .string("Stay\u{202E} silent.")])])),
            ("S7 tab", try suite([spoken(["style": .string("Natural\tpace")])])),
        ]
        for (name, payload) in refused {
            var rejected = false
            do { _ = try validator.validate(payload) } catch is RecordingContractError { rejected = true }
            #expect(rejected, "\(name) must fail closed")
        }
        _ = try validator.validate(suite([spoken(["text": .string("First line.\nSecond line.")])]))
        _ = try validator.validate(suite([spoken(["text": .string("Legacy\u{200B} bytes.")])], version: v1))
    }
}
