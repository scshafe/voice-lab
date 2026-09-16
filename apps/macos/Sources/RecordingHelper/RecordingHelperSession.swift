import CaptureBuffer
import Darwin
import Foundation
import RecordingContracts
import RecordingCore

/// Recorder control and capture are independent executors. The stdin reader can
/// deliver Stop while the ordered writer, sealing, or reliable output is busy.
final class RecordingHelperSession: @unchecked Sendable {
    private let configuration: RecordingHelperConfiguration
    private let controller: NativeCaptureController
    private let stateLock = NSLock()
    private let control = DispatchQueue(label: "cole.recorder.helper-control")
    private let completion = DispatchQueue(label: "cole.recorder.helper-completion")
    private let outputQueue = DispatchQueue(label: "cole.recorder.helper-reliable-output")
    private let visualQueue = DispatchQueue(label: "cole.recorder.helper-telemetry")
    private let active = DispatchGroup()
    private let waveformArchives = RecordingWaveformArchiveQueue(maximumPending: 2)
    private let requestSchema: Data
    private let responseSchema: Data
    private let telemetrySchema: Data
    private let telemetryWriter: RecordingTelemetryWriter
    private var timer: DispatchSourceTimer?
    private var handshaken = false
    private var phase = "ready"
    private var takeID: String?
    private var startOperationID: String?
    private var startCommand: RecordingJSON?
    private var startRequestID = "recorder-state"
    private var seal: RecordingJSON?
    private var ownership: NativeCaptureOwnership?
    private var revision: Int64 = 0
    private var sequence: Int64 = 0
    private var telemetrySequence: Int64 = 0
    private var completedTelemetryGeneration: UInt64? // visual executor only
    private var captureGeneration: UInt64 = 0
    private var stopDuringStartup = false
    private var closed = false
    private var terminationScheduled = false
    private var queuedCommands = 0
    private var pendingReliableBytes = 0

    init(configuration: RecordingHelperConfiguration) throws {
        self.configuration = configuration
        if let pcm = configuration.syntheticPCM {
            let barrier = try configuration.syntheticSealBarrier.map { try RecordingSyntheticSealBarrier(path: $0) }
            let startBarrier = try configuration.syntheticStartBarrier.map { try RecordingSyntheticSealBarrier(path: $0, name: "start") }
            self.controller = NativeCaptureController(backendFactory: {
                let backend = SyntheticCaptureBackend(pcm: pcm, chunkFrames: configuration.syntheticChunkFrames, totalFrames: configuration.syntheticTotalFrames)
                if let startBarrier { backend.setStartHook { try startBarrier.waitOnFirstCall() } }
                return backend
            }, writeHook: { point in if point == .finalHeader { try barrier?.wait() } })
        } else { self.controller = NativeCaptureController() }
        self.requestSchema = try Data(contentsOf: configuration.schemaDirectory.appendingPathComponent("recording-recorder-request.schema.json"))
        self.responseSchema = try Data(contentsOf: configuration.schemaDirectory.appendingPathComponent("recording-response.schema.json"))
        self.telemetrySchema = try Data(contentsOf: configuration.schemaDirectory.appendingPathComponent("recording-telemetry.schema.json"))
        self.telemetryWriter = try RecordingTelemetryWriter(descriptor: STDERR_FILENO)
        guard fcntl(STDOUT_FILENO, F_SETNOSIGPIPE, 1) == 0 else {
            throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Could not configure reliable output pipe")
        }
        let timer = DispatchSource.makeTimerSource(queue: visualQueue)
        timer.schedule(deadline: .now(), repeating: .nanoseconds(16_666_667), leeway: .milliseconds(2))
        timer.setEventHandler { [weak self] in self?.publishProgress() }
        self.timer = timer
        timer.resume()
    }

    func receive(_ payload: Data) throws {
        let request = try RecordingWireEnvelope.decode(payload, schema: requestSchema)
        guard request.meta.process_generation == configuration.generation else {
            respond(requestID: request.meta.request_id, event: nil, takeID: request.meta.take_id,
                    error: NativeCaptureFailure("RECORDING.STALE_GENERATION", "Recorder request belongs to another generation"))
            return
        }
        if request.data.object?["command"] == .string("stop") {
            stateLock.lock()
            let allowed = handshaken && request.meta.take_id == takeID && ["starting", "recording", "stopping"].contains(phase)
            if allowed { stopDuringStartup = true }
            stateLock.unlock()
            if allowed { controller.requestStop() }
        }
        stateLock.lock()
        guard queuedCommands < 64, !closed else {
            stateLock.unlock()
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Recorder control queue is closed or full")
        }
        queuedCommands += 1
        stateLock.unlock()
        control.async { [self] in
            handle(request)
            stateLock.lock(); queuedCommands -= 1; stateLock.unlock()
        }
    }

    /// Parent loss requests stop immediately and bounds helper lifetime. No reconnect
    /// path automatically reopens input; the recovery receipt stays under asset root.
    func parentLost() -> Bool {
        stateLock.lock(); closed = true; stopDuringStartup = true; stateLock.unlock()
        controller.requestStop(parentLost: true)
        let deadline = DispatchTime.now() + 3
        let controlDrained = DispatchGroup()
        controlDrained.enter()
        control.async { controlDrained.leave() }
        guard controlDrained.wait(timeout: deadline) == .success else { return false }
        controller.requestStop(parentLost: true)
        let finished = active.wait(timeout: deadline) == .success
        timer?.cancel()
        return finished
    }

    private func handle(_ request: RecordingWireEnvelope) {
        let command = request.data.object?["command"]?.string ?? ""
        do {
            if command == "handshake" {
                guard request.data.object?["build"] == configuration.expectedClientBuild,
                      request.data.object?["expected_peer_build"] == configuration.build,
                      request.data.object?["role"] == .string("ui") else {
                    throw NativeCaptureFailure("RECORDING.BUILD_MISMATCH", "Recorder handshake build identity mismatch")
                }
                stateLock.lock(); handshaken = true; stateLock.unlock()
                respond(requestID: request.meta.request_id, event: .object([
                    "event": .string("handshake"), "role": .string("recorder"), "build": configuration.build,
                ]), takeID: nil)
                return
            }
            stateLock.lock(); let ready = handshaken; stateLock.unlock()
            guard ready else { throw NativeCaptureFailure("RECORDING.VERSION_MISMATCH", "Recorder handshake is required") }
            switch command {
            case "prepare":
                guard request.data.object?["device"]?.object?["backend"] == .string("coreaudio") else {
                    throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native recorder requires Core Audio device identity")
                }
                if configuration.syntheticPCM == nil {
                    guard let uid = request.data.object?["device"]?.object?["device_id"]?.string else {
                        throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Microphone preparation requires its exact device UID")
                    }
                    try CoreAudioCaptureBackend().validateSelectedDevice(uid)
                }
                acknowledge(request, action: "prepared")
            case "start": try start(request)
            case "stop":
                stateLock.lock()
                guard request.meta.take_id == takeID else { stateLock.unlock(); throw NativeCaptureFailure("RECORDING.STALE_REVISION", "Stop does not identify the active take") }
                if ["starting", "recording"].contains(phase) { phase = "stopping"; revision += 1 }
                stateLock.unlock()
                controller.requestStop()
                publishState(requestID: request.meta.request_id)
            case "snapshot": publishState(requestID: request.meta.request_id)
            case "shutdown":
                controller.requestStop()
                acknowledge(request, action: "shutdown")
                completion.async { [self] in
                    let deadline = DispatchTime.now() + 3
                    let finished = active.wait(timeout: deadline) == .success
                    let flushed = DispatchGroup()
                    flushed.enter()
                    outputQueue.async { flushed.leave() }
                    _ = flushed.wait(timeout: deadline)
                    _exit(finished ? 0 : 70)
                }
            default: throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Unknown recorder command")
            }
        } catch let failure as NativeCaptureFailure {
            respond(requestID: request.meta.request_id, event: nil, takeID: request.meta.take_id, error: failure)
        } catch {
            respond(requestID: request.meta.request_id, event: nil, takeID: request.meta.take_id,
                    error: NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Recorder command failed validation or execution"))
        }
    }

    private func start(_ request: RecordingWireEnvelope) throws {
        guard let data = request.data.object, let reservation = data["reservation"],
              let digest = data["reservation_sha256"]?.string,
              let identity = reservation.object?["take_id"]?.string,
              let operationID = reservation.object?["operation_id"]?.string else {
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Start requires a checksum-bound reservation")
        }
        stateLock.lock()
        if operationID == startOperationID {
            let same = request.data == startCommand
            stateLock.unlock()
            guard same else { throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Start operation ID was reused with different data") }
            publishState(requestID: request.meta.request_id)
            return
        }
        guard !["starting", "recording", "stopping"].contains(phase), !closed else {
            stateLock.unlock()
            throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "Recorder is not ready for another take")
        }
        stateLock.unlock()
        let lock = try NativeCaptureOwnership(assetRoot: configuration.assetRoot)
        stateLock.lock()
        guard !closed else { stateLock.unlock(); throw NativeCaptureFailure("RECORDING.CANCELLED", "Parent channel closed before capture could start") }
        takeID = identity
        startOperationID = operationID
        startCommand = request.data
        startRequestID = request.meta.request_id
        seal = nil
        stopDuringStartup = false
        phase = "starting"
        revision += 1
        captureGeneration += 1
        let nativeGeneration = captureGeneration
        ownership = lock
        stateLock.unlock()
        do {
            let native = try NativeCaptureRequest(reservation: reservation, reservationSHA256: digest,
                recorderBuild: configuration.build, assetRoot: configuration.assetRoot,
                bufferGeneration: nativeGeneration)
            try controller.start(request: native)
        } catch {
            stateLock.lock(); phase = "error"; revision += 1; ownership = nil; stateLock.unlock()
            throw error
        }
        stateLock.lock(); let pendingStop = stopDuringStartup; stateLock.unlock()
        if pendingStop { controller.requestStop() }
        publishState(requestID: request.meta.request_id)
        active.enter()
        completion.async { [self] in
            defer { active.leave() }
            do {
                let outcome = try controller.finish(timeout: 615)
                // Serialize the reviewable draft publication and bounded lease handoff with
                // Start. The native finish wait above and archive IO remain independent.
                control.sync {
                    stateLock.lock()
                    seal = outcome.seal
                    phase = outcome.seal == nil ? "error" : "draft"
                    revision += 1
                    ownership = nil
                    let requestID = startRequestID
                    stateLock.unlock()
                    if let failure = outcome.failure {
                        respond(requestID: requestID, event: nil, takeID: identity, error: failure)
                    }
                    publishState(requestID: requestID)
                    // Publish reviewable draft before any archive serialization/IO, and reserve
                    // one of only two constant-time leases before retaining the old capture run.
                    if let completedSeal = outcome.seal,
                       let sourceSHA256 = completedSeal.object?["original_sha256"]?.string,
                       let frames = completedSeal.object?["audio"]?.object?["frames"]?.integer {
                        waveformArchives.enqueue(takeID: identity, sourceSHA256: sourceSHA256,
                            assetRoot: configuration.assetRoot,
                            makeArchive: { try controller.waveformArchiveJob(takeID: identity, sourceSHA256: sourceSHA256, finalWrittenFrames: frames) },
                            completion: { [weak self] result in self?.publishArchiveResult(result) })
                    }
                }
            } catch {
                controller.requestStop(parentLost: true)
                // A failed completion is still a terminal: release ownership and publish its
                // recorder-state so the client leaves Stopping and the service can recheck.
                control.sync {
                    stateLock.lock()
                    phase = "error"
                    revision += 1
                    ownership = nil
                    let requestID = startRequestID
                    stateLock.unlock()
                    respond(requestID: requestID, event: nil, takeID: identity,
                            error: NativeCaptureFailure("RECORDING.WRITE_FAILED", "Recorder completion failed; partial evidence requires recovery"))
                    publishState(requestID: requestID)
                }
            }
        }
    }

    private func publishArchiveResult(_ result: RecordingWaveformArchiveCompletion) {
        let status: String
        let error: RecordingJSON
        switch result.result {
        case .stored: status = "stored"; error = .null
        case .needsRebuild(let failure):
            status = "needs-rebuild"
            error = .object(["code": .string("RECORDING.WAVEFORM_CACHE_UNAVAILABLE"),
                "message": .string(failure.message), "details": .array([]), "retryable": .boolean(true)])
        }
        // Previous-take cache status is its own reliable event, never a transport mutation.
        respond(requestID: "waveform-cache-" + UUID().uuidString.lowercased(), event: .object([
            "event": .string("waveform-cache"), "take_id": .string(result.takeID),
            "source_sha256": .string(result.sourceSHA256), "status": .string(status), "error": error,
        ]), takeID: result.takeID)
    }

    private func publishState(requestID: String) {
        let progress = controller.progress()
        stateLock.lock()
        let currentPhase = phase
        let currentTake = takeID
        let currentSeal = seal
        stateLock.unlock()
        let frames = currentSeal?.object?["audio"]?.object?["frames"] ?? .integer(Int64(progress?.writtenFrames ?? 0))
        respond(requestID: requestID, event: .object([
            "event": .string("recorder-state"), "transport": .string(currentPhase),
            "take_id": currentTake.map(RecordingJSON.string) ?? .null,
            "written_frames": frames, "seal": currentSeal ?? .null,
        ]), takeID: currentTake)
    }

    private func acknowledge(_ request: RecordingWireEnvelope, action: String) {
        respond(requestID: request.meta.request_id, event: .object([
            "event": .string("acknowledged"), "operation_id": .string(request.meta.request_id), "action": .string(action),
        ]), takeID: nil)
    }

    private func respond(requestID: String, event: RecordingJSON?, takeID: String?, error: NativeCaptureFailure? = nil) {
        stateLock.lock()
        sequence += 1
        let response: RecordingJSON = .object([
            "data": event ?? .null, "error": error?.json ?? .null,
            "meta": .object([
                "protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string(requestID),
                "process_generation": .string(configuration.generation), "state_revision": .integer(revision),
                "event_sequence": .integer(sequence), "take_id": takeID.map(RecordingJSON.string) ?? .null,
            ]),
        ])
        do {
            let payload = try response.transportData()
            _ = try RecordingSchemaValidator(schema: responseSchema).validate(payload)
            let framed = try RecordingFrameCodec.encode(payload)
            guard pendingReliableBytes + framed.count <= 2_097_152 else {
                stateLock.unlock(); terminateAfterTransportFailure(); return
            }
            pendingReliableBytes += framed.count
            outputQueue.async { [self] in
                do { try FileHandle.standardOutput.write(contentsOf: framed) }
                catch { terminateAfterTransportFailure() }
                stateLock.lock(); pendingReliableBytes -= framed.count; stateLock.unlock()
            }
            stateLock.unlock()
        } catch {
            stateLock.unlock()
            terminateAfterTransportFailure()
        }
    }

    /// A failed reliable channel cannot be reported through that same channel.
    /// Preserve recovery evidence and terminate within the parent-loss deadline.
    private func terminateAfterTransportFailure() {
        stateLock.lock()
        guard !terminationScheduled else { stateLock.unlock(); return }
        terminationScheduled = true
        closed = true
        stateLock.unlock()
        controller.requestStop(parentLost: true)
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            _ = parentLost()
            _exit(70)
        }
    }

    private func publishProgress() {
        // A completed waveform is immutable. Keep retrying a partially written or
        // queued final frame without rebuilding and revalidating it at 60 Hz.
        do { try telemetryWriter.flush() }
        catch { terminateAfterTransportFailure(); return }
        guard let progress = controller.progress() else { return }
        stateLock.lock()
        let changed = phase == "starting" && progress.writtenFrames > 0
        if changed { phase = "recording"; revision += 1 }
        let requestID = startRequestID
        stateLock.unlock()
        if changed { publishState(requestID: requestID) }
        do {
            stateLock.lock()
            let currentTake = takeID
            let currentPhase = phase
            let currentGeneration = captureGeneration
            let completed = ["draft", "error"].contains(currentPhase)
                && ["draft", "error"].contains(progress.phase)
            if completed, completedTelemetryGeneration == currentGeneration {
                stateLock.unlock()
                return
            }
            telemetrySequence += 1
            let visualSequence = telemetrySequence
            let currentRevision = revision
            stateLock.unlock()
            guard let currentTake, currentTake == progress.takeID else { return }
            let meta: RecordingJSON = .object([
                "protocol_version": .string("cole-voice-lab.recording/v1"),
                "request_id": .string("waveform-\(visualSequence)"),
                "process_generation": .string(configuration.generation),
                "state_revision": .integer(currentRevision), "event_sequence": .integer(visualSequence),
                "take_id": .string(currentTake),
            ])
            let frames = Int64(progress.writtenFrames)
            let recent = ["starting", "recording", "stopping"].contains(currentPhase)
                ? max(0, frames - 192_000)..<frames : nil
            guard let frame = try controller.framedWaveformSnapshot(range: recent, meta: meta) else { return }
            let payload = Data(frame.dropFirst(4))
            let visual = try RecordingJSON.parse(payload)
            // A concurrent take selection may have replaced the controller between the two reads.
            // It is stale visual traffic, never a reason to stop the new raw capture.
            guard visual.object?["data"]?.object?["take_id"] == .string(currentTake) else { return }
            _ = try RecordingWireEnvelope.decode(payload, schema: telemetrySchema)
            try telemetryWriter.offerFrame(frame)
            if completed { completedTelemetryGeneration = currentGeneration }
        } catch { terminateAfterTransportFailure() }
    }
}
