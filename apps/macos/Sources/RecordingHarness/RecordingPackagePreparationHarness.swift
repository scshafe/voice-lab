import AppKit
import Combine
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

extension RecordingHarness {
    /// Records the fixture catalog through the actual helper and service, then exercises the
    /// explicit Prepare for lab flow. Python owns every package rule; this proves the flow.
    @MainActor static func packagePreparation(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw packageError("Package harness requires explicit synthetic PCM") }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let model = try RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: backend)
        defer { model.close() }
        // Four real captures, their independent analysis jobs and a package worker subprocess
        // share this host with the rest of the gate; the interactive budget is too tight.
        let observer = ReviewObserver(model, barrierSeconds: 120)
        let evidence = RecordingPackageHarnessEvidence(model: model, directory: directory)
        do {
            try evidence.checkpoint("connect")
            await model.connect()
            try await observer.wait { model.canRecord && model.state.historyCompleteness == "complete" }

            var recorded: [(prompt: String, takeID: String)] = []
            var oneOffWithoutRoomTone = false
            // The first utterance is saved before any room tone exists, so the one-off path and
            // the missing-prerequisite explanation are both exercised on real durable state.
            for prompt in ["package-reading-one", "package-room-tone", "package-reading-two", "package-reading-three"] {
                try evidence.checkpoint("select-prompt:" + prompt)
                model.selectPrompt(prompt)
                try await observer.wait { model.state.prompt?.id == prompt && !model.state.navigationPending }
                try evidence.checkpoint("record:" + prompt)
                model.record()
                try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
                try evidence.checkpoint("stop:" + prompt)
                model.stop()
                try await observer.wait { model.canSave && model.selectedReview != nil && !model.review.loading }
                guard let takeID = model.state.takeID else { throw packageError("Stopped take is missing its identity") }
                try evidence.checkpoint("save:" + prompt)
                model.save(advance: false)
                try await observer.wait { model.state.phase == .ready && !model.state.navigationPending }
                // Save acknowledgment precedes the asynchronous history projection. The package
                // selection uses that projection's kept AND verified eligibility, so await both.
                try evidence.checkpoint("await-saved-projection:" + prompt)
                try await RecordingPackageHarnessSynchronization.waitForEligibleTakes(
                    [takeID], model: model, observer: observer)
                try evidence.checkpoint("saved-projection-ready:" + prompt)
                recorded.append((prompt, takeID))
                if prompt == "package-reading-one" {
                    try evidence.checkpoint("one-off-without-room-tone")
                    model.requestPreparePackage()
                    try await observer.wait { model.packageSheetOpen }
                    oneOffWithoutRoomTone = !model.canSubmitPackage
                        && (model.packageSubmitUnavailableReason?.contains("No room tone") ?? false)
                        && model.canSave == false  // the take is saved, so Save is not offered again
                        && model.state.takeID == takeID
                    model.dismissPackageSheet()
                    try await observer.wait { !model.packageSheetOpen }
                }
            }

            // Scenario 1: a one-off saved with no room tone in its context stays saved, reviewable
            // and playable, and the explicit Prepare flow explains the missing prerequisite.
            // (Checked against durable state captured before the room tone was recorded.)
            guard oneOffWithoutRoomTone else { throw packageError("A one-off saved without room tone was not offered its explanation") }

            // The fixed workspace must not move because a package flow exists.
            let host = NSHostingView(rootView: RecordingWorkspace(model: model))
            host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
            host.appearance = NSAppearance(named: .aqua); host.layoutSubtreeIfNeeded()
            let baseline = measuredRegions(view: host)
            try validateRegions(baseline, size: host.frame.size, focusMode: false, baseline: nil)

            var observed: [String] = []
            var states: [String] = []
            let watcher = model.$packagePreparation.sink { status in
                if let phase = status?.phase.rawValue, states.last != phase { states.append(phase) }
            }
            defer { watcher.cancel() }

            // Scenario 2: a selection the service refuses is explained by the service, in place,
            // and leaves Save, navigation and Record exactly as they were.
            let saveAvailableBefore = model.canSave
            let selectionBefore = model.state.selection
            let promptBefore = model.state.prompt?.id
            let recordedIDs = Set(recorded.map(\.takeID))
            guard recordedIDs.count == recorded.count else { throw packageError("Recorded take identities are not unique") }
            try evidence.checkpoint("await-all-package-eligible")
            try await RecordingPackageHarnessSynchronization.waitForEligibleTakes(
                recordedIDs, model: model, observer: observer)
            model.requestPreparePackage()
            try await observer.wait { model.packageSheetOpen }
            try evidence.checkpoint("package-selection-opened")
            try RecordingPackageHarnessSynchronization.requireFullSelection(recordedIDs, model: model)
            try RecordingPackageHarnessSynchronization.selectRoomToneOnly(recordedIDs, model: model)
            try evidence.checkpoint("submit-room-tone-only")
            model.submitPackagePreparation()
            try evidence.checkpoint("await-blocked")
            try await RecordingPackageHarnessSynchronization.waitForTerminal(
                .blocked, model: model, observer: observer)
            try evidence.checkpoint("blocked-as-expected")
            let blockedCode = model.packagePreparation?.errorCode ?? "none"
            let blockedMessage = model.packagePreparation?.message ?? "none"
            guard blockedMessage.contains("utterance") else {
                throw packageError("Blocked preparation did not name its missing prerequisite: " + blockedMessage)
            }
            let blockedLeftSaveAvailable = model.canSave == saveAvailableBefore
            let blockedLeftSelectionUnchanged = model.state.selection == selectionBefore
                && model.state.prompt?.id == promptBefore
            guard blockedLeftSaveAvailable, blockedLeftSelectionUnchanged, model.canRecord else {
                throw packageError("Blocked preparation changed Save, navigation or Record")
            }
            host.layoutSubtreeIfNeeded()
            try validateRegions(measuredRegions(view: host), size: host.frame.size, focusMode: false, baseline: baseline)

            // Scenario 3: the complete selection prepares exactly one package.
            try RecordingPackageHarnessSynchronization.selectAllRecorded(recordedIDs, model: model)
            observed.removeAll()
            try evidence.checkpoint("submit-full-package")
            model.submitPackagePreparation()
            try evidence.checkpoint("await-prepared")
            try await RecordingPackageHarnessSynchronization.waitForTerminal(
                .prepared, model: model, observer: observer)
            try evidence.checkpoint("prepared-as-expected")
            observed = states
            guard let sessionID = model.packagePreparation?.sessionID,
                  let manifest = model.packagePreparation?.manifestSHA256,
                  model.packagePreparation?.entryCount == recorded.count else {
                throw packageError("Prepared package did not publish its identity")
            }

            // Scenario 3: resubmitting the same durable operation returns the same package.
            try evidence.checkpoint("resubmit-same-package")
            model.submitPackagePreparation()
            try await RecordingPackageHarnessSynchronization.waitForTerminal(
                .prepared, model: model, observer: observer)
            let resubmitSession = model.packagePreparation?.sessionID ?? "unknown"
            guard resubmitSession == sessionID, model.packagePreparation?.manifestSHA256 == manifest else {
                throw packageError("Idempotent resubmit produced a different package")
            }
            model.dismissPackageSheet()
            host.layoutSubtreeIfNeeded()
            try validateRegions(measuredRegions(view: host), size: host.frame.size, focusMode: false, baseline: baseline)
            try render(view: host, size: host.frame.size, scale: 2, destination: directory.appendingPathComponent("package-prepared-light.png"))

            let report: RecordingJSON = .object([
                "suite": .string("package-preparation"),
                "group_id": .string(model.state.groupID ?? "unknown"),
                "take_ids": .object(Dictionary(uniqueKeysWithValues: recorded.map { ($0.prompt, RecordingJSON.string($0.takeID)) })),
                "verified_take_count": .integer(Int64(recorded.count)),
                "blocked_error_code": .string(blockedCode),
                "blocked_message": .string(blockedMessage),
                "one_off_without_room_tone_explained": .boolean(oneOffWithoutRoomTone),
                "blocked_left_save_available": .boolean(blockedLeftSaveAvailable),
                "blocked_left_selection_unchanged": .boolean(blockedLeftSelectionUnchanged),
                "observed_states": .array(observed.map(RecordingJSON.string)),
                "attempts": .integer(Int64(model.packagePreparation?.attempts ?? 0)),
                "session_id": .string(model.packagePreparation?.sessionID ?? "unknown"),
                "manifest_sha256": .string(model.packagePreparation?.manifestSHA256 ?? "unknown"),
                "package_path": .string(model.packagePreparation?.packagePath ?? "unknown"),
                "selected_suite_id": .string(model.packagePreparation?.selectedSuiteID ?? "unknown"),
                "entry_count": .integer(Int64(model.packagePreparation?.entryCount ?? 0)),
                "idempotent_resubmit_session_id": .string(resubmitSession),
                "fixed_geometry": .boolean(true),
                "microphone_opened": .boolean(false),
                "window_shown": .boolean(false),
                "installed_app_restarted": .boolean(false),
            ])
            try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("package-report.json"), options: .atomic)
            try evidence.checkpoint("complete")
            print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
        } catch {
            try evidence.recordFailure(error)
            throw error
        }
    }

    private static func packageError(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.PACKAGE_HARNESS", message: message)
    }
}

/// Synchronizes the harness with the exact projection used by the real package sheet.
@MainActor enum RecordingPackageHarnessSynchronization {
    static func waitForEligibleTakes(_ takeIDs: Set<String>, model: RecordingWorkspaceModel,
                                    observer: ReviewObserver) async throws {
        guard !takeIDs.isEmpty else { throw failure("Package eligibility needs recorded take identities") }
        try await observer.wait {
            let candidates = model.state.history.compactMap(RecordingPackageCandidate.init(row:))
                .filter { ($0.groupID == nil || $0.groupID == model.state.groupID) && takeIDs.contains($0.takeID) }
            return candidates.count == takeIDs.count
                && Set(candidates.map(\.takeID)) == takeIDs
                && candidates.allSatisfy(\.looksEligible)
        }
    }

    static func requireFullSelection(_ takeIDs: Set<String>, model: RecordingWorkspaceModel) throws {
        guard !takeIDs.isEmpty, model.packageCandidates.count == takeIDs.count,
              Set(model.packageCandidates.map(\.takeID)) == takeIDs,
              model.packageCandidates.allSatisfy(\.looksEligible),
              model.packageSelectedTakeIDs == takeIDs else {
            throw failure("Package selection does not match every recorded kept and verified take")
        }
    }

    static func selectRoomToneOnly(_ takeIDs: Set<String>, model: RecordingWorkspaceModel) throws {
        try requireFullSelection(takeIDs, model: model)
        let roomTones = Set(model.packageCandidates.filter(\.isRoomTone).map(\.takeID))
        guard !roomTones.isEmpty, roomTones.count < takeIDs.count else {
            throw failure("Blocked-package scenario needs room tone and an utterance")
        }
        for candidate in model.packageCandidates where !candidate.isRoomTone
            && model.packageSelectedTakeIDs.contains(candidate.takeID) {
            model.togglePackageSelection(candidate.takeID)
        }
        guard model.packageSelectedTakeIDs == roomTones else {
            throw failure("Blocked-package command must select exactly the recorded room tone")
        }
    }

    static func selectAllRecorded(_ takeIDs: Set<String>, model: RecordingWorkspaceModel) throws {
        for candidate in model.packageCandidates where !model.packageSelectedTakeIDs.contains(candidate.takeID) {
            model.togglePackageSelection(candidate.takeID)
        }
        try requireFullSelection(takeIDs, model: model)
    }

    static func waitForTerminal(_ expected: RecordingPackagePreparationPhase,
                                model: RecordingWorkspaceModel, observer: ReviewObserver) async throws {
        try await observer.wait {
            guard let phase = model.packagePreparation?.phase else { return false }
            let terminal: [RecordingPackagePreparationPhase] = [.prepared, .blocked, .failed]
            return terminal.contains(phase)
        }
        guard model.packagePreparation?.phase == expected else {
            throw RecordingContractError(code: "RECORDING.PACKAGE_HARNESS_UNEXPECTED_TERMINAL",
                message: "Package scenario expected \(expected.rawValue), received \(model.packagePreparation?.phase.rawValue ?? "none")")
        }
    }

    private static func failure(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.PACKAGE_HARNESS", message: message)
    }
}

/// Bounded step evidence survives a failed scenario, including its submitted selection.
@MainActor final class RecordingPackageHarnessEvidence {
    private let model: RecordingWorkspaceModel
    private let directory: URL
    private var checkpoints: [RecordingJSON] = []
    private var currentStep = "initializing"

    init(model: RecordingWorkspaceModel, directory: URL) {
        self.model = model; self.directory = directory
    }

    func checkpoint(_ step: String) throws {
        currentStep = step
        try retain(error: nil)
    }

    func recordFailure(_ error: Error) throws { try retain(error: error) }

    private func retain(error: Error?) throws {
        let status = model.packagePreparation
        checkpoints.append(.object([
            "step": .string(currentStep),
            "workspace": ReviewObserver.checkpoint(model, label: currentStep),
            "package_phase": status.map { .string($0.phase.rawValue) } ?? .null,
            "package_operation_id": model.packageOperationIdentity.map(RecordingJSON.string) ?? .null,
            "package_error_code": status?.errorCode.map(RecordingJSON.string) ?? .null,
            "package_message": status?.message.map(RecordingJSON.string) ?? .null,
            "package_selected_take_ids": .array(model.packageSelectedTakeIDs.sorted().map(RecordingJSON.string)),
            "package_candidates": candidates(model.packageCandidates),
            "history_candidates": candidates(model.state.history.compactMap(RecordingPackageCandidate.init(row:))),
            "failure": error.map { .string(String(describing: $0)) } ?? .null,
        ]))
        let report: RecordingJSON = .object([
            "suite": .string("package-preparation-progress"),
            "step": .string(currentStep), "completed": .boolean(currentStep == "complete" && error == nil),
            "failure": error.map { .string(String(describing: $0)) } ?? .null,
            "checkpoints": .array(checkpoints),
        ])
        try report.transportData().write(to: directory.appendingPathComponent("package-progress.json"), options: .atomic)
    }

    private func candidates(_ values: [RecordingPackageCandidate]) -> RecordingJSON {
        .array(values.map { .object([
            "take_id": .string($0.takeID), "decision": .string($0.decision),
            "analysis": .string($0.analysis), "looks_eligible": .boolean($0.looksEligible),
            "is_room_tone": .boolean($0.isRoomTone),
        ]) })
    }
}
