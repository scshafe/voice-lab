import AppKit
import SwiftUI

/// AppKit marker measures the rendered region rather than echoing intended frames.
public final class RecordingRegionMarker: NSView {
    public let regionName: String

    public init(regionName: String) {
        self.regionName = regionName
        super.init(frame: .zero)
        setAccessibilityElement(false)
    }

    required init?(coder: NSCoder) { nil }
}

struct RecordingRegionProbe: NSViewRepresentable {
    let regionName: String

    func makeNSView(context: Context) -> RecordingRegionMarker {
        RecordingRegionMarker(regionName: regionName)
    }

    func updateNSView(_ nsView: RecordingRegionMarker, context: Context) {}
}
