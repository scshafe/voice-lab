import AppKit
import SwiftUI

/// Small supporting text keeps at least 4.5:1 contrast on native workspace surfaces.
public enum RecordingPalette {
    public static let supportingNSColor = NSColor(name: NSColor.Name("RecordingSupportingText")) { appearance in
        let component: CGFloat = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? 0.72 : 0.36
        return NSColor(srgbRed: component, green: component, blue: component, alpha: 1)
    }
    public static let supportingText = Color(nsColor: supportingNSColor)

    /// WCAG 2 relative luminance in sRGB; nil when the colour cannot be converted.
    public static func relativeLuminance(_ color: NSColor) -> Double? {
        guard let srgb = color.usingColorSpace(.sRGB) else { return nil }
        let linear = [srgb.redComponent, srgb.greenComponent, srgb.blueComponent].map { value -> Double in
            let v = Double(value); return v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
        }
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
    }

    /// Luminance of one rendered pixel, so offscreen snapshots can prove which appearance they drew.
    public static func renderedLuminance(_ bitmap: NSBitmapImageRep, x: Int, y: Int) -> Double? {
        bitmap.colorAt(x: x, y: y).flatMap(relativeLuminance)
    }
}
