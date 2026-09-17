import AppKit
import ApplicationServices
import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingUI
import SwiftUI
import Testing

@Suite(.serialized)
struct RecordingWorkspaceTests {
    @Test @MainActor func startingAndRecordingKeepStopIndependentOfUnavailableServices() {
        for phase in [RecordingShellPhase.starting, .recording] {
            var state = RecordingWorkspaceState()
            state.phase = phase; state.deviceReady = false; state.serviceReady = false
            state.storageReady = false; state.contextReady = false; state.permission = .denied
            #expect(state.canStop)
            #expect(state.showsStop)
            #expect(!state.canRecord)
        }
        var state = RecordingWorkspaceState(); state.phase = .stopping
        #expect(state.showsStop)
        #expect(!state.canStop)
    }

    @Test @MainActor func recordRequiresEveryIndependentReadinessGate() throws {
        let model = RecordingWorkspaceModel(fixture: .init())
        var ready = model.state
        ready.contextReady = true; ready.deviceReady = true; ready.storageReady = true
        ready.serviceReady = true; ready.permission = .notDetermined
        #expect(ready.canRecord)
        for gate in ["context", "device", "storage", "service", "permission"] {
            var unavailable = ready
            switch gate {
            case "context": unavailable.contextReady = false
            case "device": unavailable.deviceReady = false
            case "storage": unavailable.storageReady = false
            case "service": unavailable.serviceReady = false
            default: unavailable.permission = .denied
            }
            #expect(!unavailable.canRecord)
        }
        ready.historyCompleteness = "unknown"
        #expect(ready.canRecord)
    }

    @Test @MainActor func unavailableAndInertFixturesCannotCreateProcessesOrSave() {
        let unavailable = RecordingWorkspaceModel(unavailable: "Configuration missing")
        unavailable.record(); unavailable.save(); unavailable.stop()
        #expect(unavailable.configuration == nil)
        #expect(!unavailable.state.canRecord)
        #expect(!unavailable.canSave)
        #expect(unavailable.state.status == "Configuration missing")
        let draft = RecordingWorkspaceModel(fixture: .init(phase: .draftReview))
        draft.save(); draft.record()
        #expect(!draft.canSave)
        #expect(draft.waveform.snapshot == nil)
    }

    @Test @MainActor func explicitFocusAndReadingSizeNeverFollowTransportPhase() {
        for phase in RecordingShellPhase.allCases {
            let model = RecordingWorkspaceModel(fixture: .init(phase: phase, scriptSize: 32, focusMode: true, roomTone: true))
            #expect(model.focusMode)
            #expect(model.scriptSize == 32)
            #expect(model.state.prompt?.isRoomTone == true)
            #expect(model.state.prompt?.displayPrompt == nil)
            #expect(model.state.prompt?.displayInstructions?.contains("Do not read") == true)
        }
    }

    /// TU0 (Step 6 card): silent kinds never render the placeholder or READ ALOUD; v1 spoken rendering is unchanged.
    @Test @MainActor func silentKindsNeverReadAloudOrShowThePlaceholder() throws {
        let placeholder = "Your selected prompt will appear here."
        let v2RoomTone = try Self.presentation(["prompt_id": .string("room-tone-start"), "instructions": .string("Remain still and silent."),
            "category": .string("room-tone"), "style": .string("Silence"), "expected_seconds": .integer(15)])
        let nullText = try Self.presentation(["prompt_id": .string("room-tone-start"), "text": .null, "instructions": .string("Remain still and silent."),
            "labels": .array([]), "category": .string("room-tone"), "style": .string("Silence"), "expected_seconds": .integer(15)])
        let procedure = try Self.presentation(["prompt_id": .string("p10-unplug-while-capturing"), "instructions": .string("Unplug the synthetic device."),
            "labels": .array([.string("test"), .string("device-loss")]), "category": .string("procedure"),
            "style": .string("Follow the steps"), "expected_seconds": .integer(10)], purpose: "physical-test")
        #expect(v2RoomTone == nullText)
        for (silent, banner) in [(v2RoomTone, "SILENT ROOM TONE · DO NOT SPEAK"), (procedure, "TEST STEP · FOLLOW INSTRUCTIONS")] {
            #expect(silent.kindBanner == banner)
            #expect(silent.displayPrompt == nil)
            #expect(silent.scriptBody == silent.displayInstructions)
            let texts = Self.hosted(RecordingScriptCard(prompt: silent, scriptSize: 32)).flatMap(\.texts)
            #expect(texts.contains(banner) && texts.contains("No spoken prompt") && texts.contains(silent.displayInstructions!))
            #expect(!texts.contains { $0.hasSuffix("· READ ALOUD") || $0 == "READ ALOUD" || $0.contains(placeholder) })
        }
        let v1Spoken = try Self.presentation(["prompt_id": .string("natural-reading"), "text": .string("Read these synthetic words."),
            "category": .string("neutral"), "style": .string("Natural pace"), "expected_seconds": .number(4), "reference_candidate": .boolean(true)])
        #expect(v1Spoken.kindBanner == nil && v1Spoken.scriptBody == "Read these synthetic words.")
        #expect(v1Spoken.displayPrompt == "Read these synthetic words." && v1Spoken.displayInstructions == nil)
        let spokenTexts = Self.hosted(RecordingScriptCard(prompt: v1Spoken, scriptSize: 24)).flatMap(\.texts)
        #expect(spokenTexts.contains("PROMPT · READ ALOUD") && spokenTexts.contains("Read these synthetic words."))
        #expect(throws: RecordingContractError.self) {
            try Self.presentation(["prompt_id": .string("room"), "category": .string("room-tone"), "style": .string("Silence"), "expected_seconds": .integer(15)])
        }
    }

    /// TU1: derived fields for every kind in both catalog versions; a v1 room tone's text is Instructions.
    @Test func derivedFieldsMatchThePresentationContract() throws {
        for (prompt, kind, displayPrompt, displayInstructions, labels, symbol) in try Self.presentationMatrix() {
            #expect(prompt.kind == kind)
            #expect(prompt.displayPrompt == displayPrompt)
            #expect(prompt.displayInstructions == displayInstructions)
            #expect(prompt.effectiveLabels == labels)
            #expect(prompt.librarySymbol == symbol)
            #expect(prompt.guidance == "Guidance for " + prompt.id)
            #expect((prompt.kindBanner == nil) == (kind == .spoken))
        }
        #expect(RecordingPromptPresentation.labelTitle("reference-candidate") == "Reference Candidate")
    }

    /// TU1: `recording.script` exists iff the prompt is spoken; `recording.instructions` always exists and holds the guidance.
    @Test @MainActor func scriptSectionsFollowKindInEveryCatalogVersion() throws {
        for (prompt, kind, displayPrompt, displayInstructions, _, _) in try Self.presentationMatrix() {
            let nodes = Self.hosted(RecordingScriptCard(prompt: prompt, scriptSize: 24))
            let script = nodes.filter { $0.identifier == "recording.script" }
            let instructions = nodes.filter { $0.identifier == "recording.instructions" }
            #expect(script.count == (kind == .spoken ? 1 : 0), "\(prompt.id)")
            #expect(script.allSatisfy { $0.label == "Prompt, read aloud" && $0.texts.contains(displayPrompt ?? "") })
            #expect(instructions.count == 1, "\(prompt.id)")
            #expect(instructions.first?.label == "Instructions, do not read aloud")
            #expect(instructions.first?.texts.contains("Guidance: " + prompt.guidance) == true, "\(prompt.id)")
            #expect(instructions.first?.texts.contains("INSTRUCTIONS · NOT READ ALOUD") == true)
            if let displayInstructions { #expect(instructions.first?.texts.contains(displayInstructions) == true, "\(prompt.id)") }
            // Instructions are never inside the read-aloud section.
            if let displayInstructions { #expect(!script.contains { $0.texts.contains(displayInstructions) }) }
        }
    }

    /// TU1: chips follow effective-label order, keep the category and Test chips pinned, and overflow into `+N`.
    @Test @MainActor func labelChipsKeepOrderPinTheCategoryAndTestAndOverflow() throws {
        let tokens = ["procedure", "test", "recovery", "device-loss", "reference-candidate"]
        #expect(RecordingPromptPresentation.chipRows(tokens, pinned: 2) == [
            ["Procedure", "Test", "Recovery", "Device Loss", "Reference Candidate"],
            ["Procedure", "Test", "Recovery", "Device Loss", "+1"],
            ["Procedure", "Test", "Recovery", "+2"],
            ["Procedure", "Test", "+3"],
        ])
        #expect(RecordingPromptPresentation.chipRows(["neutral", "holdout"], pinned: 1) == [["Neutral", "Holdout"], ["Neutral", "+1"]])
        #expect(RecordingPromptPresentation.chipRows(["room-tone"], pinned: 1) == [["Room Tone"]])
        let step = try Self.presentation(["prompt_id": .string("p11-reconnect-and-recover"), "instructions": .string("Reconnect the synthetic device."),
            "labels": .array([.string("recovery"), .string("test"), .string("device-loss")]), "category": .string("procedure"),
            "style": .string("Follow the steps"), "expected_seconds": .integer(3)], purpose: "physical-test")
        #expect(step.effectiveLabels == ["procedure", "test", "recovery", "device-loss"])
        #expect(step.pinnedLabelCount == 2)
        let label = "Labels: Procedure, Test, Recovery, Device Loss"
        let wide = try #require(Self.hosted(RecordingLabelChips(prompt: step), size: CGSize(width: 600, height: 20)).first { $0.identifier == "recording.labels" })
        #expect(wide.label == label && wide.texts == [label, "Procedure", "Test", "Recovery", "Device Loss"])
        // A narrow header still shows the pinned chips; the accessibility label keeps every label.
        let narrow = try #require(Self.hosted(RecordingLabelChips(prompt: step), size: CGSize(width: 40, height: 20)).first { $0.identifier == "recording.labels" })
        #expect(narrow.label == label && narrow.texts == [label, "Procedure", "Test", "+2"])
        let spoken = try Self.presentation(["prompt_id": .string("reading"), "text": .string("Synthetic words."), "category": .string("neutral"),
            "style": .string("Natural"), "expected_seconds": .integer(3)], purpose: "capture-pilot")
        #expect(spoken.pinnedLabelCount == 1 && spoken.effectiveLabels == ["neutral"])
    }

    /// TU1: catalog text containing Markdown renders literally in chips, Prompt, Instructions and Guidance.
    @Test @MainActor func catalogMarkdownRendersLiterally() throws {
        let prompt = try Self.presentation(["prompt_id": .string("markdown-step"), "text": .string("Say **x** now."),
            "instructions": .string("Open [a](b) first."), "labels": .array([.string("**x**"), .string("[a](b)")]),
            "category": .string("neutral"), "style": .string("Keep *calm*"), "expected_seconds": .integer(3)], purpose: "capture-pilot")
        let texts = Self.hosted(RecordingScriptCard(prompt: prompt, scriptSize: 24)).flatMap(\.texts)
        for literal in ["Say **x** now.", "Open [a](b) first.", "Guidance: Keep *calm*"] { #expect(texts.contains(literal), "\(literal)") }
        let chips = try #require(Self.hosted(RecordingLabelChips(prompt: prompt), size: CGSize(width: 600, height: 20)).first { $0.identifier == "recording.labels" })
        #expect(chips.label == "Labels: Neutral, **X**, [A](B)" && chips.texts == [chips.label, "Neutral", "**X**", "[A](B)"])
    }

    /// TU1 (§6.3): under a Prompt, Instructions keep their natural height when short and never take more
    /// than 40% of the card; longer instructions scroll inside that share while the Prompt keeps the rest.
    @Test @MainActor func instructionsUnderAPromptTakeAtMostFortyPercentOfTheCard() throws {
        let size = CGSize(width: 748, height: 256)
        func sections(instructions: String) throws -> (script: CGRect, instructions: CGRect) {
            let prompt = try Self.presentation(["prompt_id": .string("reading"), "text": .string("Read the synthetic words."), "instructions": .string(instructions),
                "category": .string("neutral"), "style": .string("Natural"), "expected_seconds": .integer(3)])
            let nodes = Self.hosted(RecordingScriptCard(prompt: prompt, scriptSize: 24), size: size)
            return (try #require(nodes.first { $0.identifier == "recording.script" }).frame,
                    try #require(nodes.first { $0.identifier == "recording.instructions" }).frame)
        }
        let short = try sections(instructions: "Face the synthetic microphone.")
        let long = try sections(instructions: String(repeating: "Keep the synthetic microphone still. ", count: 40))
        #expect(short.instructions.height < long.instructions.height)
        #expect(long.instructions.height <= size.height * 0.4 + 1)
        #expect(short.script.height > long.script.height)
        for card in [short, long] {
            // Real frames: Instructions hold at least the heading and the 32-point guidance row.
            #expect(card.instructions.height > 32 && card.script.height > 32)
            #expect(card.script.height + card.instructions.height <= size.height)
            #expect(!card.script.insetBy(dx: 0, dy: 1).intersects(card.instructions))
        }
    }

    /// TU1: the hosted workspace mounts the chips row in its header and the script card in its script
    /// region, so a spoken fixture shows both sections and a room-tone fixture shows Instructions only.
    @Test @MainActor func workspaceMountsChipsAndScriptSectionsForEveryFixtureKind() throws {
        for roomTone in [false, true] {
            let nodes = Self.hosted(RecordingWorkspace(state: .init(roomTone: roomTone)), size: CGSize(width: 1060, height: 700))
            let chips = nodes.filter { $0.identifier == "recording.labels" }
            #expect(chips.count == 1 && chips.first?.label == (roomTone ? "Labels: Room Tone" : "Labels: Neutral"), "\(roomTone)")
            #expect(nodes.filter { $0.identifier == "recording.script" }.count == (roomTone ? 0 : 1), "\(roomTone)")
            #expect(nodes.filter { $0.identifier == "recording.instructions" }.count == 1, "\(roomTone)")
        }
    }

    /// TU1 (§6.4): search is case-insensitive over the title, id, Prompt, Instructions, guidance and every label title.
    @Test @MainActor func librarySearchMatchesEveryHaystackField() {
        let model = RecordingWorkspaceModel(fixture: .init(), catalog: Self.libraryCatalog())
        #expect(model.prompts.map(\.id) == ["room-tone-start", "p01-warm-reading", "p02-unplug-device", "p03-quick-numbers"])
        for (query, expected, field) in [
            ("Quick Numbers", ["p03-quick-numbers"], "title"),
            ("p03-quick", ["p03-quick-numbers"], "prompt_id"),
            ("warm synthetic words", ["p01-warm-reading"], "displayPrompt"),
            ("Unplug the", ["p02-unplug-device"], "displayInstructions"),
            ("start tone", ["room-tone-start"], "v1 room-tone text as Instructions"),
            ("Steady pace", ["p03-quick-numbers"], "style"),
            ("Reference Candidate", ["p01-warm-reading"], "label title"),
            ("device loss", ["p02-unplug-device"], "label title, case-insensitive"),
            ("STEADY PACE", ["p03-quick-numbers"], "style, case-insensitive"),
            ("Test", ["room-tone-start", "p01-warm-reading", "p02-unplug-device", "p03-quick-numbers"], "purpose-derived Test label"),
            ("no such words", [], "no match"),
        ] {
            model.searchText = query
            #expect(model.filteredPrompts.map(\.id) == expected, "\(field): \(query)")
        }
        #expect(model.currentPromptFilteredOut)
        model.searchText = ""
        #expect(model.filteredPrompts.count == 4 && !model.currentPromptFilteredOut)
    }

    /// TU1 (§6.4): an empty selection shows all; labels combine with OR; labels, status and search combine with AND;
    /// `currentPromptFilteredOut` follows the same predicate.
    @Test @MainActor func labelFilterTruthTable() {
        let model = RecordingWorkspaceModel(fixture: .init(), catalog: Self.libraryCatalog())
        model.state.promptStatuses = ["room-tone-start": "missing", "p01-warm-reading": "recorded", "p02-unplug-device": "missing", "p03-quick-numbers": "draft"]
        #expect(model.selectedLabels.isEmpty && model.filteredPrompts.count == 4 && !model.currentPromptFilteredOut)
        model.selectedLabels = ["warm"]
        #expect(model.filteredPrompts.map(\.id) == ["p01-warm-reading"])
        #expect(model.currentPromptFilteredOut, "the selected room tone carries no warm label")
        model.selectedLabels = ["warm", "device-loss"]
        #expect(model.filteredPrompts.map(\.id) == ["p01-warm-reading", "p02-unplug-device"], "A or B")
        model.selectedLabels = ["recovery"]
        #expect(model.filteredPrompts.map(\.id) == ["p02-unplug-device", "p03-quick-numbers"])
        model.libraryFilter = "missing"
        #expect(model.filteredPrompts.map(\.id) == ["p02-unplug-device"], "labels and status combine with AND")
        model.libraryFilter = "attention"
        #expect(model.filteredPrompts.map(\.id) == ["p03-quick-numbers"])
        model.libraryFilter = "all"; model.searchText = "count"
        #expect(model.filteredPrompts.map(\.id) == ["p03-quick-numbers"], "labels and search combine with AND")
        model.searchText = "Unplug"
        #expect(model.filteredPrompts.map(\.id) == ["p02-unplug-device"])
        model.searchText = "Quick"; model.libraryFilter = "missing"
        #expect(model.filteredPrompts.isEmpty && model.currentPromptFilteredOut)
        model.searchText = ""; model.libraryFilter = "all"; model.selectedLabels = ["room-tone", "warm"]
        #expect(model.filteredPrompts.map(\.id) == ["room-tone-start", "p01-warm-reading"] && !model.currentPromptFilteredOut)
        model.showAllLabels()
        #expect(model.selectedLabels.isEmpty && model.filteredPrompts.count == 4)
        model.setLabel("numbers", selected: true); model.setLabel("numbers", selected: true)
        #expect(model.selectedLabels == ["numbers"] && model.filteredPrompts.map(\.id) == ["p03-quick-numbers"])
        model.setLabel("numbers", selected: false)
        #expect(model.selectedLabels.isEmpty && model.filteredPrompts.count == 4)
    }

    /// TU1 (§6.4): options are the union of effective labels in first-appearance catalog order, and tokens absent
    /// from the loaded catalog are pruned, so a stale selection never hides every prompt.
    @Test @MainActor func labelOptionsFollowFirstAppearanceOrderAndPruneUnknownTokens() {
        let model = RecordingWorkspaceModel(fixture: .init(), catalog: Self.libraryCatalog())
        #expect(model.labelOptions == ["room-tone", "test", "neutral", "warm", "reference-candidate", "procedure", "device-loss", "recovery", "numbers", "holdout"])
        model.selectedLabels = ["warm", "ghost"]
        #expect(model.selectedLabels == ["warm"])
        model.selectedLabels = ["ghost"]
        #expect(model.selectedLabels.isEmpty && model.filteredPrompts.count == 4)
        model.setLabel("ghost", selected: true)
        #expect(model.selectedLabels.isEmpty && model.labelsMenuTitle == "Labels: Any")
        #expect(RecordingWorkspaceModel(fixture: .init(roomTone: true)).labelOptions == ["room-tone"])
        #expect(RecordingWorkspaceModel(fixture: .init()).labelOptions == ["neutral"])
        #expect(RecordingWorkspaceModel(unavailable: "Configuration missing").labelOptions.isEmpty)
    }

    /// TU1 (§6.4): the menu reads "Labels: Any", up to two selected titles in option order, else the selected count.
    @Test @MainActor func labelsMenuTitleForms() {
        let model = RecordingWorkspaceModel(fixture: .init(), catalog: Self.libraryCatalog())
        #expect(model.labelsMenuTitle == "Labels: Any")
        model.selectedLabels = ["recovery"]
        #expect(model.labelsMenuTitle == "Labels: Recovery")
        model.selectedLabels = ["test", "room-tone"]
        #expect(model.labelsMenuTitle == "Labels: Room Tone, Test", "option order, not insertion order")
        model.selectedLabels = ["device-loss", "reference-candidate"]
        #expect(model.labelsMenuTitle == "Labels: Reference Candidate, Device Loss")
        model.selectedLabels = ["recovery", "warm", "holdout"]
        #expect(model.labelsMenuTitle == "Labels: 3 selected")
        model.selectedLabels = Set(model.labelOptions)
        #expect(model.labelsMenuTitle == "Labels: 10 selected")
        model.showAllLabels()
        #expect(model.labelsMenuTitle == "Labels: Any")
    }

    /// TU1 (§6.4): search and filters never affect Continue Missing, Save & Next or the covered count.
    @Test @MainActor func filtersNeverAffectContinueMissingSaveNextOrTheCoveredCount() {
        let model = RecordingWorkspaceModel(fixture: .init(), catalog: Self.libraryCatalog())
        model.state.contextReady = true; model.state.historyCompleteness = "complete"
        model.state.coveredCount = 2; model.state.totalCount = 4
        model.state.promptStatuses = ["room-tone-start": "missing", "p01-warm-reading": "recorded", "p02-unplug-device": "missing", "p03-quick-numbers": "recorded"]
        #expect(model.canContinueMissing && model.saveAdvances && model.saveActionTitle == "Save & Next")
        model.searchText = "no such words"; model.libraryFilter = "missing"; model.selectedLabels = ["holdout"]
        #expect(model.filteredPrompts.isEmpty && model.currentPromptFilteredOut)
        #expect(model.canContinueMissing && model.saveAdvances && model.saveActionTitle == "Save & Next")
        #expect(model.state.coveredCount == 2 && model.state.totalCount == 4)
        model.state.historyCompleteness = "building"
        #expect(!model.canContinueMissing && !model.saveAdvances && model.saveActionTitle == "Save")
        model.searchText = ""; model.libraryFilter = "all"; model.showAllLabels()
        #expect(!model.canContinueMissing, "clearing filters cannot enable Continue Missing without complete coverage")
    }

    /// TU1 (§6.4): the sidebar hosts a prominent full-width Continue Missing above the search field, then the Show
    /// picker and the Labels menu; the search field stays the first editable field and the menu is not editable.
    @Test @MainActor func sidebarPlacesContinueMissingAboveSearchAndTheLabelMenuIsNotEditable() throws {
        let model = RecordingWorkspaceModel(fixture: .init(), catalog: Self.libraryCatalog())
        model.selectedLabels = ["test", "room-tone"]
        let size = CGSize(width: 1060, height: 700)
        let (nodes, firstEditable) = Self.hosting(RecordingWorkspace(model: model), size: size) { host -> ([AccessibilityNode], CGRect?) in
            @MainActor func textField(_ view: NSView) -> NSTextField? {
                if let field = view as? NSTextField, field.isEditable { return field }
                for child in view.subviews { if let field = textField(child) { return field } }
                return nil
            }
            return (Self.accessibilityNodes(host), textField(host)?.accessibilityFrame())
        }
        func node(_ identifier: String) throws -> AccessibilityNode { try #require(nodes.first { $0.identifier == identifier }, "\(identifier)") }
        let continueMissing = try node("library.continueMissing"), search = try node("library.search")
        let show = try node("library.filter"), labels = try node("library.labels")
        #expect(continueMissing.texts.contains("Continue Missing"))
        #expect(labels.texts.contains("Labels: Room Tone, Test"))
        // Screen frames grow upward, so a control above another has the larger minY.
        #expect(continueMissing.frame.minY >= search.frame.maxY - 1, "\(continueMissing.frame) above \(search.frame)")
        #expect(search.frame.minY >= show.frame.maxY - 1 && show.frame.minY >= labels.frame.maxY - 1)
        #expect(continueMissing.frame.width >= RecordingLayout.sidebarWidth - 2 * RecordingLayout.inset - 1)
        #expect(continueMissing.frame.height > search.frame.height, "large control size")
        // The transport harness rule: the first editable NSTextField in view order is the search field, whose
        // SwiftUI node sits one point inside the field's bezel.
        let field = try #require(firstEditable)
        #expect(search.frame.insetBy(dx: -2, dy: -2).contains(field), "\(field) vs \(search.frame)")
        #expect(!labels.frame.intersects(field.insetBy(dx: 1, dy: 1)))
        // Without a selected prompt the card shows only its placeholder; there is no empty Instructions heading.
        let unselected = Self.hosted(RecordingScriptCard(prompt: nil, scriptSize: 24))
        #expect(unselected.flatMap(\.texts).contains("Your selected prompt will appear here."))
        #expect(!unselected.contains { $0.identifier == "recording.instructions" } && !unselected.flatMap(\.texts).contains("INSTRUCTIONS · NOT READ ALOUD"))
    }

    /// The fine-tune footer slot is one control named in words in both states, and the status region
    /// is one element named Status whose line VoiceOver reads with its warning.
    @Test @MainActor func theFineTuneSlotAndTheStatusRegionAreNamedForVoiceOver() throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .draftReview))
        let footer = CGSize(width: 748, height: RecordingLayout.recordTarget)
        let idle = Self.hosted(RecordingTransportControls(model: model), size: footer)
        let start = try #require(idle.first { $0.identifier == "transport.fine-tune" })
        #expect(start.label == "Fine-tune model" && start.texts.contains("Fine-tune model"))
        #expect(!idle.contains { $0.identifier == "transport.cancel-fine-tune" })
        model.fineTune.phase = .running
        model.fineTune.operationID = "fine-tune-job"
        let running = Self.hosted(RecordingTransportControls(model: model), size: footer)
        let cancel = try #require(running.first { $0.identifier == "transport.cancel-fine-tune" })
        #expect(cancel.label == "Cancel fine-tune")
        #expect(!running.contains { $0.identifier == "transport.fine-tune" })
        // Both labels fit the slot's fixed width, so switching never moves Retake or Save.
        #expect(abs(start.frame.width - cancel.frame.width) <= 1, "\(start.frame) vs \(cancel.frame)")
        #expect(start.frame.minX == cancel.frame.minX)

        let statusSize = CGSize(width: 748, height: 20)
        let quiet = try #require(Self.hosted(RecordingReviewStatus(model: model), size: statusSize).first { $0.identifier == "recording.status" })
        #expect(quiet.label == "Status" && quiet.texts.contains(model.statusRegion.line))
        let failure = "fine-tune supervision stopped; restart the app to recover the job"
        model.applyFineTuneSupervisorError(.object(["code": .string("RECORDING.WRITE_FAILED"), "message": .string(failure)]))
        #expect(model.statusRegion.line == failure && model.statusRegion.needsAttention)
        let warned = try #require(Self.hosted(RecordingReviewStatus(model: model), size: statusSize).first { $0.identifier == "recording.status" })
        #expect(warned.label == "Status" && warned.texts.contains("Warning: " + failure), "\(warned.texts)")
    }

    /// TU1 (§6.4): a catalog label containing Markdown renders literally in the Labels menu title.
    @Test @MainActor func labelsMenuTitleRendersCatalogTitlesVerbatim() throws {
        let catalog = RecordingJSON.object(["purpose": .string("capture-pilot"), "prompts": .array([
            .object(["prompt_id": .string("markdown-step"), "text": .string("Say **x** now."), "labels": .array([.string("**x**"), .string("[a](b)")]),
                     "category": .string("neutral"), "style": .string("Keep *calm*"), "expected_seconds": .integer(3)]),
        ])])
        let model = RecordingWorkspaceModel(fixture: .init(), catalog: catalog)
        #expect(model.labelOptions == ["neutral", "**x**", "[a](b)"])
        model.selectedLabels = ["**x**", "[a](b)"]
        #expect(model.labelsMenuTitle == "Labels: **X**, [A](B)")
        let labels = try #require(Self.hosted(RecordingWorkspace(model: model), size: CGSize(width: 1060, height: 700)).first { $0.identifier == "library.labels" })
        #expect(labels.texts.contains("Labels: **X**, [A](B)"), "\(labels.texts)")
    }

    /// A synthetic physical-test catalog: a v1-shaped room tone, a spoken reference candidate, a procedure step and a
    /// holdout, with distinct ids, titles, texts, instructions, guidance and labels for the search and filter cases.
    private static func libraryCatalog() -> RecordingJSON {
        .object(["purpose": .string("physical-test"), "prompts": .array([
            .object(["prompt_id": .string("room-tone-start"), "text": .string("Remain silent for the start tone."),
                     "category": .string("room-tone"), "style": .string("Silence at the start"), "expected_seconds": .integer(15)]),
            .object(["prompt_id": .string("p01-warm-reading"), "text": .string("Read the warm synthetic words."), "instructions": .string("Face the synthetic microphone."),
                     "labels": .array([.string("warm"), .string("test")]), "category": .string("neutral"), "style": .string("Warm delivery"),
                     "expected_seconds": .integer(8), "reference_candidate": .boolean(true)]),
            .object(["prompt_id": .string("p02-unplug-device"), "instructions": .string("Unplug the synthetic device."),
                     "labels": .array([.string("device-loss"), .string("recovery")]), "category": .string("procedure"), "style": .string("Follow each step"),
                     "expected_seconds": .integer(5)]),
            .object(["prompt_id": .string("p03-quick-numbers"), "text": .string("Count from one to five."), "labels": .array([.string("recovery")]),
                     "category": .string("numbers"), "style": .string("Steady pace"), "expected_seconds": .integer(4), "holdout": .boolean(true)]),
        ])])
    }

    /// Spoken, room tone and procedure prompts as v1-shaped (text only) and v2-shaped catalog JSON.
    private static func presentationMatrix() throws -> [(RecordingPromptPresentation, RecordingPromptPresentation.Kind, String?, String?, [String], String)] {
        func prompt(_ id: String, _ category: String, text: String? = nil, instructions: String? = nil, labels: [String] = [], purpose: String = "capture-pilot",
                    reference: Bool = false, holdout: Bool = false) throws -> RecordingPromptPresentation {
            var object: [String: RecordingJSON] = ["prompt_id": .string(id), "category": .string(category), "style": .string("Guidance for " + id),
                "expected_seconds": .integer(12), "reference_candidate": .boolean(reference), "holdout": .boolean(holdout)]
            if let text { object["text"] = .string(text) }
            if let instructions { object["instructions"] = .string(instructions) }
            if !labels.isEmpty { object["labels"] = .array(labels.map(RecordingJSON.string)) }
            return try presentation(object, purpose: purpose)
        }
        return [
            (try prompt("v1-spoken", "reference", text: "Read the v1 words.", reference: true), .spoken, "Read the v1 words.", nil, ["reference", "reference-candidate"], "text.alignleft"),
            (try prompt("v1-room-tone", "room-tone", text: "Remain quiet for ten seconds."), .roomTone, nil, "Remain quiet for ten seconds.", ["room-tone"], "waveform"),
            (try prompt("v1-procedure", "procedure", text: "Unplug the synthetic device."), .procedure, nil, "Unplug the synthetic device.", ["procedure"], "checklist"),
            (try prompt("v2-spoken", "neutral", text: "Read the v2 words.", instructions: "Face the synthetic microphone.", labels: ["warm"], holdout: true),
             .spoken, "Read the v2 words.", "Face the synthetic microphone.", ["neutral", "warm", "holdout"], "text.alignleft"),
            (try prompt("v2-room-tone", "room-tone", instructions: "Remain still and silent."), .roomTone, nil, "Remain still and silent.", ["room-tone"], "waveform"),
            (try prompt("v2-procedure", "procedure", instructions: "Reconnect the synthetic device.", labels: ["test", "recovery"], purpose: "physical-test"),
             .procedure, nil, "Reconnect the synthetic device.", ["procedure", "test", "recovery"], "checklist"),
        ]
    }

    private static func presentation(_ object: [String: RecordingJSON], purpose: String = "capture-pilot") throws -> RecordingPromptPresentation {
        try RecordingPromptPresentation(.object(object), purpose: purpose)
    }

    @MainActor private static var applicationLaunched = false
    /// One accessibility element with its own label, screen frame and every text in its subtree.
    struct AccessibilityNode { let identifier: String; let label: String; let texts: [String]; let frame: CGRect }

    /// Hosts a view in a window that is never ordered and reads its in-process accessibility tree.
    @MainActor private static func hosted(_ view: some View, size: CGSize = CGSize(width: 748, height: 256)) -> [AccessibilityNode] {
        hosting(view, size: size) { accessibilityNodes($0) }
    }

    /// Hosts a view in a window that is never ordered and hands the live host to `inspect` before it closes.
    @MainActor private static func hosting<V: View, R>(_ view: V, size: CGSize, inspect: @MainActor (NSHostingView<V>) -> R) -> R {
        NSApplication.shared.setActivationPolicy(.prohibited)
        if !applicationLaunched { NSApplication.shared.finishLaunching(); applicationLaunched = true }
        let host = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: CGRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        window.contentView = host; host.frame = CGRect(origin: .zero, size: size); host.layoutSubtreeIfNeeded()
        defer { window.contentView = nil; window.close() }
        return inspect(host)
    }

    /// Every identified accessibility element under a hosted view, plus one root node holding every text.
    @MainActor private static func accessibilityNodes(_ host: NSView) -> [AccessibilityNode] {
        // An own-process request, as in the accessibility harness, makes SwiftUI publish its accessibility nodes.
        var role: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(AXUIElementCreateApplication(getpid()), kAXRoleAttribute as CFString, &role)
        var nodes: [AccessibilityNode] = []
        // SwiftUI's accessibility nodes answer the informal NSAccessibility selectors without declaring the protocol.
        @discardableResult func visit(_ element: AnyObject, depth: Int) -> [String] {
            guard depth < 40 else { return [] }
            let label = (element.accessibilityLabel?() ?? nil) ?? ""
            let value = element.perform?(#selector(NSAccessibilityElement.accessibilityValue))?.takeUnretainedValue() as? String ?? ""
            var texts = [label, (element.accessibilityTitle?() ?? nil) ?? "", value].filter { !$0.isEmpty }
            let identifier = element.accessibilityIdentifier?() ?? ""
            // Key-value coding reaches the struct-valued frame getter, which `perform` cannot return.
            let object = element as? NSObject
            let frame = object?.responds(to: NSSelectorFromString("accessibilityFrame")) == true
                ? (object?.value(forKey: "accessibilityFrame") as? NSValue)?.rectValue ?? .null : .null
            let index = nodes.count
            if !identifier.isEmpty { nodes.append(AccessibilityNode(identifier: identifier, label: label, texts: [], frame: frame)) }
            for child in (element.accessibilityChildren?() ?? nil) ?? [] { texts += visit(child as AnyObject, depth: depth + 1) }
            if !identifier.isEmpty { nodes[index] = AccessibilityNode(identifier: identifier, label: label, texts: texts, frame: frame) }
            return texts
        }
        nodes.append(AccessibilityNode(identifier: "", label: "", texts: visit(host, depth: 0), frame: host.frame))
        return nodes
    }

    @Test func privateStorageUsesSameBoundedReservationAsWriterAndRejectsSymlinks() throws {
        let root = URL(fileURLWithPath: "/private/tmp/cole-native-workspace-storage-" + UUID().uuidString.lowercased(), isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        try NativeCaptureStorage.preflight(assetRoot: root)
        let alias = root.appendingPathComponent("alias")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: root)
        #expect(throws: NativeCaptureFailure.self) { try NativeCaptureStorage.preflight(assetRoot: alias) }
        #expect(throws: NativeCaptureFailure.self) { try NativeCaptureStorage.preflight(assetRoot: root, maximumFrames: 0) }
    }
}
