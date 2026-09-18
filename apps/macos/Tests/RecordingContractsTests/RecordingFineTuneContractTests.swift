import Foundation
@testable import RecordingContracts
import Testing

/// Fine-tune commands and events against the committed generated schemas: take-less, with
/// named state evidence and run paths relative to the asset root. Mirrors
/// tests/test_recording_contracts.py::finetune_cases.
@Suite struct RecordingFineTuneContractTests {
    private let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    private let runID = "finetune-20260916t080000z"
    private var runRoot: String { "derived/finetune/\(runID)" }
    private let failure = RecordingJSON.object([
        "code": .string("FINETUNE.TRAINING_FAILED"), "message": .string("Synthetic training failure"), "retryable": .boolean(false),
    ])
    /// Mirrors finetune.FineTuneErrorCode, which Python holds equal to the wire ErrorCode's FINETUNE set.
    private let fineTuneCodes = [
        "FINETUNE.CATALOG_HELD_OUT", "FINETUNE.CHECKPOINT_MISSING", "FINETUNE.CORPUS_INCOMPLETE",
        "FINETUNE.CORPUS_PROJECTION_FAILED", "FINETUNE.DOWNLOADS_NOT_ALLOWED", "FINETUNE.DOWNLOAD_FAILED", "FINETUNE.EMPTY_CORPUS",
        "FINETUNE.FFMPEG_FAILED", "FINETUNE.FFMPEG_UNAVAILABLE", "FINETUNE.INTERRUPTED", "FINETUNE.JOB_ACTIVE",
        "FINETUNE.NO_FINISHED_RUN", "FINETUNE.PRETRAINED_MISMATCH", "FINETUNE.REFERENCE_MISSING",
        "FINETUNE.REQUIREMENTS_MISSING", "FINETUNE.RUNTIME_FAILED", "FINETUNE.RUNTIME_UNAVAILABLE",
        "FINETUNE.RUN_ARCHIVED", "FINETUNE.RUN_EXISTS",
        "FINETUNE.SOURCE_CHANGED", "FINETUNE.SYNTHESIS_DEADLINE", "FINETUNE.TRAINING_FAILED", "FINETUNE.UNSUPPORTED_TEXT",
    ]

    private func validator(_ name: String) throws -> RecordingSchemaValidator {
        try RecordingSchemaValidator(preparing: Data(contentsOf: repository.appendingPathComponent("schemas/\(name).schema.json")))
    }

    private func accepts(_ validator: RecordingSchemaValidator, _ value: [String: RecordingJSON]) throws -> Bool {
        do { _ = try validator.validate(RecordingJSON.object(value).transportData()); return true }
        catch is RecordingContractError { return false }
    }

    private func meta(takeID: RecordingJSON) -> RecordingJSON {
        .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("synthetic-request"),
                 "process_generation": .string("synthetic-gen"), "state_revision": .integer(2),
                 "event_sequence": .integer(1), "take_id": takeID])
    }

    private func response(data: RecordingJSON = .null, error: RecordingJSON = .null, takeID: RecordingJSON = .null) -> [String: RecordingJSON] {
        ["data": data, "error": error, "meta": meta(takeID: takeID)]
    }

    private func request(_ data: [String: RecordingJSON], takeID: RecordingJSON = .null) -> [String: RecordingJSON] {
        ["data": .object(data), "error": .null, "meta": meta(takeID: takeID)]
    }

    /// A job in its training stage, pass 30 of 120.
    private func progress(_ changes: [String: RecordingJSON] = [:]) -> [String: RecordingJSON] {
        ["event": .string("fine-tune-progress"), "operation_id": .string("op-fine-tune"), "state": .string("running"),
         "stage": .string("training"), "run_id": .string(runID), "epoch": .integer(30), "epochs": .integer(120),
         "update": .integer(270), "loss": .number(0.52), "eta_seconds": .integer(2520)].merging(changes) { $1 }
    }

    /// A finished run with one fine-tuned and one base sample.
    private func run(_ changes: [String: RecordingJSON] = [:]) -> [String: RecordingJSON] {
        ["run_id": .string(runID), "run_root": .string(runRoot), "status": .string("finished"),
         "started_at": .string("2026-09-16T08:00:00Z"), "finished_at": .string("2026-09-16T10:00:00Z"),
         "epochs": .integer(120), "prompt_count": .integer(34), "retained_seconds": .number(181.25),
         "finetuned_sample_paths": .array([.string("\(runRoot)/samples/finetuned/sample-01.wav")]),
         "base_sample_paths": .array([.string("\(runRoot)/samples/base/sample-01.wav")])].merging(changes) { $1 }
    }

    private var noSamples: [String: RecordingJSON] {
        ["finetuned_sample_paths": .array([]), "base_sample_paths": .array([])]
    }

    private func runEvent(_ summary: [String: RecordingJSON]) -> RecordingJSON {
        .object(["event": .string("fine-tune"), "operation_id": .string("op-fine-tune"), "run": .object(summary)])
    }

    /// A status reply; `finished` defaults to the latest run alone (the service lists it first).
    private func status(active: [String: RecordingJSON]? = nil, latest: [String: RecordingJSON]? = nil,
                        finished: [[String: RecordingJSON]]? = nil, latestError: RecordingJSON? = nil,
                        configured: Bool = true) -> RecordingJSON {
        var event: [String: RecordingJSON] = [
            "event": .string("fine-tune-status"), "operation_id": .string("op-fine-tune-status"),
            "runtime_configured": .boolean(configured),
            "active": active.map(RecordingJSON.object) ?? .null, "latest_run": latest.map(RecordingJSON.object) ?? .null,
            "finished_runs": .array((finished ?? latest.map { [$0] } ?? []).map(RecordingJSON.object)),
        ]
        if let latestError { event["latest_run_error"] = latestError }
        return .object(event)
    }

    /// A finished run `hoursEarlier` than the default one, with its own id and paths.
    private func earlierRun(_ id: String, hoursEarlier: Int) -> [String: RecordingJSON] {
        let root = "derived/finetune/\(id)"
        return run(["run_id": .string(id), "run_root": .string(root),
                    "started_at": .string(String(format: "2026-09-16T%02d:00:00Z", 8 - hoursEarlier)),
                    "finished_at": .string(String(format: "2026-09-16T%02d:00:00Z", 10 - hoursEarlier)),
                    "finetuned_sample_paths": .array([.string("\(root)/samples/finetuned/sample-01.wav")]),
                    "base_sample_paths": .array([.string("\(root)/samples/base/sample-01.wav")])])
    }

    @Test func progressNamesItsStateEvidence() throws {
        let responses = try validator("recording-response")
        let queued: [String: RecordingJSON] = ["event": .string("fine-tune-progress"), "operation_id": .string("op-fine-tune"), "state": .string("queued")]
        let accepted: [(String, [String: RecordingJSON])] = [
            ("training", progress()),
            ("queued", queued),
            ("started before its first stage", queued.merging(["state": .string("running")]) { $1 }),
            ("selecting", queued.merging(["state": .string("running"), "stage": .string("selecting"), "message": .string("Preparing the recordings")]) { $1 }),
            ("finished", progress(["state": .string("finished"), "stage": .null, "epoch": .integer(120)])),
            ("failed", progress(["state": .string("failed"), "error": failure])),
            ("cancelled", progress(["state": .string("cancelled")])),
            ("integer loss", progress(["loss": .integer(1)])),
        ]
        for (name, event) in accepted {
            #expect(try accepts(responses, response(data: .object(event))), "\(name) is valid progress")
        }
        let refused: [(String, [String: RecordingJSON])] = [
            ("training without its pass", progress(["epoch": .null])),
            ("training without its total", progress(["epochs": .null])),
            ("pass without its total", progress(["stage": .string("sampling"), "epochs": .null])),
            ("pass over its total", progress(["epoch": .integer(121)])),
            ("pass zero", progress(["epoch": .integer(0)])),
            ("fractional pass", progress(["epoch": .number(30.5)])),
            ("finished without its run", progress(["state": .string("finished"), "run_id": .null])),
            ("failed without its error", progress(["state": .string("failed")])),
            ("cancelled with an error", progress(["state": .string("cancelled"), "error": failure])),
            ("running with an error", progress(["error": failure])),
            ("queued with a stage", progress(["state": .string("queued")])),
            ("unknown state", progress(["state": .string("paused")])),
            ("unknown stage", progress(["stage": .string("packing")])),
            ("blank message", progress(["message": .string("")])),
        ]
        for (name, event) in refused {
            #expect(try !accepts(responses, response(data: .object(event))), "\(name) must fail closed")
        }
        // Fine-tune messages are take-less.
        #expect(try !accepts(responses, response(data: .object(progress()), takeID: .string("synthetic-take"))))
        // A non-finite loss cannot be sent at all.
        let valid = String(decoding: try RecordingJSON.object(response(data: .object(progress()))).transportData(), as: UTF8.self)
        #expect(valid.contains("\"loss\":0.52"))
        for token in ["Infinity", "NaN", "1e999"] {
            let payload = Data(valid.replacingOccurrences(of: "\"loss\":0.52", with: "\"loss\":\(token)").utf8)
            #expect(throws: (any Error).self, "loss \(token) must fail closed") { _ = try responses.validate(payload) }
        }
    }

    @Test func runSummaryKeepsItsPathsRelativeAndOnlyAFinishedRunHasSamples() throws {
        let responses = try validator("recording-response")
        let accepted: [(String, [String: RecordingJSON])] = [
            ("finished", run()),
            ("finished without samples", run(noSamples)),
            ("running", run(noSamples.merging(["status": .string("running"), "finished_at": .null]) { $1 })),
            ("failed", run(noSamples.merging(["status": .string("failed"), "error": .string("training ended without model_last.pt")]) { $1 })),
            ("archived", run(["location": .string("archived"), "in_vault": .boolean(true)])),
            ("in the vault but still local", run(["in_vault": .boolean(true)])),
        ]
        for (name, summary) in accepted {
            #expect(try accepts(responses, response(data: runEvent(summary))), "\(name) is a valid run")
        }
        let failedWithoutSamples = noSamples.merging(["status": .string("failed")]) { $1 }
        let refused: [(String, [String: RecordingJSON])] = [
            ("absolute run root", run(["run_root": .string("/Users/synthetic/\(runRoot)")])),
            ("parent run root", run(["run_root": .string("derived/../\(runRoot)")])),
            ("absolute sample", run(["finetuned_sample_paths": .array([.string("/\(runRoot)/samples/finetuned/sample-01.wav")])])),
            ("sample outside its run", run(["base_sample_paths": .array([.string("derived/finetune/another-run/samples/base/sample-01.wav")])])),
            ("sample escaping its run", run(["base_sample_paths": .array([.string("\(runRoot)/../another-run/sample-01.wav")])])),
            ("sample that is the run", run(["base_sample_paths": .array([.string(runRoot)])])),
            ("sample in a sibling run", run(["base_sample_paths": .array([.string("\(runRoot)-2/sample-01.wav")])])),
            ("running with a finish time", run(noSamples.merging(["status": .string("running")]) { $1 })),
            ("finished without a finish time", run(["finished_at": .null])),
            ("finished before it started", run(["finished_at": .string("2026-09-16T07:59:59Z")])),
            ("finished with an error", run(["error": .string("Synthetic")])),
            ("failed without an error", run(failedWithoutSamples)),
            ("failed with samples", run(["status": .string("failed"), "error": .string("Synthetic")])),
            ("cancelled status", run(["status": .string("cancelled")])),
            ("no prompts", run(["prompt_count": .integer(0)])),
            ("negative seconds", run(["retained_seconds": .number(-1)])),
            ("absolute runtime path", run(["pretrained_path": .string("/Users/synthetic/pretrained.pt")])),
            ("an unknown location", run(["location": .string("removed")])),
            ("a null location", run(["location": .null])),
            ("in_vault that is not a flag", run(["in_vault": .string("yes")])),
        ]
        for (name, summary) in refused {
            #expect(try !accepts(responses, response(data: runEvent(summary))), "\(name) must fail closed")
        }
    }

    @Test func statusCarriesOnlyTheActiveJobAndTheLatestFinishedRun() throws {
        let responses = try validator("recording-response")
        let queued: [String: RecordingJSON] = ["event": .string("fine-tune-progress"), "operation_id": .string("op-fine-tune"), "state": .string("queued")]
        #expect(try accepts(responses, response(data: status(configured: false))))
        #expect(try accepts(responses, response(data: status(active: progress(), latest: run()))))
        #expect(try accepts(responses, response(data: status(active: queued))))
        // A latest run that cannot be read is named beside the still-reported active job.
        let unreadable = RecordingJSON.object(["code": .string("FINETUNE.SOURCE_CHANGED"),
            "message": .string("the latest fine-tuned model cannot be read: synthetic"), "retryable": .boolean(false)])
        #expect(try accepts(responses, response(data: status(active: progress(), latestError: unreadable))))
        #expect(try accepts(responses, response(data: status(latestError: .null))))
        let failedRun = run(noSamples.merging(["status": .string("failed"), "error": .string("Synthetic")]) { $1 })
        let cancelledCode = RecordingJSON.object(["code": .string("FINETUNE.CANCELLED"), "message": .string("Synthetic"),
                                                  "retryable": .boolean(false)])
        let refused: [(String, RecordingJSON)] = [
            ("finished active job", status(active: progress(["state": .string("finished")]))),
            ("cancelled active job", status(active: progress(["state": .string("cancelled")]))),
            ("failed latest run", status(latest: failedRun)),
            ("invalid active job", status(active: progress(["epoch": .null]))),
            ("latest run and its error", status(latest: run(), latestError: unreadable)),
            ("latest run error with a retired code", status(latestError: cancelledCode)),
            ("latest run error without a message", status(latestError: .object(["code": .string("FINETUNE.SOURCE_CHANGED"),
                                                                                 "retryable": .boolean(false)]))),
        ]
        for (name, event) in refused {
            #expect(try !accepts(responses, response(data: event)), "\(name) must fail closed")
        }
        guard case .object(var unconfigured) = status() else { Issue.record("status is an object"); return }
        unconfigured.removeValue(forKey: "runtime_configured")
        #expect(try !accepts(responses, response(data: .object(unconfigured))))
    }

    @Test func statusListsFinishedRunsNewestFirstWithTheLatestFirst() throws {
        let responses = try validator("recording-response")
        let older = earlierRun("finetune-run-98", hoursEarlier: 1)
        let tied = earlierRun("finetune-run-97", hoursEarlier: 1)
        let listed = [run(), older, tied]
        #expect(try accepts(responses, response(data: status(latest: run(), finished: listed))))
        #expect(try accepts(responses, response(data: status(finished: []))))
        let thirtyTwo = [run()] + (1..<32).map { earlierRun(String(format: "finetune-run-%02d", 99 - $0), hoursEarlier: min($0, 7)) }
        let failed = older.merging(noSamples.merging(["status": .string("failed"), "error": .string("Synthetic")]) { $1 }) { $1 }
        let refused: [(String, RecordingJSON)] = [
            ("tied runs in the wrong order", status(latest: run(), finished: [run(), tied, older])),
            ("a run listed twice", status(latest: run(), finished: [run(), older, older])),
            ("an older run first", status(latest: run(), finished: [older, run()])),
            ("the latest run is not the first", status(latest: older, finished: listed)),
            ("finished runs without the latest", status(finished: listed)),
            ("the latest without finished runs", status(latest: run(), finished: [])),
            ("a failed run listed", status(latest: run(), finished: [run(), failed])),
            ("thirty-three runs", status(latest: run(), finished: thirtyTwo + [earlierRun("finetune-run-00", hoursEarlier: 7)])),
            ("repeated reference prompts", status(latest: run(["reference_prompt_ids": .array([.string("reference-a"), .string("reference-a")])]))),
        ]
        for (name, event) in refused {
            #expect(try !accepts(responses, response(data: event)), "\(name) must fail closed")
        }
    }

    // MARK: Speak

    private let speechOperation = "op-speak"
    private var speechClip: String { "derived/speech/\(speechOperation)/speech-01.wav" }

    /// The latest run spoke two seconds with its reference.
    private func speech(_ changes: [String: RecordingJSON] = [:]) -> [String: RecordingJSON] {
        ["event": .string("synthesis"), "operation_id": .string(speechOperation), "state": .string("finished"),
         "model": .string("latest"), "text": .string("The meeting moved to Thursday."), "run_id": .string(runID),
         "reference_run_id": .string(runID), "reference_prompt_id": .string("reference-neutral-01"),
         "relative_path": .string(speechClip), "sha256": .string(String(repeating: "5", count: 64)),
         "frames": .integer(48_000), "sample_rate_hz": .integer(24_000), "seconds": .number(2)].merging(changes) { $1 }
    }

    private var unresolved: [String: RecordingJSON] {
        ["run_id": .null, "reference_run_id": .null, "reference_prompt_id": .null, "relative_path": .null, "sha256": .null,
         "frames": .null, "sample_rate_hz": .null, "seconds": .null]
    }

    @Test func speakCommandsBoundTheirTextAndSteps() throws {
        let requests = try validator("recording-service-request")
        let speak: [String: RecordingJSON] = ["command": .string("synthesize"), "operation_id": .string(speechOperation),
                                              "text": .string("Hi."), "model": .string("latest")]
        let cancel: [String: RecordingJSON] = ["command": .string("cancel-synthesis"), "operation_id": .string("op-cancel-speak"),
                                               "cancelled_operation_id": .string(speechOperation)]
        let longest = String(repeating: "é", count: 1_999) + "\n"
        for command in [speak, cancel,
                        speak.merging(["model": .string("base"), "reference_prompt_id": .string("reference-neutral-01"),
                                       "nfe_steps": .integer(4), "text": .string("Line one.\nLine two.")]) { $1 },
                        speak.merging(["model": .string(runID), "nfe_steps": .integer(64), "text": .string(longest)]) { $1 }] {
            #expect(try accepts(requests, request(command)))
            #expect(try !accepts(requests, request(command, takeID: .string("synthetic-take"))), "Speak commands are take-less")
        }
        var withoutText = speak
        withoutText.removeValue(forKey: "text")
        var withoutModel = speak
        withoutModel.removeValue(forKey: "model")
        var withoutTarget = cancel
        withoutTarget.removeValue(forKey: "cancelled_operation_id")
        let refused: [(String, [String: RecordingJSON])] = [
            ("empty text", speak.merging(["text": .string("")]) { $1 }),
            ("text over 2,000 characters", speak.merging(["text": .string(String(repeating: "é", count: 2_001))]) { $1 }),
            ("whitespace-only text", speak.merging(["text": .string("  \n \u{3000} ")]) { $1 }),
            ("a control character", speak.merging(["text": .string("Hello\tthere.")]) { $1 }),
            ("a format character", speak.merging(["text": .string("Hello\u{200B}there.")]) { $1 }),
            ("missing text", withoutText),
            ("too few steps", speak.merging(["nfe_steps": .integer(3)]) { $1 }),
            ("too many steps", speak.merging(["nfe_steps": .integer(65)]) { $1 }),
            ("fractional steps", speak.merging(["nfe_steps": .number(32.5)]) { $1 }),
            ("missing model", withoutModel),
            ("unstable model", speak.merging(["model": .string("Latest")]) { $1 }),
            ("unstable reference", speak.merging(["reference_prompt_id": .string("Reference One")]) { $1 }),
            ("unknown field", speak.merging(["voice": .string("cole")]) { $1 }),
            ("cancel without its target", withoutTarget),
        ]
        for (name, command) in refused {
            #expect(try !accepts(requests, request(command)), "\(name) must fail closed")
        }
    }

    @Test func speakEventsNameTheirStateAndAClipUnderTheirOperation() throws {
        let responses = try validator("recording-response")
        let deadline = RecordingJSON.object(["code": .string("FINETUNE.SYNTHESIS_DEADLINE"), "message": .string("Synthetic deadline"),
                                             "retryable": .boolean(true)])
        let running = speech(unresolved.merging(["state": .string("running")]) { $1 })
        let accepted: [(String, [String: RecordingJSON])] = [
            ("queued", speech(unresolved.merging(["state": .string("queued")]) { $1 })),
            ("running", running),
            ("running and resolved", speech(["state": .string("running"), "relative_path": .null, "sha256": .null,
                                             "frames": .null, "sample_rate_hz": .null, "seconds": .null])),
            ("finished with the latest run", speech()),
            ("finished with base", speech(["model": .string("base"), "run_id": .null])),
            ("finished with a chosen run", speech(["model": .string(runID)])),
            ("failed", running.merging(["state": .string("failed"), "error": deadline]) { $1 }),
            ("cancelled", running.merging(["state": .string("cancelled")]) { $1 }),
            ("integer seconds", speech(["frames": .integer(24_000), "seconds": .integer(1)])),
        ]
        for (name, event) in accepted {
            #expect(try accepts(responses, response(data: .object(event))), "\(name) is a valid Speak event")
        }
        let refused: [(String, [String: RecordingJSON])] = [
            ("finished without its path", speech(["relative_path": .null])),
            ("finished without its digest", speech(["sha256": .null])),
            ("finished without its frames", speech(["frames": .null])),
            ("finished without its rate", speech(["sample_rate_hz": .null])),
            ("finished without its seconds", speech(["seconds": .null])),
            ("finished without its reference", speech(["reference_prompt_id": .null])),
            ("finished without its run", speech(["run_id": .null])),
            ("base with a run", speech(["model": .string("base")])),
            ("a chosen run speaking as another", speech(["model": .string("finetune-another-run")])),
            ("a foreign reference run", speech(["reference_run_id": .string("finetune-another-run")])),
            ("a clip of another operation", speech(["relative_path": .string("derived/speech/op-other/speech-01.wav")])),
            ("a clip of a sibling operation", speech(["relative_path": .string("derived/speech/\(speechOperation)-2/speech-01.wav")])),
            ("the operation directory itself", speech(["relative_path": .string("derived/speech/\(speechOperation)")])),
            ("a clip escaping its operation", speech(["relative_path": .string("derived/speech/\(speechOperation)/../op-other/speech-01.wav")])),
            ("an absolute clip", speech(["relative_path": .string("/" + speechClip)])),
            ("a clip in a run", speech(["relative_path": .string("\(runRoot)/speech-01.wav")])),
            ("zero frames", speech(["frames": .integer(0)])),
            ("48 kHz", speech(["sample_rate_hz": .integer(48_000)])),
            ("seconds disagreeing with frames", speech(["seconds": .number(1)])),
            ("a short digest", speech(["sha256": .string(String(repeating: "5", count: 63))])),
            ("running with a clip", speech(["state": .string("running")])),
            ("failed with a clip", speech(["state": .string("failed"), "error": deadline])),
            ("failed without an error", running.merging(["state": .string("failed")]) { $1 }),
            ("cancelled with an error", running.merging(["state": .string("cancelled"), "error": deadline]) { $1 }),
            ("finished with an error", speech(["error": deadline])),
            ("queued and resolved", speech(["state": .string("queued"), "relative_path": .null, "sha256": .null,
                                            "frames": .null, "sample_rate_hz": .null, "seconds": .null])),
            ("unknown state", running.merging(["state": .string("paused")]) { $1 }),
            ("whitespace-only text", speech(["text": .string(" \n ")])),
            ("unstable model", speech(["model": .string("Base")])),
        ]
        for (name, event) in refused {
            #expect(try !accepts(responses, response(data: .object(event))), "\(name) must fail closed")
        }
        #expect(try !accepts(responses, response(data: .object(speech()), takeID: .string("synthetic-take"))), "Speak events are take-less")
    }

    @Test func commandsAreTakeLessAndBounded() throws {
        let requests = try validator("recording-service-request")
        let fineTune: [String: RecordingJSON] = ["command": .string("fine-tune"), "operation_id": .string("op-fine-tune"), "allow_missing": .boolean(true)]
        let cancel: [String: RecordingJSON] = ["command": .string("cancel-fine-tune"), "operation_id": .string("op-cancel-fine-tune"),
                                               "cancelled_operation_id": .string("op-fine-tune")]
        let statusCommand: [String: RecordingJSON] = ["command": .string("fine-tune-status"), "operation_id": .string("op-fine-tune-status")]
        for command in [fineTune, fineTune.merging(["epochs": .integer(12), "allow_missing": .boolean(false)]) { $1 },
                        fineTune.merging(["epochs": .integer(10_000)]) { $1 }, cancel, statusCommand] {
            #expect(try accepts(requests, request(command)))
            #expect(try !accepts(requests, request(command, takeID: .string("synthetic-take"))), "fine-tune commands are take-less")
        }
        var withoutAllowance = fineTune
        withoutAllowance.removeValue(forKey: "allow_missing")
        var withoutTarget = cancel
        withoutTarget.removeValue(forKey: "cancelled_operation_id")
        let refused: [(String, [String: RecordingJSON])] = [
            ("zero epochs", fineTune.merging(["epochs": .integer(0)]) { $1 }),
            ("too many epochs", fineTune.merging(["epochs": .integer(10_001)]) { $1 }),
            ("fractional epochs", fineTune.merging(["epochs": .number(12.5)]) { $1 }),
            ("missing allowance", withoutAllowance),
            ("textual allowance", fineTune.merging(["allow_missing": .string("true")]) { $1 }),
            ("cancel without its target", withoutTarget),
            ("status with arguments", statusCommand.merging(["allow_missing": .boolean(true)]) { $1 }),
            ("retired package preparation", ["command": .string("prepare-package"), "operation_id": .string("op-package"),
                                             "group_id": .string("synthetic-group"), "take_ids": .array([.string("synthetic-take")]),
                                             "room_tone_take_ids": .array([.string("synthetic-take")])]),
        ]
        for (name, command) in refused {
            #expect(try !accepts(requests, request(command)), "\(name) must fail closed")
        }
    }

    @Test func everyFineTuneFailureIsAWireErrorCode() throws {
        let responses = try validator("recording-response")
        #expect(fineTuneCodes.count == 23)
        for code in fineTuneCodes {
            let error = RecordingJSON.object(["code": .string(code), "message": .string("Synthetic"), "retryable": .boolean(false)])
            #expect(try accepts(responses, response(error: error)), "\(code) is a wire error code")
        }
        let unknown = RecordingJSON.object(["code": .string("FINETUNE.UNKNOWN"), "message": .string("Synthetic"), "retryable": .boolean(false)])
        #expect(try !accepts(responses, response(error: unknown)))
        // A cancelled job carries no error, so no code names a cancellation.
        let cancelled = RecordingJSON.object(["code": .string("FINETUNE.CANCELLED"), "message": .string("Synthetic"), "retryable": .boolean(false)])
        #expect(try !accepts(responses, response(error: cancelled)))
        // Package preparation is no longer a service event.
        let retired = RecordingJSON.object(["event": .string("package-preparation"), "operation_id": .string("op-package"),
                                            "group_id": .string("synthetic-group"), "state": .string("queued")])
        #expect(try !accepts(responses, response(data: retired)))
    }
}
