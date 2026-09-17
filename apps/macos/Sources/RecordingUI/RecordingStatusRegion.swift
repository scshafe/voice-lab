import Foundation
import RecordingContracts
import RecordingCore

/// What the one-line status region says, where the words came from, and whether it warns.
public struct RecordingStatusRegion: Equatable, Sendable {
    public enum Source: String, Equatable, Sendable { case recording, review, recovery, fineTune }
    public let line: String
    public let source: Source
    public let needsAttention: Bool
    /// The line as VoiceOver reads it: a warning says so first.
    public var spokenLine: String { needsAttention ? "Warning: " + line : line }
}

/// The footer slot that starts a fine-tune, or cancels the one that is queued or running. It is
/// one control whose words change, so a job starting or ending never moves keyboard focus.
public struct RecordingFineTuneSlot: Equatable, Sendable {
    public let title: String
    public let identifier: String
    public let enabled: Bool
    /// Why the control is unavailable, or what it does.
    public let help: String
}

/// The Library header's coverage: prompts with a kept take over the active catalog's prompts.
public struct RecordingLibraryCoverage: Equatable, Sendable {
    public let text: String
    public let spokenText: String
}

extension RecordingLibraryCoverage {
    /// `covered` is nil until the library metadata is complete; digits are grouped for `locale`.
    public init(covered: Int64?, total: Int64, locale: Locale) {
        func number(_ value: Int64) -> String { value.formatted(.number.locale(locale)) }
        if let covered {
            text = number(covered) + "/" + number(total)
            spokenText = "\(number(covered)) of \(number(total)) prompts recorded"
        } else {
            text = "—/" + number(total)
            spokenText = "\(number(total)) prompts; recorded count is loading"
        }
    }
}

@MainActor
extension RecordingWorkspaceModel {
    /// The status region's line. Capture, playback, trim feedback, a recovery supervisor error
    /// and a running recovery scan come first. Then recording errors and warnings always win
    /// over fine-tuning. A job's outcome (model ready, cancelled, failed, refused, supervision
    /// stopped) holds the region for `recordingStatusHold` after the job ends, even when the
    /// recording status changes meanwhile; any other recording status wins for
    /// `recordingStatusHold` after it changes; then an active fine-tune's line; then the outcome
    /// until the recording status next changes or the next job starts. Standing recovery
    /// findings rank below every fine-tune line and above the plain recording status. A run
    /// merely restored from an earlier session is not news.
    public var statusRegion: RecordingStatusRegion {
        let (line, source) = statusRegionSource
        let reviewAttention = review.playback.failure != nil || review.failure != nil || review.fieldError != nil
        let attention = state.errorCode != nil || reviewAttention
            || (source != .fineTune && !recoveryAttentionRows.isEmpty)
            || (source == .fineTune && fineTuneNeedsAttention)
        return RecordingStatusRegion(line: line, source: source, needsAttention: attention)
    }

    private var statusRegionSource: (String, RecordingStatusRegion.Source) {
        if state.showsStop { return (state.status, .recording) }
        if let error = review.playback.failure ?? review.failure { return (error.message, .review) }
        if let error = review.fieldError { return (error, .review) }
        if review.isDirty { return ("Unsaved trim preview · original retained", .review) }
        if review.cacheStatus == "needs-rebuild" {
            return ("Waveform archive unavailable · displayed peaks ready; rebuild available", .review)
        }
        // A capture terminal's own words are never masked by background recovery progress.
        if let notice = captureTerminalNotice, notice == state.status { return (notice, .recording) }
        if let recovery = recoveryProgressLine { return (recovery, .recovery) }
        // What shows when no fine-tune line does: standing findings, else the recording status.
        let recording: (String, RecordingStatusRegion.Source) = recoveryFindingsLine.map { ($0, .recovery) }
            ?? (legacyReviewStatus ?? state.status, .recording)
        guard let fineTuneLine = fineTuneStatusLine, !recordingStatusIsWarning else { return recording }
        let now = statusClock()
        if !fineTune.isActive, let outcome = fineTuneOutcome, now - outcome.at < recordingStatusHold {
            return (fineTuneLine, .fineTune)
        }
        if let change = recordingStatusChange, now - change.at < recordingStatusHold { return recording }
        if fineTune.isActive { return (fineTuneLine, .fineTune) }
        if let outcome = fineTuneOutcome, outcome.sequence > (recordingStatusChange?.sequence ?? 0) {
            return (fineTuneLine, .fineTune)
        }
        return recording
    }

    /// Recording words that must stay visible: an error, a capture that needs recovery, audio to
    /// reconnect, or Record unavailable while the region offers Check again.
    var recordingStatusIsWarning: Bool {
        state.errorCode != nil || state.phase == .recoverableError || audioReconnectRequired
            || (state.phase == .ready && !state.canRecord && !state.unsealedRecheckPending)
    }

    /// The recording status changed: it holds the region for `recordingStatusHold`, and the
    /// region is redrawn when the hold ends so a waiting fine-tune line appears on time.
    func noteRecordingStatusChange() {
        statusSequence &+= 1
        recordingStatusChange = (statusSequence, statusClock())
        statusHoldTask?.cancel()
        let hold = recordingStatusHold
        statusHoldTask = Task { [weak self] in
            do { try await Task.sleep(for: hold) } catch { return }
            self?.objectWillChange.send()
        }
    }

    /// A job ended (or was refused, or supervision stopped): its line holds the region for one
    /// hold period, then shows until the recording status next changes. The region is redrawn
    /// when the hold ends, so a recording status that changed meanwhile appears on time.
    func noteFineTuneOutcome() {
        statusSequence &+= 1
        fineTuneOutcome = (statusSequence, statusClock())
        fineTuneOutcomeHoldTask?.cancel()
        let hold = recordingStatusHold
        fineTuneOutcomeHoldTask = Task { [weak self] in
            do { try await Task.sleep(for: hold) } catch { return }
            self?.objectWillChange.send()
        }
    }

    /// Cancel fine-tune while this app's job is queued or running over a live channel;
    /// otherwise Fine-tune model (disabled with its reason once the channel closed).
    public var fineTuneSlot: RecordingFineTuneSlot {
        if fineTune.isActive && !processChannelFailed {
            return RecordingFineTuneSlot(title: "Cancel fine-tune", identifier: "transport.cancel-fine-tune",
                                         enabled: canCancelFineTune, help: fineTuneCancelReason)
        }
        return RecordingFineTuneSlot(title: "Fine-tune model", identifier: "transport.fine-tune",
                                     enabled: canStartFineTune, help: fineTuneUnavailableReason)
    }

    /// The footer slot's action: cancel the queued or running job, otherwise start one.
    public func activateFineTuneSlot() {
        if fineTune.isActive { cancelFineTune() } else { startFineTune() }
    }

    public var libraryCoverage: RecordingLibraryCoverage {
        state.historyCompleteness == "complete"
            ? RecordingLibraryCoverage(covered: state.coveredCount, total: state.totalCount, locale: .current)
            : RecordingLibraryCoverage(covered: nil, total: Int64(prompts.count), locale: .current)
    }
}
