import AVFoundation
import Foundation
import RecordingContracts

/// Selected-source cache loading/rebuilding only. No historical library scan or output engine.
public enum RecordingWaveformSelectionLoader {
    public static func load(_ source: RecordingPlaybackSource, processGeneration: String,
                            diagnosticContext: RecordingDiagnosticContext? = nil,
                            diagnosticSink: RecordingDiagnosticSink? = nil) throws -> RecordingWaveformCache {
        let context = diagnosticSink == nil ? nil : (diagnosticContext ?? RecordingDiagnosticContext(traceID: UUID().uuidString.lowercased())).source(source)
        let span = RecordingDiagnosticSpan("waveform.prepare", context: context, sink: diagnosticSink)
        do {
            let cache = try prepare(source, processGeneration: processGeneration, diagnosticContext: span.childContext, diagnosticSink: diagnosticSink)
            span.finish(); return cache
        } catch { span.finish(error is CancellationError ? .cancelled : .failure, error: error); throw error }
    }

    private static func prepare(_ source: RecordingPlaybackSource, processGeneration: String,
                                diagnosticContext: RecordingDiagnosticContext?, diagnosticSink: RecordingDiagnosticSink?) throws -> RecordingWaveformCache {
        let path = source.assetRoot.appendingPathComponent(source.relativePath).path
        let lease = try RecordingPrivateFile.openReadOnly(path, maximumBytes: source.format.maximumFileBytes(frameCount: source.frameCount),
            diagnosticContext: diagnosticContext, diagnosticSink: diagnosticSink)
        if let identity = source.fileIdentity { try lease.validate(expectedIdentity: identity, relativePath: source.relativePath) }
        let cacheSpan = RecordingDiagnosticSpan("waveform.cache_load", context: diagnosticContext, sink: diagnosticSink)
        do {
            let cached = try RecordingWaveformArchiveStore.load(sourceSHA256: source.sourceSHA256,
                assetRoot: source.assetRoot, takeID: source.takeID, processGeneration: processGeneration)
            cacheSpan.finish(.cacheHit); return cached
        } catch { cacheSpan.finish(.cacheMiss, error: error) }
        guard try lease.sha256() == source.sourceSHA256 else { throw changed() }
        let openSpan = RecordingDiagnosticSpan("waveform.av_audio_file_open", context: diagnosticContext, sink: diagnosticSink)
        let file: AVAudioFile
        do {
            file = try AVAudioFile(forReading: URL(fileURLWithPath: path), commonFormat: .pcmFormatFloat32, interleaved: false)
            openSpan.finish()
        } catch { openSpan.finish(.failure, error: error); throw error }
        let decodeSpan = RecordingDiagnosticSpan("waveform.decode", context: diagnosticContext, sink: diagnosticSink)
        do {
            let cache = try decode(file, source: source, processGeneration: processGeneration)
            try lease.validateUnchanged()
            decodeSpan.finish(byteCount: source.frameCount * 3)
            return cache
        } catch { decodeSpan.finish(error is CancellationError ? .cancelled : .failure, error: error); throw error }
    }

    private static func decode(_ file: AVAudioFile, source: RecordingPlaybackSource, processGeneration: String) throws -> RecordingWaveformCache {
        let format = file.fileFormat.streamDescription.pointee
        guard file.length == source.frameCount, format.mSampleRate == 48_000, format.mChannelsPerFrame == 1,
              format.mBitsPerChannel == 24, format.mFormatID == kAudioFormatLinearPCM,
              let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 4096) else { throw changed() }
        let cache = try RecordingWaveformCache(takeID: source.takeID, processGeneration: processGeneration, maximumFrames: source.frameCount)
        var written: Int64 = 0
        while written < source.frameCount {
            try Task.checkCancellation()
            try file.read(into: buffer, frameCount: AVAudioFrameCount(min(4096, source.frameCount - written)))
            guard buffer.frameLength > 0, let samples = buffer.floatChannelData?[0] else { throw changed() }
            // Every signed24 PCM value is exactly representable as Float32 at this scale.
            // Conversion is solely a selected-source decoder; capture always uses original bytes.
            var bytes = Data(count: Int(buffer.frameLength) * 3)
            for index in 0..<Int(buffer.frameLength) {
                let scaled = Double(samples[index]) * 8_388_608
                guard scaled.isFinite, scaled >= -8_388_608, scaled <= 8_388_607,
                      scaled.rounded() == scaled else { throw changed() }
                let sample = Int32(scaled)
                bytes[index * 3] = UInt8(truncatingIfNeeded: sample)
                bytes[index * 3 + 1] = UInt8(truncatingIfNeeded: sample >> 8)
                bytes[index * 3 + 2] = UInt8(truncatingIfNeeded: sample >> 16)
            }
            _ = try bytes.withUnsafeBytes { try cache.append(pcm: $0, startFrame: written, sampleClock: nil) }
            written += Int64(buffer.frameLength)
        }
        return cache
    }
    private static func changed() -> RecordingContractError {
        RecordingContractError(code: "RECORDING.SOURCE_CHANGED", message: "Selected waveform source differs from its authorized original PCM")
    }
}
