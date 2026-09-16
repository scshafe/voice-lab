import AppKit

public enum RecordingKeyboardAction: Equatable, Sendable {
    case recordOrStop, playPause, save(advance: Bool), seek(frames: Int64), retainedStart, retainedEnd
}

/// Keyboard policy is shared by the native monitor and deterministic key-event tests.
public struct RecordingKeyboardContext: Sendable {
    public let editingText: Bool
    public let dialogOrPopup: Bool
    public let waveformFocused: Bool
    public init(editingText: Bool, dialogOrPopup: Bool, waveformFocused: Bool) {
        self.editingText = editingText; self.dialogOrPopup = dialogOrPopup; self.waveformFocused = waveformFocused
    }
}

public enum RecordingKeyboardRouter {
    public static func action(for event: NSEvent, context: RecordingKeyboardContext) -> RecordingKeyboardAction? {
        guard event.type == .keyDown, !context.editingText, !context.dialogOrPopup else { return nil }
        let modifiers = event.modifierFlags.intersection([.command, .option, .control, .shift])
        let text = event.charactersIgnoringModifiers?.lowercased()
        if modifiers == .command, text == "s", !event.isARepeat { return .save(advance: false) }
        guard modifiers.isEmpty || modifiers == .shift else { return nil }
        if !event.isARepeat {
            if text == "r" { return .recordOrStop }
            if text == " " { return .playPause }
            if event.keyCode == 36 || event.keyCode == 76 { return .save(advance: true) }
        }
        guard context.waveformFocused, modifiers.isEmpty else { return nil }
        switch event.keyCode {
        case 123: return .seek(frames: -4_800)
        case 124: return .seek(frames: 4_800)
        case 115: return .retainedStart
        case 119: return .retainedEnd
        default: return nil
        }
    }
}
