import Foundation
import RecordingContracts

/// Indexed, prompt-scoped take pages are separate from global library summaries.
@MainActor
extension RecordingWorkspaceModel {
    public var visibleTakeHistory: [RecordingJSON] { takeHistoryPromptID == state.prompt?.id ? takeHistory : [] }
    public func loadTakeHistory(reset: Bool = false) async {
        guard let service, let promptID = state.prompt?.id, let catalogSHA256 else { return }
        if reset {
            takeHistoryGeneration &+= 1; takeHistoryPageCursor = nil
            takeHistoryPreviousCursors.removeAll(); takeHistoryNextCursor = nil
        } else if takeHistoryLoading { return }
        let token = takeHistoryGeneration
        takeHistoryLoading = true; takeHistoryError = nil
        do {
            let response = try result(await service.command(.object([
                "command": .string("history"), "catalog_sha256": .string(catalogSHA256),
                "prompt_id": .string(promptID), "cursor": takeHistoryPageCursor.map(RecordingJSON.string) ?? .null,
                "prompt_cursor": .null, "group_cursor": .null, "page_size": .integer(200),
            ]), takeID: nil))
            guard token == takeHistoryGeneration, state.prompt?.id == promptID,
                  response.data.object?["take_prompt_id"] == .string(promptID) else { return }
            takeHistoryPromptID = promptID
            takeHistory = response.data.object?["takes"]?.array ?? []
            takeHistoryNextCursor = response.data.object?["next_cursor"]?.string
            takeHistoryLoading = false
        } catch {
            if token == takeHistoryGeneration {
                takeHistoryLoading = false; takeHistoryNextCursor = nil
                takeHistoryError = "Take history is unavailable · metadata remains unknown"
                if takeHistoryPageCursor != nil {
                    takeHistoryPageCursor = nil; takeHistoryPreviousCursors.removeAll()
                    await loadTakeHistory(reset: true)
                }
            }
        }
    }
    public func nextTakeHistoryPage() {
        guard !takeHistoryLoading, let next = takeHistoryNextCursor else { return }
        takeHistoryPreviousCursors.append(takeHistoryPageCursor)
        if takeHistoryPreviousCursors.count > 64 { takeHistoryPreviousCursors.removeFirst() }
        takeHistoryPageCursor = next
        Task { [weak self] in await self?.loadTakeHistory() }
    }
    public func previousTakeHistoryPage() {
        guard !takeHistoryLoading, !takeHistoryPreviousCursors.isEmpty else { return }
        takeHistoryPageCursor = takeHistoryPreviousCursors.removeLast()
        Task { [weak self] in await self?.loadTakeHistory() }
    }
}
