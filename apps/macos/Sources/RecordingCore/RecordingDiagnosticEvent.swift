import Foundation
import RecordingContracts

/// Optional in-memory evidence only. Callers own bounded storage and off-path serialization.
public typealias RecordingDiagnosticSink = @Sendable (RecordingDiagnosticEvent) -> Void

/// Immutable attribution follows an operation across actors and detached source readers.
public struct RecordingDiagnosticContext: Codable, Sendable, Equatable {
    public let traceID: String
    public let operationID: String?
    public let requestID: String?
    public let preparationID: String?
    public let takeID: String?
    public let sourcePath: String?
    public let sourceSHA256: String?
    public let parentSpanID: String?

    public init(traceID: String, operationID: String? = nil, requestID: String? = nil, preparationID: String? = nil,
                takeID: String? = nil, sourcePath: String? = nil, sourceSHA256: String? = nil,
                parentSpanID: String? = nil) {
        self.traceID = traceID; self.operationID = operationID; self.requestID = requestID; self.preparationID = preparationID
        self.takeID = takeID; self.sourcePath = sourcePath; self.sourceSHA256 = sourceSHA256
        self.parentSpanID = parentSpanID
    }

    public func child(of spanID: String?) -> Self {
        Self(traceID: traceID, operationID: operationID, requestID: requestID, preparationID: preparationID,
             takeID: takeID, sourcePath: sourcePath, sourceSHA256: sourceSHA256, parentSpanID: spanID)
    }

    public func source(_ source: RecordingPlaybackSource, preparationID: String? = nil) -> Self {
        Self(traceID: traceID, operationID: operationID, requestID: requestID, preparationID: preparationID ?? self.preparationID,
             takeID: source.takeID, sourcePath: source.assetRoot.appendingPathComponent(source.relativePath).path,
             sourceSHA256: source.sourceSHA256, parentSpanID: parentSpanID)
    }

    public func path(_ path: String) -> Self {
        Self(traceID: traceID, operationID: operationID, requestID: requestID, preparationID: preparationID,
             takeID: takeID, sourcePath: path, sourceSHA256: sourceSHA256, parentSpanID: parentSpanID)
    }

    public func request(_ requestID: String?, operationID: String? = nil, takeID: String? = nil) -> Self {
        Self(traceID: traceID, operationID: operationID ?? self.operationID, requestID: requestID,
             preparationID: preparationID, takeID: takeID ?? self.takeID,
             sourcePath: sourcePath, sourceSHA256: sourceSHA256, parentSpanID: parentSpanID)
    }
}

public struct RecordingDiagnosticEvent: Codable, Sendable, Equatable {
    public enum Phase: String, Codable, Sendable { case begin, end }
    public enum Outcome: String, Codable, Sendable { case success, failure, cancelled, coalesced, cacheHit = "cache_hit", cacheMiss = "cache_miss", superseded }
    public let spanID: String
    public let name: String
    public let phase: Phase
    public let uptimeNanoseconds: UInt64
    public let context: RecordingDiagnosticContext
    public let outcome: Outcome?
    public let errorCode: String?
    public let byteCount: Int64?
    public let device: Int64?
    public let inode: UInt64?

    public init(spanID: String, name: String, phase: Phase, uptimeNanoseconds: UInt64,
                context: RecordingDiagnosticContext, outcome: Outcome? = nil, errorCode: String? = nil,
                byteCount: Int64? = nil, device: Int64? = nil, inode: UInt64? = nil) {
        self.spanID = spanID; self.name = name; self.phase = phase; self.uptimeNanoseconds = uptimeNanoseconds
        self.context = context; self.outcome = outcome; self.errorCode = errorCode
        self.byteCount = byteCount; self.device = device; self.inode = inode
    }
}

/// A nil sink performs no clock read, UUID creation or event allocation. No per-buffer events.
public struct RecordingDiagnosticSpan: Sendable {
    public let spanID: String?
    public let context: RecordingDiagnosticContext?
    private let name: String
    private let sink: RecordingDiagnosticSink?

    public init(_ name: String, context: RecordingDiagnosticContext? = nil, sink: RecordingDiagnosticSink?) {
        self.name = name; self.sink = sink
        guard let sink else { spanID = nil; self.context = context; return }
        let identifier = UUID().uuidString.lowercased()
        spanID = identifier
        let context = context ?? RecordingDiagnosticContext(traceID: identifier)
        self.context = context
        sink(RecordingDiagnosticEvent(spanID: identifier, name: name, phase: .begin,
            uptimeNanoseconds: DispatchTime.now().uptimeNanoseconds, context: context,
            outcome: nil, errorCode: nil, byteCount: nil, device: nil, inode: nil))
    }

    public var childContext: RecordingDiagnosticContext? { context?.child(of: spanID) }

    public func finish(_ outcome: RecordingDiagnosticEvent.Outcome = .success, error: (any Error)? = nil, errorCode: String? = nil,
                       requestID: String? = nil,
                       byteCount: Int64? = nil, device: Int64? = nil, inode: UInt64? = nil) {
        guard let sink, let spanID, let context else { return }
        sink(RecordingDiagnosticEvent(spanID: spanID, name: name, phase: .end,
            uptimeNanoseconds: DispatchTime.now().uptimeNanoseconds, context: requestID.map { context.request($0) } ?? context,
            outcome: outcome, errorCode: errorCode ?? (error as? RecordingContractError)?.code,
            byteCount: byteCount, device: device, inode: inode))
    }
}
