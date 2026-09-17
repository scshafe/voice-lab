import Foundation

/// A pending destructive-to-preview navigation requires one explicit discard/save choice.
public enum RecordingReviewIntent: Equatable, Sendable {
    case selectTake(String), selectPrompt(String), continueMissing, record, retake, decision(String), close
}

public enum RecordingReviewConfirmation: Equatable, Sendable {
    case discardTrim(RecordingReviewIntent)
    case decision(action: String)
    /// Quitting (or closing the window) while a fine-tune is queued or running stops it.
    case quitDuringFineTune
}
