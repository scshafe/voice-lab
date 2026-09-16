import Foundation

/// Named layout tokens; transport state never participates in region sizing.
public enum RecordingLayout {
    public static let inset: CGFloat = 16
    public static let gap: CGFloat = 8
    public static let sidebarWidth: CGFloat = 280
    public static let recordTarget: CGFloat = 56
    public static let scriptTextSize: CGFloat = 24
    public static let supportingTextSize: CGFloat = 13
    public static let minimumWidth: CGFloat = 1060
    public static let minimumHeight: CGFloat = 700

    /// Calculate stable workspace regions for a window and explicit focus choice.
    public static func regions(size: CGSize, focusMode: Bool) -> [String: CGRect] {
        let sidebar = focusMode ? 0 : sidebarWidth
        let x = sidebar + inset
        let width = size.width - x - inset
        let extraHeight = max(0, size.height - minimumHeight)
        let waveformHeight = 120 + extraHeight * 0.3
        let scriptHeight = 256 + extraHeight * 0.7
        var y = inset
        var result: [String: CGRect] = [:]
        if !focusMode {
            result["library"] = CGRect(x: 0, y: 0, width: sidebar, height: size.height)
        }
        for (name, height): (String, CGFloat) in [
            ("header", 48), ("script", scriptHeight), ("take_picker", 24),
            ("waveform", waveformHeight), ("trim", 24), ("timer", 24),
            ("playback", 32), ("footer", recordTarget), ("status", 20),
        ] {
            result[name] = CGRect(x: x, y: y, width: width, height: height)
            y += height + gap
        }
        return result
    }
}
