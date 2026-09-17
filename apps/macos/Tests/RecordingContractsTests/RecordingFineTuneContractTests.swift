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
        "FINETUNE.PRETRAINED_MISMATCH", "FINETUNE.REFERENCE_MISSING", "FINETUNE.REQUIREMENTS_MISSING", "FINETUNE.RUNTIME_FAILED",
        "FINETUNE.RUNTIME_UNAVAILABLE", "FINETUNE.RUN_EXISTS", "FINETUNE.SOURCE_CHANGED", "FINETUNE.TRAINING_FAILED",
        "FINETUNE.UNSUPPORTED_TEXT",
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

    private func status(active: [String: RecordingJSON]? = nil, latest: [String: RecordingJSON]? = nil,
                        latestError: RecordingJSON? = nil, configured: Bool = true) -> RecordingJSON {
        var event: [String: RecordingJSON] = [
            "event": .string("fine-tune-status"), "operation_id": .string("op-fine-tune-status"),
            "runtime_configured": .boolean(configured),
            "active": active.map(RecordingJSON.object) ?? .null, "latest_run": latest.map(RecordingJSON.object) ?? .null,
        ]
        if let latestError { event["latest_run_error"] = latestError }
        return .object(event)
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
        #expect(fineTuneCodes.count == 20)
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
