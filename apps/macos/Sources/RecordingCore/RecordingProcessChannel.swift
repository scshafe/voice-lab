import Darwin
import Foundation
import RecordingContracts

/// Canonical inherited-pipe transport. Every queue and pending request is bounded;
/// telemetry coalesces independently from reliable responses and state changes.
public final class RecordingProcessChannel: @unchecked Sendable {
    public let events: AsyncThrowingStream<RecordingWireEnvelope, Error>
    public let telemetry: AsyncThrowingStream<RecordingWireEnvelope, Error>
    private let eventContinuation: AsyncThrowingStream<RecordingWireEnvelope, Error>.Continuation
    private let telemetryContinuation: AsyncThrowingStream<RecordingWireEnvelope, Error>.Continuation
    private let configuration: RecordingProcessConfiguration
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private let visual = Pipe()
    private let lock = NSLock()
    private let writer = DispatchQueue(label: "cole.recording.control-writer")
    private let reader = DispatchQueue(label: "cole.recording.control-reader")
    private let visualReader = DispatchQueue(label: "cole.recording.telemetry-reader")
    private let deadlines = DispatchQueue(label: "cole.recording.request-deadlines")
    private var pending: [String: PendingRequest] = [:]
    private var replays = RecordingRequestReplayCache()
    private var closed = false
    private var resnapshotPending = false
    private var requestCount: UInt64 = 0
    private var queuedRequestCount = 0
    private var lastRevision: Int64 = 0
    private var lastEventSequence: Int64 = 0
    private var eventGuard: RecordingEventGuard
    private let prepareSchema: @Sendable (Data) throws -> RecordingSchemaValidator
    private var requestValidator: RecordingSchemaValidator?
    private var responseValidator: RecordingSchemaValidator?
    private var telemetryValidator: RecordingSchemaValidator?
    private var startAttempted = false // Owned only by the serial writer queue.
    private let maximumPendingRequests = 64

    private struct PendingRequest {
        let command: String
        let token: UUID
        let continuation: CheckedContinuation<RecordingWireEnvelope, Error>
    }

    public convenience init(configuration: RecordingProcessConfiguration) {
        self.init(configuration: configuration, prepareSchema: { try RecordingSchemaValidator(preparing: $0) })
    }

    init(configuration: RecordingProcessConfiguration,
         prepareSchema: @escaping @Sendable (Data) throws -> RecordingSchemaValidator) {
        self.configuration = configuration
        self.prepareSchema = prepareSchema
        self.eventGuard = RecordingEventGuard(generation: configuration.processGeneration)
        let events = AsyncThrowingStream<RecordingWireEnvelope, Error>.makeStream(bufferingPolicy: .bufferingOldest(128))
        self.events = events.stream
        self.eventContinuation = events.continuation
        let telemetry = AsyncThrowingStream<RecordingWireEnvelope, Error>.makeStream(bufferingPolicy: .bufferingNewest(1))
        self.telemetry = telemetry.stream
        self.telemetryContinuation = telemetry.continuation
    }

    /// Launch and handshake on the control executor; no process wait or file hash
    /// executes on the caller's UI actor.
    public func start() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            writer.async { [self] in
                do {
                    guard !startAttempted else {
                        throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Local process channel was already started")
                    }
                    startAttempted = true
                    try configuration.verifyExecutable()
                    let directory = configuration.schemaDirectory
                    requestValidator = try prepareSchema(Data(contentsOf: directory.appendingPathComponent("recording-\(configuration.peer.rawValue)-request.schema.json")))
                    responseValidator = try prepareSchema(Data(contentsOf: directory.appendingPathComponent("recording-response.schema.json")))
                    telemetryValidator = try prepareSchema(Data(contentsOf: directory.appendingPathComponent("recording-telemetry.schema.json")))
                    process.executableURL = configuration.executable
                    process.arguments = configuration.arguments
                    process.environment = configuration.environment
                    process.standardInput = input
                    process.standardOutput = output
                    process.standardError = visual
                    try process.run()
                    guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) == 0 else {
                        throw RecordingContractError(code: "RECORDING.WRITE_FAILED", message: "Failed to configure safe control-pipe writes")
                    }
                    try input.fileHandleForReading.close()
                    try output.fileHandleForWriting.close()
                    try visual.fileHandleForWriting.close()
                    reader.async { [self] in readReliable() }
                    visualReader.async { [self] in readTelemetry() }
                    continuation.resume()
                } catch { fail(error); continuation.resume(throwing: error) }
            }
        }
        let reply = try await request(.object([
            "command": .string("handshake"), "role": .string("ui"),
            "build": configuration.clientBuild, "expected_peer_build": configuration.expectedPeerBuild,
        ]), takeID: nil)
        guard reply.error == .null, reply.data.object?["event"] == .string("handshake"),
              reply.data.object?["build"] == configuration.expectedPeerBuild,
              reply.data.object?["role"] == .string(configuration.peer.rawValue) else {
            let failure = RecordingContractError(code: "RECORDING.BUILD_MISMATCH", message: "Peer handshake did not match its exact role and build")
            fail(failure)
            throw failure
        }
    }

    /// Send one command using a stable request ID. Callers retain durable operation
    /// IDs inside command data when retrying after an uncertain response.
    public func request(_ command: RecordingJSON, takeID: String?, requestID: String? = nil,
                        timeout: TimeInterval = 5) async throws -> RecordingWireEnvelope {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            guard !closed, pending.count + queuedRequestCount < maximumPendingRequests else {
                lock.unlock()
                continuation.resume(throwing: RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Control request queue is closed or full"))
                return
            }
            queuedRequestCount += 1
            lock.unlock()
            writer.async { [self] in
                do {
                    lock.lock()
                    queuedRequestCount -= 1
                    guard !closed, pending.count < maximumPendingRequests else {
                        lock.unlock()
                        throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Control channel is closed or its request bound is full")
                    }
                    requestCount += 1
                    let identifier = requestID ?? "native-request-\(requestCount)"
                    let stateRevision = lastRevision
                    let eventSequence = lastEventSequence
                    guard pending[identifier] == nil else {
                        lock.unlock()
                        throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Request ID is already in flight")
                    }
                    lock.unlock()
                    let value: RecordingJSON = .object([
                        "data": command, "error": .null,
                        "meta": .object([
                            "protocol_version": .string("cole-voice-lab.recording/v1"),
                            "request_id": .string(identifier),
                            "process_generation": .string(configuration.processGeneration),
                            "state_revision": .integer(stateRevision), "event_sequence": .integer(eventSequence),
                            "take_id": takeID.map(RecordingJSON.string) ?? .null,
                        ]),
                    ])
                    let payload = try replays.payload(requestID: requestID, command: command, takeID: takeID, envelope: value)
                    guard let requestValidator else {
                        throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Request schema is not prepared")
                    }
                    _ = try requestValidator.validate(payload)
                    lock.lock()
                    guard !closed else { lock.unlock(); throw RecordingContractError(code: "RECORDING.CANCELLED", message: "Control channel closed before request write") }
                    let token = UUID()
                    pending[identifier] = PendingRequest(command: command.object?["command"]?.string ?? "", token: token, continuation: continuation)
                    lock.unlock()
                    do { try input.fileHandleForWriting.write(contentsOf: RecordingFrameCodec.encode(payload)) }
                    catch { fail(error); return }
                    deadlines.asyncAfter(deadline: .now() + timeout) { [self] in timeoutRequest(identifier, token: token) }
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    /// Closing the inherited control pipe is also the recorder's parent-loss signal.
    public func close() {
        fail(RecordingContractError(code: "RECORDING.CANCELLED", message: "Local process channel was closed"))
    }

    public var processIdentifier: Int32 { process.processIdentifier }

    private func readReliable() {
        var codec = RecordingFrameCodec()
        var pipe = RecordingPipeReader(descriptor: output.fileHandleForReading.fileDescriptor)
        do {
            while let chunk = try pipe.next() {
                // This queue block lasts until EOF. Drain Foundation's schema/JSON
                // temporaries per bounded pipe read instead of retaining them for
                // the lifetime of the connected service or recorder.
                try autoreleasepool {
                    for payload in try codec.append(chunk) { try receive(payload) }
                }
            }
            try codec.finish()
            fail(RecordingContractError(code: "RECORDING.PARENT_LOST", message: "Peer control channel ended"))
        } catch { fail(error) }
    }

    private func readTelemetry() {
        var codec = RecordingFrameCodec(maximumBytes: RecordingFrameCodec.telemetryMaximumBytes)
        var pipe = RecordingPipeReader(descriptor: visual.fileHandleForReading.fileDescriptor)
        do {
            while let chunk = try pipe.next() {
                guard configuration.peer == .recorder else { continue }
                // Coalescing the Swift stream does not release autoreleased decode
                // temporaries on this lifetime-long reader queue block.
                try autoreleasepool {
                    for payload in try codec.append(chunk) {
                        guard let telemetryValidator else {
                            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Telemetry schema is not prepared")
                        }
                        let envelope = try RecordingWireEnvelope.decode(payload, validator: telemetryValidator)
                        guard envelope.meta.process_generation == configuration.processGeneration else { continue }
                        telemetryContinuation.yield(envelope)
                    }
                }
            }
            if configuration.peer == .recorder { try codec.finish() }
            telemetryContinuation.finish()
        } catch { fail(error) }
    }

    private func receive(_ payload: Data) throws {
        guard let responseValidator else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Response schema is not prepared")
        }
        let envelope = try RecordingWireEnvelope.decode(payload, validator: responseValidator)
        lock.lock()
        let request = pending[envelope.meta.request_id]
        let isSnapshot = request?.command == "snapshot" || request?.command == "handshake"
        let disposition: RecordingEventGuard.Disposition
        do { disposition = try eventGuard.receive(envelope.meta, snapshot: isSnapshot) }
        catch { lock.unlock(); throw error }
        if request != nil { pending.removeValue(forKey: envelope.meta.request_id) }
        if request?.command == "snapshot" { resnapshotPending = false }
        if disposition == .apply {
            lastRevision = envelope.meta.state_revision
            lastEventSequence = envelope.meta.event_sequence
        }
        let needsSnapshot = disposition == .snapshotRequired && !resnapshotPending
        if needsSnapshot { resnapshotPending = true }
        lock.unlock()
        request?.continuation.resume(returning: envelope)
        if disposition == .apply {
            if case .dropped = eventContinuation.yield(envelope) {
                throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Reliable state event consumer exceeded its bound")
            }
        }
        if needsSnapshot {
            Task { [self] in
                do { _ = try await requestSnapshot() }
                catch { fail(error) }
            }
        }
    }

    public func requestSnapshot() async throws -> RecordingWireEnvelope {
        try await request(.object(["command": .string("snapshot")]), takeID: nil)
    }

    private func timeoutRequest(_ identifier: String, token: UUID) {
        lock.lock()
        let expired = pending[identifier]?.token == token
        lock.unlock()
        if expired { fail(RecordingContractError(code: "RECORDING.CANCELLED", message: "Local process request exceeded its deadline; channel closed for recovery")) }
    }

    private func fail(_ error: Error) {
        lock.lock()
        if closed { lock.unlock(); return }
        closed = true
        let requests = pending.values
        pending.removeAll()
        lock.unlock()
        let failure: Error
        do { try input.fileHandleForWriting.close(); failure = error }
        catch let closeError {
            failure = RecordingContractError(code: "RECORDING.WRITE_FAILED", message: "Control channel failed and its input pipe could not close", context: ["cause": String(describing: error), "close_error": String(describing: closeError)])
        }
        for request in requests { request.continuation.resume(throwing: failure) }
        eventContinuation.finish(throwing: failure)
        telemetryContinuation.finish(throwing: failure)
    }
}
