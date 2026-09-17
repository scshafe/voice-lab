import Foundation
import RecordingContracts
import RecordingCore

/// Review actions extend the canonical workspace; all durable mutations remain service-owned.
@MainActor
extension RecordingWorkspaceModel {
    public var selectedReview: RecordingReviewSource? {
        review.source?.playback.takeID == state.takeID ? review.source : nil
    }
    public var selectedFrames: Int64? { selectedReview?.playback.frameCount ?? state.sealedFrames }
    public var selectedDecision: String? { selectedReview?.decision ?? state.decision }
    public var canSelectTake: Bool { [.ready, .draftReview].contains(state.phase) && !state.navigationPending && !state.registrationPending }
    public var canEditTrim: Bool { canSelectTake && selectedReview != nil && review.canReview && review.fieldError == nil }
    public var canPlaySelected: Bool { canSelectTake && selectedReview != nil && review.canPlay }

    func prepareCurrentReview(diagnosticContext: RecordingDiagnosticContext? = nil) {
        guard let takeID = state.takeID, canSelectTake else { return }
        reviewTask?.cancel()
        reviewTask = Task { [weak self] in await self?.loadReview(takeID, manual: false, diagnosticContext: diagnosticContext) }
    }
    /// Record is unavailable during a manual load, so only an automatic one can be in flight.
    /// Retire it before capture owns playback: its cancelled source load is not a failure.
    func supersedeAutomaticReview() {
        reviewTask?.cancel(); reviewTask = nil; reviewOperationID = nil
        if review.loading { review.cancelPreparation(generation: review.generation) }
    }
    public func selectTake(_ takeID: String) {
        guard canSelectTake else { return }
        if review.isDirty { reviewConfirmation = .discardTrim(.selectTake(takeID)); return }
        reviewTask?.cancel()
        reviewTask = Task { [weak self] in await self?.loadReview(takeID, manual: true) }
    }
    private func loadReview(_ takeID: String, manual: Bool, diagnosticContext: RecordingDiagnosticContext? = nil) async {
        guard let request = reviewTransport(), let assetRoot = reviewAssetRoot else { return }
        let operation = newOperation("review"); reviewOperationID = operation
        let context = diagnosticSink == nil ? nil : (diagnosticContext ?? RecordingDiagnosticContext(traceID: operation))
            .request(nil, operationID: operation, takeID: takeID)
        let span = RecordingDiagnosticSpan("workspace.review", context: context, sink: diagnosticSink)
        let selection = state.selection
        if manual {
            var updated = state; updated.navigationPending = true; updated.activeOperationID = operation
            updated.status = "Loading selected take…"; publish(updated)
        }
        do {
            if manual { try await review.playback.shutdown() }
            let response = try result(await request(takeID, selection, operation, span.childContext))
            guard reviewOperationID == operation, state.selection == selection,
                  let raw = response.data.object?["source"], let cursor = response.data.object?["cursor"] else { span.finish(.superseded, requestID: response.meta.request_id); return }
            let parse = RecordingDiagnosticSpan("review.source_parse", context: span.childContext?.request(response.meta.request_id), sink: diagnosticSink)
            let selected: RecordingReviewSource
            do { selected = try RecordingReviewSource(raw, assetRoot: assetRoot); parse.finish() }
            catch { parse.finish(.failure, error: error); throw error }
            let preparationGeneration = review.generation &+ 1
            let prepared = try await review.prepare(selected, processGeneration: recorderGeneration,
                diagnosticContext: span.childContext?.request(response.meta.request_id))
            guard reviewOperationID == operation, state.selection == selection,
                  !state.showsStop, state.phase != .saving else {
                review.cancelPreparation(generation: preparationGeneration); span.finish(.superseded, requestID: response.meta.request_id); return
            }
            if selected.originalKind == "native-original", state.take.object?["reservation"]?.object?["take_id"] == .string(takeID),
               state.take.object?["decision_revision"] != .integer(selected.decisionRevision) || state.take.object?["edit_revision"] != .integer(selected.editRevision) {
                review.cancelPreparation(generation: preparationGeneration)
                span.finish(.superseded, requestID: response.meta.request_id)
                prepareCurrentReview(diagnosticContext: span.childContext); return
            }
            let application = RecordingDiagnosticSpan("workspace.review_application", context: prepared.diagnosticContext, sink: diagnosticSink)
            do {
                try applyCursor(cursor, preserveTake: true) { updated in
                    updated.navigationPending = false
                    if manual { updated.activeOperationID = nil }
                    updated.phase = selected.decision == "draft" ? .draftReview : .ready
                    updated.status = selected.decision == "draft" ? "Draft · ready to review" : "Selected take · \(selected.analysis)"
                }
                application.finish()
            } catch { application.finish(.failure, error: error); throw error }
            try review.commit(prepared, waveform: waveform)
            span.finish(requestID: response.meta.request_id)
        } catch {
            span.finish(error is CancellationError ? .cancelled : .failure, error: error)
            guard reviewOperationID == operation, !Task.isCancelled else { return }
            review.report(error)
            if manual {
                var updated = state; updated.navigationPending = false; publish(updated)
                fail(error, phase: .recoverableError)
            }
        }
    }

    private func reviewTransport() -> (@MainActor (String, RecordingJSON, String, RecordingDiagnosticContext?) async throws -> RecordingWireEnvelope)? {
        if let reviewRequest { return reviewRequest }
        guard let service else { return nil }
        return { takeID, selection, operation, context in try await service.review(takeID: takeID, selection: selection, operationID: operation, diagnosticContext: context) }
    }

    public func playPauseSelected() {
        guard canPlaySelected else { return }
        Task { [weak self] in
            guard let self else { return }
            do { try await review.playback.playPause() } catch { review.report(error) }
        }
    }
    public func seekSelected(to frame: Int64) {
        guard canPlaySelected else { return }
        Task { [weak self] in
            guard let self else { return }
            do { try await review.playback.seek(to: frame) } catch { review.report(error) }
        }
    }
    public func skipSelected(frames: Int64) {
        guard canPlaySelected else { return }
        Task { [weak self] in
            guard let self else { return }
            do { try await review.playback.skip(frames: frames) } catch { review.report(error) }
        }
    }
    public func recoverPlayback() {
        guard !state.showsStop else { return }
        Task { [weak self] in
            guard let self else { return }
            do { try await review.playback.recover(); prepareCurrentReview() } catch { review.report(error) }
        }
    }

    /// Save only an explicit frame-range intent. Python owns its recipe digest and derivative job.
    public func saveTrim(thenKeep: Bool = false, advance: Bool = false) {
        guard canEditTrim, let selected = selectedReview, let service, let takeID = state.takeID else { return }
        if !review.isDirty { if thenKeep { save(advance: advance) }; return }
        let operation = newOperation("edit"); let selection = state.selection
        if selected.originalKind != "native-original" { legacyReviewOperation = (operation, false); deferredLegacyReviewEvent = nil }
        let desired = review.trimRange
        var updated = state; updated.phase = .saving; updated.activeOperationID = operation; updated.status = "Saving trim…"; publish(updated)
        Task { [weak self] in
            guard let self else { return }
            do {
                try await review.playback.shutdown()
                let response = try result(await service.edit(takeID: takeID, selection: selection,
                    operationID: operation, sourceSHA256: selected.playback.sourceSHA256,
                    editRevision: selected.editRevision, frameRange: selected.frameRange(desired)))
                guard state.activeOperationID == operation, state.selection == selection,
                      selectedReview?.playback.sourceSHA256 == selected.playback.sourceSHA256 else { return }
                try applyReviewMutation(response.data, previous: selected)
                updated = state; updated.phase = selectedDecision == "draft" ? .draftReview : .ready
                updated.activeOperationID = nil; updated.status = response.data.object?["event"] == .string("review-action") ? legacyActionStatus(response.data) : "Trim saved · checking"; publish(updated)
                await refreshHistory()
                if thenKeep { save(advance: advance) }
                if let intent = reviewAfterSaveIntent { reviewAfterSaveIntent = nil; performReviewIntent(intent) }
            } catch { fail(error, phase: selected.decision == "draft" ? .draftReview : .ready) }
        }
    }
    public func resetTrim() {
        guard canEditTrim else { return }
        review.resetPreview(); saveTrim()
    }

    public func requestRetake() {
        guard canSelectTake, state.takeID != nil else { return }
        if review.isDirty { reviewConfirmation = .discardTrim(.retake); return }
        let selection = state.selection; let operation = newOperation("retake")
        guard let service else { return }
        var updated = state; updated.navigationPending = true; updated.activeOperationID = operation
        updated.status = "Preparing another take…"; publish(updated)
        Task { [weak self] in
            guard let self else { return }
            do {
                try await review.playback.shutdown()
                let response = try result(await service.retake(selection: selection, operationID: operation))
                guard state.activeOperationID == operation, state.selection == selection, let cursor = response.data.object?["cursor"] else { return }
                try applyCursor(cursor, preserveTake: false) { updated in
                    updated.phase = .ready; updated.navigationPending = false; updated.activeOperationID = nil
                    updated.status = "Previous take retained · ready for another take"
                }
                await refreshHistory()
            } catch {
                updated = state; updated.navigationPending = false; publish(updated)
                fail(error, phase: .recoverableError)
            }
        }
    }

    public func requestDecision(_ action: String) {
        guard canSelectTake, selectedReview != nil, ["remove", "reject", "restore"].contains(action) else { return }
        if review.isDirty { reviewConfirmation = .discardTrim(.decision(action)); return }
        if action == "restore" { commitDecision(action, reason: nil) }
        else {
            decisionReason = action == "remove" ? "Removed from use" : "Rejected after review"
            reviewConfirmation = .decision(action: action)
        }
    }
    public func confirmDecision() {
        guard case .decision(let action) = reviewConfirmation, !decisionReason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        reviewConfirmation = nil; commitDecision(action, reason: decisionReason)
    }
    private func commitDecision(_ action: String, reason: String?) {
        guard let selected = selectedReview, let service, let takeID = state.takeID else { return }
        let selection = state.selection; let operation = newOperation(action)
        if selected.originalKind != "native-original" { legacyReviewOperation = (operation, false); deferredLegacyReviewEvent = nil }
        var updated = state; updated.navigationPending = true; updated.activeOperationID = operation; publish(updated)
        Task { [weak self] in
            guard let self else { return }
            do {
                try await review.playback.shutdown()
                let response = try result(await service.decision(takeID: takeID, selection: selection,
                    operationID: operation, decisionRevision: selected.decisionRevision, action: action, reason: reason))
                guard state.activeOperationID == operation, state.selection == selection else { return }
                try applyReviewMutation(response.data, previous: selected)
                updated = state; updated.navigationPending = false; updated.activeOperationID = nil
                updated.phase = selectedDecision == "draft" ? .draftReview : .ready
                updated.status = response.data.object?["event"] == .string("review-action") ? legacyActionStatus(response.data) : (action == "restore" ? "Take restored" : "Take \(action == "remove" ? "removed" : "rejected") · available to restore")
                publish(updated); await refreshHistory()
            } catch {
                updated = state; updated.navigationPending = false; publish(updated)
                fail(error, phase: selected.decision == "draft" ? .draftReview : .ready)
            }
        }
    }

    func applyReviewMutation(_ event: RecordingJSON, previous: RecordingReviewSource) throws {
        guard let configuration else { return }
        if event.object?["event"] == .string("review-action"), let active = legacyReviewOperation,
           active.id == event.object?["operation_id"]?.string, active.terminal, event.object?["state"] == .string("pending") { return }
        let authorized: RecordingReviewSource
        if let raw = event.object?["source"] {
            authorized = try RecordingReviewSource(raw, assetRoot: configuration.assetRoot)
        } else if let take = event.object?["take"], var raw = previous.raw.object {
            for key in ["decision", "decision_revision", "edit_revision", "analysis", "export", "frame_range"] {
                guard let value = take.object?[key] else { throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Review mutation omitted canonical take state") }
                raw[key] = value
            }
            if let recipe = event.object?["recipe_sha256"] { raw["edit_recipe_sha256"] = recipe }
            authorized = try RecordingReviewSource(.object(raw), assetRoot: configuration.assetRoot)
            var updated = state; updated.take = take; publish(updated)
        } else { throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Review mutation omitted its canonical source or take") }
        if event.object?["event"] == .string("review-action"), let operation = event.object?["operation_id"]?.string {
            legacyReviewOperation = (operation, event.object?["state"] != .string("pending"))
            legacyReviewStatus = legacyActionStatus(event)
        }
        if let derivative = event.object?["derivative_state"]?.string { reviewDerivativeState = derivative }
        try review.updateAuthorization(authorized)
        if let deferred = deferredDerivativeEvent { deferredDerivativeEvent = nil; handleDerivative(deferred) }
        if let deferred = deferredLegacyReviewEvent {
            deferredLegacyReviewEvent = nil; handleLegacyReviewAction(deferred)
        }
    }

    func legacyActionStatus(_ event: RecordingJSON) -> String {
        switch event.object?["state"]?.string {
        case "pending": "Legacy action saved · preparing compatibility evidence"
        case "completed": "Legacy action completed · original retained"
        default: event.object?["error"]?.object?["message"]?.string ?? "Legacy action failed · original retained"
        }
    }
    func handleLegacyReviewAction(_ event: RecordingJSON) {
        if let operation = event.object?["operation_id"]?.string, state.activeOperationID == operation,
           legacyReviewOperation?.id == operation, event.object?["state"] != .string("pending"),
           let selected = selectedReview, let raw = event.object?["source"],
           raw.object?["take_id"] == .string(selected.playback.takeID),
           raw.object?["source_sha256"] == .string(selected.playback.sourceSHA256),
           raw.object?["edit_revision"] != .integer(selected.editRevision) || raw.object?["decision_revision"] != .integer(selected.decisionRevision) {
            deferredLegacyReviewEvent = event; return
        }
        guard let operation = event.object?["operation_id"]?.string,
              let active = legacyReviewOperation, active.id == operation, !active.terminal,
              let selected = selectedReview, let raw = event.object?["source"],
              raw.object?["take_id"] == .string(selected.playback.takeID),
              raw.object?["source_sha256"] == .string(selected.playback.sourceSHA256),
              raw.object?["edit_revision"] == .integer(selected.editRevision),
              raw.object?["decision_revision"] == .integer(selected.decisionRevision),
              event.object?["cursor"]?.object?["selection"] == state.selection else { return }
        do {
            try applyReviewMutation(event, previous: selected)
            var updated = state; updated.status = legacyActionStatus(event); publish(updated)
            Task { [weak self] in await self?.refreshHistory() }
        } catch { review.report(error) }
    }

    func handleDerivative(_ event: RecordingJSON) {
        if state.phase == .saving, state.activeOperationID?.hasPrefix("edit-") == true,
           let selected = selectedReview, event.object?["take_id"] == .string(selected.playback.takeID),
           event.object?["source_sha256"] == .string(selected.playback.sourceSHA256), selected.editRevision < Int64.max,
           event.object?["edit_revision"] == .integer(selected.editRevision + 1) {
            deferredDerivativeEvent = event; return
        }
        guard let source = selectedReview, event.object?["take_id"] == .string(source.playback.takeID),
              event.object?["source_sha256"] == .string(source.playback.sourceSHA256),
              event.object?["edit_revision"] == .integer(source.editRevision),
              event.object?["recipe_sha256"] == source.raw.object?["edit_recipe_sha256"] else { return }
        reviewDerivativeState = event.object?["state"]?.string
        var updated = state
        updated.status = event.object?["state"] == .string("ready") ? "Trim preview ready" : "Trim preview unavailable · original retained"
        publish(updated)
    }

    public func cancelReviewConfirmation() {
        if case .discardTrim(.close) = reviewConfirmation { closeCancellationHandler?(); closeCancellationHandler = nil }
        if case .quitDuringFineTune = reviewConfirmation { closeCancellationHandler?(); closeCancellationHandler = nil }
        reviewConfirmation = nil
    }
    public func saveTrimAndContinue() {
        guard case .discardTrim(let intent) = reviewConfirmation else { return }
        reviewConfirmation = nil; reviewAfterSaveIntent = intent; saveTrim()
    }
    public func discardTrimAndContinue() {
        guard case .discardTrim(let intent) = reviewConfirmation else { return }
        reviewConfirmation = nil; review.discardPreview(); performReviewIntent(intent)
    }
    private func performReviewIntent(_ intent: RecordingReviewIntent) {
        switch intent {
        case .selectTake(let take): selectTake(take)
        case .selectPrompt(let prompt): selectPrompt(prompt)
        case .continueMissing: continueMissing()
        case .record: record()
        case .retake: requestRetake()
        case .decision(let action): requestDecision(action)
        case .close: requestClose()
        }
    }
    /// Quit or close: an unsaved trim is decided first, then a queued or running fine-tune
    /// (which quitting stops) is confirmed; Keep running cancels the quit.
    public func requestClose() { requestClose(stoppingFineTune: false) }

    /// The quit confirmation's Quit and stop fine-tune.
    public func quitAndStopFineTune() {
        guard case .quitDuringFineTune = reviewConfirmation else { return }
        reviewConfirmation = nil
        requestClose(stoppingFineTune: true)
    }

    private func requestClose(stoppingFineTune: Bool) {
        if review.isDirty { reviewConfirmation = .discardTrim(.close); return }
        if !stoppingFineTune, fineTune.isActive, !processChannelFailed {
            reviewConfirmation = .quitDuringFineTune
            return
        }
        Task { [weak self] in
            guard let self else { return }
            do { try await review.playback.shutdown(); close(); closeHandler?(); closeCancellationHandler = nil }
            catch { closeCancellationHandler?(); closeCancellationHandler = nil; review.report(error) }
        }
    }

    public func handleKeyboard(_ action: RecordingKeyboardAction) {
        // Transport keys stay inert behind an explicit modal choice.
        guard reviewConfirmation == nil else { return }
        switch action {
        case .recordOrStop: if state.canStop { stop() } else { record() }
        case .playPause: playPauseSelected()
        case .save(let advance):
            let chosenAdvance = advance && saveAdvances
            if review.isDirty { saveTrim(thenKeep: selectedDecision == "draft", advance: chosenAdvance) }
            else { save(advance: chosenAdvance) }
        case .seek(let frames): skipSelected(frames: frames)
        case .retainedStart: seekSelected(to: review.trimRange.lowerBound)
        case .retainedEnd: seekSelected(to: review.trimRange.upperBound)
        }
    }
}
