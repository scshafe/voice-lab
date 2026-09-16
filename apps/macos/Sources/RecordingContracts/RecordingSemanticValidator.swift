import Foundation

/// Correlated transport invariants supplement JSON Schema. Canonical evidence
/// digests are revalidated by Python, the single canonical serialization owner.
enum RecordingSemanticValidator {
    private static let silentPromptCategories: Set<String> = ["room-tone", "procedure"]
    private static let recordingGroupV2 = "cole-voice-lab.recording-group/v2"
    /// Mirrors Python RESERVED_PROMPT_LABELS; conformance fixtures keep the two lists aligned.
    private static let reservedPromptLabels: Set<String> = [
        "room-tone", "neutral", "conversational", "warm-narration", "technical", "numbers", "expressive", "reference", "procedure",
        "reference-candidate", "holdout",
        "all", "attention", "missing", "recorded", "verified", "draft", "needs-review", "saved-checking", "pending", "running",
        "invalid", "failed", "unknown", "removed", "rejected", "kept",
        "prompt", "instructions", "guidance", "read-aloud", "silent", "do-not-speak",
    ]

    static func validate(title: String, object: [String: RecordingJSON]) throws {
        let value = RecordingJSON.object(object)
        func field(_ path: String) -> RecordingJSON { lookup(value, path) }
        func string(_ path: String) -> String { field(path).string ?? "" }
        func number(_ path: String) -> Double { field(path).number ?? 0 }
        func present(_ path: String) -> Bool { field(path) != .null }
        func require(_ condition: Bool, _ message: String) throws {
            guard condition else {
                throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: message, context: ["model": title])
            }
        }
        func ordered(_ paths: [String]) -> Bool {
            let dates = paths.compactMap { timestamp(string($0)) }
            return dates.count == paths.count && zip(dates, dates.dropFirst()).allSatisfy { $0 <= $1 }
        }
        // Mirrors Python RecordingGroup: v1 (the default) embeds `catalog`; v2 carries
        // `catalog_reference` (with the suite purpose) and embeds no prompts to check against.
        func embedsCatalog(_ group: String) -> Bool {
            string(group.isEmpty ? "schema_version" : group + ".schema_version") != recordingGroupV2
        }
        func catalogPurpose(_ group: String) -> String {
            let prefix = group.isEmpty ? "" : group + "."
            return string(prefix + (embedsCatalog(group) ? "catalog.purpose" : "catalog_reference.purpose"))
        }
        switch title {
        case "RecordingError":
            try require((field("details").array ?? []).allSatisfy { item in
                guard let text = item.string else { return false }
                return !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.unicodeScalars.count <= 1024
            }, "Error details must be bounded and nonblank")
        case "PreparedCaptureGate":
            let paths = ["approval_path", "approval_sha256", "verified_at", "evidence_sha256"]
            if string("state") == "ready" { try require(paths.allSatisfy(present), "Ready capture gate requires complete evidence") }
            if string("state") == "not-required" { try require(paths.allSatisfy { !present($0) }, "Unneeded gate cannot carry approval") }
            try require(present("approval_path") == present("approval_sha256"), "Approval path and checksum must be paired")
        case "Prompt":
            // Normalized copies carry declared defaults, so null text/instructions and [] labels mean absent.
            let category = string("category")
            let silent = silentPromptCategories.contains(category)
            let labels = (field("labels").array ?? []).map { $0.string ?? "" }
            try require(!string("style").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                && ["text", "instructions"].allSatisfy { field($0) == .null || !string($0).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty },
                "Prompt text, instructions and style must be nonblank")
            try require(field("text") != .null || silent, "Only room tone and procedure prompts may omit spoken text")
            try require(category != "procedure" || (field("text") == .null && field("instructions") != .null), "Procedure prompts require instructions and no spoken text")
            try require(Set(labels).count == labels.count && reservedPromptLabels.isDisjoint(with: labels), "Prompt labels must be unique and unreserved")
        case "PromptSuite":
            let prompts = field("prompts").array ?? []
            let ids = prompts.map { lookup($0, "prompt_id").string ?? "" }
            try require(Set(ids).count == ids.count, "Catalog prompt identities must be unique")
            try require(prompts.allSatisfy { lookup($0, "holdout") == .boolean(string("purpose") == "held-out-evaluation") }, "Holdout policy must agree with catalog purpose")
            func promptString(_ prompt: RecordingJSON, _ key: String) -> String? { lookup(prompt, key).string }
            func promptLabels(_ prompt: RecordingJSON) -> [String] { (lookup(prompt, "labels").array ?? []).map { $0.string ?? "" } }
            if string("schema_version") != "cole-voice-lab.prompt-suite/v2" {
                // S1 compares arrays by count, never present(), because normalization inserts [].
                try require(string("purpose") != "physical-test" && prompts.allSatisfy {
                    promptString($0, "text") != nil && promptLabels($0).isEmpty && lookup($0, "instructions") == .null
                        && promptString($0, "category") != "procedure"
                }, "Prompt-suite v1 requires text and forbids v2 fields")
            } else {
                let physicalTest = string("purpose") == "physical-test"
                try require(physicalTest == string("suite_id").hasPrefix("cole-physical-test-"), "Physical-test purpose and suite prefix must agree")
                try require(prompts.allSatisfy { promptString($0, "category") != "room-tone" || (lookup($0, "text") == .null && lookup($0, "instructions") != .null) }, "Prompt-suite v2 room tone requires instructions and no text")
                try require(prompts.allSatisfy { prompt in
                    physicalTest
                        ? promptLabels(prompt).contains("test") && lookup(prompt, "reference_candidate") != .boolean(true)
                        : !promptLabels(prompt).contains("test") && promptString(prompt, "category") != "procedure"
                }, "Physical-test labels, references or procedures disagree with catalog purpose")
                try require(prompts.allSatisfy { prompt in
                    ["text", "instructions", "style"].allSatisfy { key in
                        !(promptString(prompt, key) ?? "").unicodeScalars.contains { scalar in
                            scalar.properties.generalCategory == .format || (scalar.properties.generalCategory == .control && scalar != "\n")
                        }
                    }
                }, "Prompt-suite v2 text cannot contain format or control characters")
            }
        case "RecordingGroup":
            if embedsCatalog("") {
                try require(present("catalog") && !present("catalog_reference"), "Recording group v1 embeds its catalog and carries no reference")
            } else {
                try require(present("catalog_reference") && !present("catalog"), "Recording group v2 references its catalog and embeds none")
                try require(field("catalog_reference.catalog_sha256") == field("catalog_sha256"), "Group catalog digest must match its reference")
            }
            try require(string("capture_device.backend") == "coreaudio", "Native group requires Core Audio")
            try require((catalogPurpose("") == "capture-corpus") != (string("gate.state") == "not-required"), "Capture gate purpose mismatch")
            try unique(field("room_tone_take_ids"), title: title)
        case "CatalogReference":
            try require(string("snapshot_path") == "provenance/corpus/snapshots/\(string("catalog_sha256")).json", "Catalog snapshot path must be content-addressed")
        case "CaptureReservation":
            // A v2 group embeds no prompts; the service checks the prompt against its snapshot.
            if embedsCatalog("group") {
                try require((field("group.catalog.prompts").array ?? []).contains { pythonEqual($0, field("prompt")) }, "Reserved prompt must exactly match its catalog")
            }
            try require(ordered(["group.opened_at", "requested_at"]), "Capture request precedes its group")
            try require(catalogPurpose("group") != "capture-corpus" || string("group.gate.state") == "ready", "Corpus capture requires a ready gate")
            try require(string("partial_path") == "tmp/recording-takes/\(string("take_id"))/capture.partial.wav", "Partial path must bind its take")
            try require(string("original_path") == "tmp/recording-takes/\(string("take_id"))/original.wav", "Original path must bind its take")
        case "AudioSpec":
            let widths: [String: Double] = ["pcm_s16le": 2, "pcm_s24le": 3, "pcm_s32le": 4]
            try require(widths[string("encoding")] == number("sample_width_bytes"), "PCM encoding and width disagree")
            let expected = number("frames") / number("sample_rate_hz")
            try require(abs(number("duration_seconds") - expected) <= max(0.001, expected * 0.0001), "Duration disagrees with sample count")
        case "CaptureContinuityEvidence":
            try require(field("success") == .boolean(true) && field("continuity_checked") == .boolean(true), "Continuity receipt cannot claim failed capture")
            try require(field("frames") == field("received_frames"), "Received and written counts disagree")
            try require(abs(number("last_sample_end") - number("first_sample_time") - number("frames")) <= 0.000001, "Timestamp span disagrees with frames")
            try require(!string("output_name").contains("/"), "Output must be a direct filename")
        case "CaptureSealReceipt":
            try require(string("original_path") == "tmp/recording-takes/\(string("take_id"))/original.wav", "Seal path must bind its take")
            try require(number("audio.sample_rate_hz") == 48000 && number("audio.channels") == 1 && number("audio.sample_width_bytes") == 3 && string("audio.encoding") == "pcm_s24le", "Native capture format mismatch")
            try require(number("audio.frames") <= 28_800_000 && number("original_size_bytes") == 44 + number("audio.frames") * 3, "Sealed WAV size/count mismatch")
            try require(ordered(["requested_at", "first_sample_at", "last_sample_at", "sealed_at"]), "Capture timestamps are not ordered")
            try require(field("continuity.frames") == field("audio.frames") && field("continuity.wav_sha256") == field("original_sha256") && field("continuity.wav_size_bytes") == field("original_size_bytes") && string("continuity.output_name") == "original.wav", "Continuity and seal identities disagree")
        case "NativeRecoveryReceipt":
            try require(string("partial_path") == "tmp/recording-takes/\(string("take_id"))/capture.partial.wav", "Recovery path must bind its take")
            try require(number("written_frames") <= number("received_frames"), "Written recovery count exceeds received frames")
            if string("state") == "sealed" {
                try require(present("seal") && !present("error"), "Sealed recovery requires successful seal")
                try require(["take_id", "reservation_sha256", "process_generation"].allSatisfy { field($0) == field("seal." + $0) } && field("seal.audio.frames") == field("written_frames") && field("received_frames") == field("written_frames"), "Recovery identity/count mismatch")
            } else { try require(!present("seal") && present("error"), "Partial recovery requires explicit failure") }
        case "RecordingRecoveryStatus":
            let sealed = ["draft", "kept", "rejected", "removed"].contains(string("status"))
            try require(field("recovered_seal") != .boolean(true) || sealed, "Recovered seal requires a sealed take status")
            try require(!sealed || !present("error"), "Sealed recovery cannot contain an error")
            try require(string("status") != "unavailable" || present("error"), "Unavailable recovery requires an explicit error")
        case "RecordingRecoveryPage":
            let entries = field("entries").array ?? []
            let identities = entries.map { lookup($0, "take_id") }
            try unique(.array(identities), title: title)
        case "RecordingRecoveryFinding":
            try require(present("result") || field("pending") == .boolean(true), "Classified recovery finding requires its result")
            try require(!present("result") || field("result.take_id") == field("take_id"), "Recovery finding result belongs to another take")
        case "RecordingRecoveryInventoryPage":
            let entries = field("entries").array ?? []
            try unique(.array(entries.map { lookup($0, "take_id") }), title: title)
            try require(number("finding_count") >= Double(entries.count), "Inventory count omits visible findings")
            let visiblePending = entries.filter { lookup($0, "pending") == .boolean(true) }.count
            let visibleRecovered = entries.filter { lookup($0, "pending") == .boolean(false) && lookup($0, "result.recovered_seal") == .boolean(true) }.count
            try require(number("pending_count") >= Double(visiblePending) && number("pending_count") <= number("finding_count"), "Inventory pending count contradicts its findings")
            try require(number("recovered_seal_count") >= Double(visibleRecovered) && number("recovered_seal_count") <= number("finding_count") - number("pending_count"), "Inventory recovered count contradicts classified findings")
            try require((string("scan_state") == "error") == present("error"), "Inventory error state requires its named error")
            if string("scan_state") == "complete" {
                try require(field("enumeration_complete") == .boolean(true) && number("pending_count") == 0 && present("covered_store_revision"), "Complete inventory requires classified coverage")
            } else {
                try require(!present("covered_store_revision"), "Unfinished inventory cannot certify store coverage")
            }
        case "RecoveryEvent":
            if present("take_id") {
                let entries = field("page.entries").array ?? []
                try require(entries.count == 1 && entries.first.map { lookup($0, "take_id") } == field("take_id") && !present("page.next_take_id"), "Single-take recovery must return exactly its requested identity")
            }
        case "RecoverCommand":
            try require(!present("take_id") || !present("after_take_id"), "Single-take recovery cannot include a page cursor")
        case "ContinueLegacyRecordingCommand":
            try require(field("operation_id") == field("context.operation_id") && field("context.new_setup") == .boolean(true), "Legacy continuation requires its explicit new native setup")
            try require(present("context.catalog") && !present("context.catalog_reference"), "Legacy continuation binds the session's embedded catalog, not a reference")
        case "RecordingFrameRange":
            try require(number("start_frame") < number("end_frame") && number("end_frame") <= number("source_frames"), "Trim range must be nonempty and within source")
        case "RecordingNavigationResult":
            try require(field("complete") == .boolean(!present("next_prompt")), "Completion and navigation target disagree")
            try require(!present("selected_take_id") || present("next_prompt"), "A selected take requires its resolved prompt")
        case "RecordingSaveOperation":
            try require(field("selection.take_id") == field("take_id"), "Save must identify the displayed draft")
            try require(field("advance") == .boolean(present("navigation")), "Advance requires persisted navigation result")
            if present("navigation") { try require(field("navigation.catalog_sha256") == field("selection.catalog_sha256"), "Save navigation catalog mismatch") }
            try require(!present("navigation.selected_take_id"), "Save and Next cannot select a take")
        case "RecordingAnalysisResult":
            let state = string("state")
            try require(present("completed_at") == !["pending", "running"].contains(state), "Analysis completion timestamp mismatch")
            if present("completed_at") { try require(ordered(["queued_at", "completed_at"]), "Analysis finished before it was queued") }
            if ["verified", "needs-review"].contains(state) {
                try require(present("metrics") && !present("error") && field("metrics.passed") == .boolean(state == "verified"), "Analysis success and findings disagree")
            } else if ["invalid", "failed"].contains(state) { try require(present("error"), "Failed analysis requires a named error") }
            else { try require(!present("metrics") && !present("error"), "Pending analysis cannot claim results") }
        case "RecordingTake":
            if present("seal") {
                try require(field("seal.take_id") == field("reservation.take_id") && field("seal.process_generation") == field("reservation.process_generation") && field("seal.requested_at") == field("reservation.requested_at") && field("seal.continuity.device_uid") == field("reservation.group.capture_device.device_id") && number("seal.audio.frames") <= number("reservation.maximum_frames"), "Take seal differs from its reservation")
            }
            if ["draft", "saving"].contains(string("transport")) { try require(present("seal"), "Draft review requires a sealed source") }
            if string("decision") == "kept" { try require(present("seal") && number("decision_revision") > 0, "Kept take requires durable decision and seal") }
            if present("decision_evidence") {
                try require(field("decision_evidence.current") == field("decision") && field("decision_evidence.revision") == field("decision_revision"), "Take decision must bind its exact operator evidence")
            }
            if string("decision") == "draft" && number("decision_revision") != 0 {
                try require(present("decision_evidence") && string("decision_evidence.action") == "restore", "Only explicit Restore can return a revised take to Draft")
            }
            if present("frame_range") { try require(present("seal") && field("frame_range.source_frames") == field("seal.audio.frames"), "Trim recipe source disagrees with seal") }
            if ["prepared", "accepted-raw"].contains(string("export")) { try require(string("decision") == "kept" && string("analysis") == "verified", "Export requires verified kept take") }
        case "RecordingDecisionEvidence":
            if string("action") == "restore" {
                try require(["rejected", "removed"].contains(string("previous")) && ["draft", "kept"].contains(string("current")) && !present("restore_decision") && !present("reason"), "Restore must recover the retained decision")
            } else {
                let target = string("action") == "reject" ? "rejected" : "removed"
                try require(string("current") == target && present("restore_decision") && present("reason") && !string("reason").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !string("reason").unicodeScalars.contains(where: { $0.value < 32 }), "Exclusion requires a recoverable decision and readable reason")
            }
        case "RecordingReviewSource":
            try require(field("source_identity.relative_path") == field("source_path") && field("frame_range.source_frames") == field("source_frames") && (string("date_precision") == "unknown") == !present("recorded_at"), "Review source must bind its original path, frames and date precision")
        case "ReviewEvent", "ReviewActionEvent":
            try require(field("cursor.subject_id") == field("source.subject_id") && field("cursor.selection.take_id") == field("source.take_id") && field("cursor.selection.prompt_id") == field("source.prompt.prompt_id") && field("cursor.selection.catalog_sha256") == field("source.catalog_sha256"), "Review cursor must select the authorized source")
            if title == "ReviewActionEvent" {
                try require(string("source.original_kind") != "native-original" && (string("state") == "failed") == present("error"), "Legacy action requires its actual source kind and bound failure")
            }
        case "EditEvent":
            try require(present("take.seal") && present("take.frame_range") && number("take.edit_revision") >= 1, "Edited event requires a sealed versioned frame decision")
            let trimmed = number("take.frame_range.start_frame") != 0 || field("take.frame_range.end_frame") != field("take.frame_range.source_frames")
            try require((string("derivative_state") == "pending") == trimmed, "Only a trimmed selection needs a derivative")
        case "DecisionEvent":
            try require(field("take.decision_evidence") == field("decision"), "Decision response must publish its exact committed intent")
        case "DerivativeEvent":
            if string("state") == "ready" {
                try require(present("derived_path") && present("derived_sha256") && !present("error") && string("derived_path") == "derived/recording-edits/native/\(string("recipe_sha256"))/\(string("derived_sha256")).wav", "Derivative output must bind its exact recipe and digest")
            } else { try require(present("error") && !present("derived_path") && !present("derived_sha256"), "Failed derivative cannot claim output") }
        case "PackagePreparationEvent":
            try require(["blocked", "failed"].contains(string("state")) == present("error"), "Blocked or failed package preparation requires its named prerequisite")
        case "RecordingPackageMap":
            let entries = field("entries").array ?? []
            try unique(.array(entries.map { lookup($0, "take_id") }), title: title)
            try unique(.array(entries.map { lookup($0, "package_relative_path") }), title: title)
            let embedded = embedsCatalog("group")
            let catalogPrompts = field("group.catalog.prompts").array ?? []
            try require(entries.allSatisfy { item in
                ["group_id", "subject_id", "catalog_sha256"].allSatisfy { lookup(item, $0) == field("group." + $0) } &&
                (!embedded || catalogPrompts.contains { pythonEqual($0, lookup(item, "prompt")) })
            }, "Package entries must share group and exact catalog")
            // A v2 group embeds no prompts, so its map must carry the exact selected suite.
            try require(embedded || present("selected_suite"), "Package of a catalog-reference group requires its selected suite")
            if present("selected_suite") {
                let entryPrompts = entries.map { lookup($0, "prompt") }
                let selectedIDs = Set(entryPrompts.compactMap { lookup($0, "prompt_id").string })
                let expected = embedded ? catalogPrompts.filter { selectedIDs.contains(lookup($0, "prompt_id").string ?? "") } : entryPrompts
                try require(string("selected_suite.purpose") == catalogPurpose("group")
                            && pythonEqual(field("selected_suite.prompts"), .array(expected))
                            && pythonEqual(.array(entryPrompts), .array(expected)),
                            "Package selected suite must preserve exact catalog purpose, content and order")
            }
            let tones = entries.filter { lookup($0, "prompt.category") == .string("room-tone") }
            try require(!tones.isEmpty && tones.allSatisfy { (field("group.room_tone_take_ids").array ?? []).contains(lookup($0, "take_id")) }, "Package requires explicit compatible room tone")
        case "OpenContextCommand":
            // Mirrors Python OpenContextCommand: a catalog reference, or (legacy continuation
            // only) an embedded catalog with its digest; Python alone recomputes that digest.
            let referenced = present("catalog_reference")
            try require(referenced ? !present("catalog") && !present("catalog_sha256") : present("catalog") && present("catalog_sha256"),
                        "Context names its catalog by reference or embeds it with its digest, never both")
            try require(string("capture_device.backend") == "coreaudio", "Native context requires Core Audio")
            try require(present("pilot_approval_path") == present("pilot_approval_sha256"), "Pilot approval path and digest must be paired")
            let purpose = string(referenced ? "catalog_reference.purpose" : "catalog.purpose")
            try require(!present("pilot_approval_path") || purpose == "capture-corpus", "Pilot approval applies only to corpus capture")
        case "CatalogEvent":
            // The suite digest is Python evidence; its identity must repeat the reference's.
            try require(field("catalog.suite_id") == field("reference.suite_id") && field("catalog.purpose") == field("reference.purpose"),
                        "Catalog event suite id and purpose must match its reference")
        case "ContextEvent":
            try require(field("cursor.subject_id") == field("group.subject_id") && field("cursor.group_id") == field("group.group_id") && field("cursor.selection.catalog_sha256") == field("group.catalog_sha256"), "Context cursor must bind its group, subject and catalog")
            if embedsCatalog("group") {
                try require((field("group.catalog.prompts").array ?? []).contains { pythonEqual(lookup($0, "prompt_id"), field("cursor.selection.prompt_id")) }, "Context cursor prompt must belong to its catalog")
            }
            if present("history") { try require(field("history.catalog_sha256") == field("group.catalog_sha256"), "Context history catalog mismatch") }
        case "NavigationEvent":
            if present("cursor") {
                try require(field("cursor.selection.catalog_sha256") == field("result.catalog_sha256") && field("cursor.complete") == field("result.complete"), "Navigation cursor catalog/completion mismatch")
                // Absent and null both read as null: an unselected target carries no take.
                if present("result.next_prompt") { try require(field("cursor.selection.prompt_id") == field("result.next_prompt.prompt_id") && field("cursor.selection.take_id") == field("result.selected_take_id"), "Navigation cursor must identify its resolved prompt") }
                try require(!present("result.selected_take_id") || string("cursor.mode") == "selected-prompt", "Only explicit prompt selection resolves a selected take")
            } else { try require(!present("result.selected_take_id"), "A selected take requires its committed cursor") }
        case "NavigateCommand":
            try require((string("mode") == "selected-prompt") == present("target_prompt_id"), "Explicit navigation target mismatch")
        case "DecisionCommand":
            try require(["reject", "remove"].contains(string("action")) == present("reason"), "Decision reason does not match action")
            if present("reason") { try require(!string("reason").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, "Decision reason must be nonblank") }
        case "PreparePackageCommand":
            try unique(field("take_ids"), title: title)
            try unique(field("room_tone_take_ids"), title: title)
            try require((field("room_tone_take_ids").array ?? []).allSatisfy { (field("take_ids").array ?? []).contains($0) }, "Room tone must belong to package selection")
        case "RecorderRequest", "ServiceRequest":
            let command = string("data.command")
            let expected: RecordingJSON = switch command {
            case "start": field("data.reservation.take_id")
            case "register-seal": field("data.receipt.take_id")
            case "reserve", "save", "navigate", "retake": field("data.selection.take_id")
            case "stop", "edit", "review-take", "decision", "retry-analysis", "recover": field("data.take_id")
            default: .null
            }
            try require(field("meta.take_id") == expected, "Request metadata take identity mismatch")
            if title == "ServiceRequest" && ["edit", "decision"].contains(command) {
                try require(field("data.selection.take_id") == field("data.take_id"), "Edit and decision must identify the displayed take")
            }
            if title == "RecorderRequest" && command == "start" { try require(field("meta.process_generation") == field("data.reservation.process_generation"), "Start generation mismatch") }
        case "RecorderStateEvent":
            if ["starting", "recording", "stopping", "draft"].contains(string("transport")) { try require(present("take_id"), "Active state requires its take identity") }
            try require(present("take_id") || number("written_frames") == 0, "Frames require a take identity")
            if present("seal") { try require(field("take_id") == field("seal.take_id") && field("written_frames") == field("seal.audio.frames"), "State counts disagree with seal") }
            if string("transport") == "draft" { try require(present("seal"), "Draft state requires successful seal") }
        case "RecordingTransition":
            let edges: [String: Set<String>] = ["ready": ["starting", "error"], "starting": ["recording", "stopping", "error"], "recording": ["stopping", "error"], "stopping": ["draft", "ready", "error"], "draft": ["saving", "ready", "error"], "saving": ["ready", "draft", "error"], "error": ["ready", "draft"]]
            try require(edges[string("previous")]?.contains(string("current")) == true, "Illegal transport transition")
        case "ReservationEvent":
            if present("cursor") { try require(field("cursor.selection.take_id") == field("take.reservation.take_id"), "Reserved cursor must identify its take") }
            try require(!present("take.seal") && string("take.decision") == "draft", "Reserved event requires an unsealed draft")
        case "SaveEvent":
            if present("cursor") {
                try require(field("cursor.operation_id") == field("operation.operation_id") && field("cursor.selection.catalog_sha256") == field("operation.selection.catalog_sha256"), "Saved cursor operation/catalog mismatch")
                if present("operation.navigation.next_prompt") {
                    try require(field("cursor.selection.prompt_id") == field("operation.navigation.next_prompt.prompt_id") && !present("cursor.selection.take_id"), "Saved cursor must publish its sole resolved target")
                } else { try require(field("cursor.selection.take_id") == field("take.reservation.take_id"), "Plain or completed Save must keep its last take visible") }
            }
            try require(field("take.reservation.take_id") == field("operation.take_id") && string("take.decision") == "kept" && field("take.decision_revision") == field("operation.decision_revision"), "Saved event differs from committed decision")
        case "RecordingHistoryEntry":
            try require((string("date_precision") == "unknown") == !present("recorded_at"), "History timestamp precision mismatch")
            try require(present("group_id") || present("legacy_session_id"), "History requires native or legacy identity")
            try require(present("source_sha256") == present("source_frames"), "History source digest and frame count must be paired")
        case "RecordingHistoryPage":
            if present("take_prompt_id") {
                try require((field("takes").array ?? []).allSatisfy { lookup($0, "prompt.prompt_id") == field("take_prompt_id") }, "Filtered history takes must bind the selected exact prompt")
            }
            for (hasMore, cursor) in [("has_more", "next_cursor"), ("has_more_prompts", "next_prompt_cursor"), ("has_more_groups", "next_group_cursor")] {
                try require((field(hasMore) == .null ? .boolean(false) : field(hasMore)) == .boolean(present(cursor)), "History cursor availability mismatch")
            }
            try require((field("takes").array ?? []).allSatisfy { lookup($0, "catalog_sha256") == field("catalog_sha256") }, "History page mixes catalogs")
            try require(string("completeness") == "complete" || !present("missing_prompt_count"), "Incomplete history cannot claim missing coverage")
            try require(number("covered_prompt_count") <= number("total_prompt_count"), "Covered count exceeds catalog prompt count")
        case "RecordingPromptSummary", "RecordingGroupSummary":
            try require(number("kept_count") <= number("take_count"), "Kept count exceeds total take count")
            let dateField = title == "RecordingPromptSummary" ? "latest_recorded_at" : "opened_at"
            try require((string("date_precision") == "unknown") == !present(dateField), "History summary date precision mismatch")
            if title == "RecordingGroupSummary" {
                try require(present("group_id") != present("legacy_session_id"), "Group summary requires one native or legacy identity")
            }
        case "RecordingResponse":
            try require(present("data") != present("error"), "Response requires exactly one result or error")
            if present("data") {
                let expected: RecordingJSON = switch string("data.event") {
                case "recorder-state", "transition", "waveform-cache", "derivative", "recovery": field("data.take_id")
                case "reserved", "take", "saved", "edited", "decision": field("data.take.reservation.take_id")
                case "context", "navigation": field("data.cursor.selection.take_id")
                case "analysis": field("data.result.take_id")
                case "review", "review-action": field("data.source.take_id")
                default: .null
                }
                try require(field("meta.take_id") == expected, "Event metadata take identity mismatch")
            }
        case "WaveformCacheEvent":
            if string("status") == "stored" {
                try require(!present("error"), "Stored waveform cache cannot contain an error")
            } else {
                try require(present("error") && string("error.code") == "RECORDING.WAVEFORM_CACHE_UNAVAILABLE" && field("error.retryable") == .boolean(true), "Missing waveform cache requires an explicit retryable rebuild error")
            }
        case "WaveformBucket":
            try require(number("minimum") <= number("maximum"), "Waveform extrema are inverted")
        case "WaveformSnapshot":
            try require(number("first_frame") <= number("end_frame") && number("end_frame") <= number("written_frames"), "Waveform extends past written frames")
            let span = number("end_frame") - number("first_frame")
            try require(Double((field("buckets").array ?? []).count) == ceil(span / number("bucket_width_frames")), "Waveform buckets do not cover their frame range")
            if present("sample_clock") {
                try require(number("sample_clock.anchor_frame") < number("written_frames"), "Waveform sample clock must anchor an actually written frame")
            }
        case "RecordingTelemetry":
            try require(field("meta.take_id") == field("data.take_id") && field("meta.process_generation") == field("data.process_generation"), "Telemetry identity mismatch")
        default: break
        }
    }

    static func timestamp(_ value: String) -> Date? {
        RecordingTimestamp.parse(value)
    }

    static func relativePath(_ value: String) throws {
        let pieces = value.split(separator: "/", omittingEmptySubsequences: false)
        guard !value.isEmpty, !value.contains("\\"), !value.unicodeScalars.contains(where: { $0.value < 32 }),
              !value.hasPrefix("/"), !pieces.contains(".."), !pieces.contains("."),
              !pieces.contains("") else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Path must be confined and relative")
        }
    }

    /// Python `==` on parsed contract values, for the checks that mirror one (prompt
    /// membership and selected-suite content). Numbers compare by exact value whatever the
    /// token (a Python float field accepts an integer token, and 1 == 1.0); strings compare by
    /// Unicode scalars (Python never applies the canonical equivalence Swift `String ==` does);
    /// arrays compare in order and objects by key and value. Object keys are schema property
    /// names, which the schema has already confined.
    static func pythonEqual(_ lhs: RecordingJSON, _ rhs: RecordingJSON) -> Bool {
        switch (lhs, rhs) {
        case let (.object(left), .object(right)):
            return left.count == right.count && left.allSatisfy { key, value in
                right[key].map { pythonEqual(value, $0) } ?? false
            }
        case let (.array(left), .array(right)):
            return left.count == right.count && zip(left, right).allSatisfy { pythonEqual($0, $1) }
        case let (.string(left), .string(right)):
            return left.unicodeScalars.elementsEqual(right.unicodeScalars)
        case let (.integer(left), .integer(right)):
            return left == right
        case let (.number(left), .number(right)):
            return left == right
        case let (.integer(integer), .number(number)), let (.number(number), .integer(integer)):
            return Int64(exactly: number) == integer
        case let (.boolean(left), .boolean(right)):
            return left == right
        case (.null, .null):
            return true
        default:
            return false
        }
    }

    private static func lookup(_ value: RecordingJSON, _ path: String) -> RecordingJSON {
        path.split(separator: ".").reduce(value) { result, key in result.object?[String(key)] ?? .null }
    }

    private static func unique(_ value: RecordingJSON, title: String) throws {
        let items = value.array ?? []
        for (index, item) in items.enumerated() where items.prefix(index).contains(item) {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Identities must be unique", context: ["model": title])
        }
    }
}
