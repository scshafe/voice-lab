import SwiftUI

/// One-line header chips in effective-label order. Pinned chips always show; later chips that
/// do not fit collapse into a trailing `+N`. Catalog tokens render verbatim, never as Markdown.
public struct RecordingLabelChips: View {
    private let prompt: RecordingPromptPresentation
    public init(prompt: RecordingPromptPresentation) { self.prompt = prompt }

    public var body: some View {
        let tokens = prompt.effectiveLabels
        // Widest row first; the last (pinned chips plus `+N`) is the fallback when nothing fits.
        ViewThatFits(in: .horizontal) {
            ForEach(Array(RecordingPromptPresentation.chipRows(tokens, pinned: prompt.pinnedLabelCount).enumerated()), id: \.offset) { _, titles in
                HStack(spacing: 4) {
                    ForEach(Array(titles.enumerated()), id: \.offset) { _, title in
                        Text(verbatim: title).font(.system(size: 11, weight: .medium)).lineLimit(1).fixedSize()
                            .padding(.horizontal, 6).padding(.vertical, 1)
                            .background(Color.secondary.opacity(0.14), in: Capsule())
                    }
                }.fixedSize()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading).clipped()
        .accessibilityElement(children: .contain)
        .accessibilityLabel(Text(verbatim: "Labels: " + tokens.map(RecordingPromptPresentation.labelTitle).joined(separator: ", ")))
        .accessibilityIdentifier("recording.labels")
    }
}

/// Script card with one layout for every kind: an optional kind banner, a Prompt section only for
/// read-aloud text, and an Instructions section that is always present and never read aloud.
public struct RecordingScriptCard: View {
    private let prompt: RecordingPromptPresentation?
    private let scriptSize: CGFloat
    public init(prompt: RecordingPromptPresentation?, scriptSize: CGFloat) { self.prompt = prompt; self.scriptSize = scriptSize }

    public var body: some View {
        GeometryReader { card in
            VStack(alignment: .leading, spacing: 8) {
                if let banner = prompt?.kindBanner { heading(banner) }
                if let text = prompt?.displayPrompt {
                    // Instructions keep their natural height up to 40% of the card and scroll beyond it;
                    // the Prompt takes the rest, so instruction-less catalogs keep their full reading area.
                    RecordingScriptSplit(share: 0.4, spacing: 8) {
                        VStack(alignment: .leading, spacing: 4) {
                            heading("PROMPT · READ ALOUD")
                            ScrollView {
                                Text(verbatim: text).font(.system(size: scriptSize)).lineSpacing(6)
                                    .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                            }.id(prompt?.id ?? "unselected-script")
                        }
                        .accessibilityElement(children: .contain).accessibilityLabel("Prompt, read aloud")
                        .accessibilityIdentifier("recording.script")
                        instructions(spoken: true)
                    }
                } else {
                    Text(prompt == nil ? "Your selected prompt will appear here." : "No spoken prompt")
                        .font(.system(size: 13)).foregroundStyle(RecordingPalette.supportingText).lineLimit(1)
                    // Without a selected prompt there are no instructions or guidance to head.
                    if prompt != nil { instructions(spoken: false).frame(maxHeight: .infinity, alignment: .topLeading) }
                }
            }
            .padding(16).frame(width: card.size.width, height: card.size.height, alignment: .topLeading)
        }
        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
    }

    private func heading(_ title: String) -> some View {
        Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(RecordingPalette.supportingText).lineLimit(1)
    }

    private func instructions(spoken: Bool) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            heading("INSTRUCTIONS · NOT READ ALOUD")
            if let text = prompt?.displayInstructions {
                let content = Text(verbatim: text).font(.system(size: 17)).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                if spoken {
                    // Short instructions keep their natural height under the Prompt; long ones scroll.
                    ViewThatFits(in: .vertical) { content; ScrollView { content }.id(prompt?.id ?? "unselected-instructions") }
                } else {
                    ScrollView { content }.id(prompt?.id ?? "unselected-instructions").frame(maxHeight: .infinity)
                }
            }
            HStack {
                if let guidance = prompt?.guidance { Text(verbatim: "Guidance: " + guidance).lineLimit(2) }
                Spacer()
                if let seconds = prompt?.expectedSeconds { Text("About \(Int(seconds)) sec").fixedSize() }
            }.font(.system(size: 13)).foregroundStyle(RecordingPalette.supportingText).frame(height: 32, alignment: .leading)
        }
        .accessibilityElement(children: .contain).accessibilityLabel("Instructions, do not read aloud")
        .accessibilityIdentifier("recording.instructions")
    }
}

/// Two stacked sections: the second keeps its natural height up to `share` of the bounds (it scrolls
/// inside that share when longer), and the first takes whatever remains. A plain `maxHeight` frame
/// would always fill its cap, which is why the Prompt needs this layout to keep its reading area.
private struct RecordingScriptSplit: Layout {
    let share: CGFloat
    let spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        proposal.replacingUnspecifiedDimensions()
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard subviews.count == 2 else {
            for subview in subviews { subview.place(at: bounds.origin, anchor: .topLeading, proposal: ProposedViewSize(bounds.size)) }
            return
        }
        let cap = bounds.height * share
        let second = min(subviews[1].sizeThatFits(ProposedViewSize(width: bounds.width, height: cap)).height, cap)
        let first = max(0, bounds.height - second - spacing)
        subviews[0].place(at: bounds.origin, anchor: .topLeading, proposal: ProposedViewSize(width: bounds.width, height: first))
        subviews[1].place(at: CGPoint(x: bounds.minX, y: bounds.minY + first + spacing), anchor: .topLeading,
                          proposal: ProposedViewSize(width: bounds.width, height: second))
    }
}
