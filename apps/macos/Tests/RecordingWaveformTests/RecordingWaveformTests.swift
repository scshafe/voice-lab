import AppKit
import Foundation
import CoreGraphics
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI
import Testing

private func pcm(_ samples: [Int32]) -> Data {
    Data(samples.flatMap { sample in [UInt8(truncatingIfNeeded: sample), UInt8(truncatingIfNeeded: sample >> 8), UInt8(truncatingIfNeeded: sample >> 16)] })
}
private func append(_ samples: [Int32], to cache: RecordingWaveformCache, chunks: [Int]) throws {
    var start = 0, iteration = 0
    while start < samples.count {
        let count = min(chunks[iteration % chunks.count], samples.count - start)
        _ = try pcm(Array(samples[start..<(start + count)])).withUnsafeBytes { try cache.append(pcm: $0, startFrame: Int64(start), sampleClock: nil) }
        start += count; iteration += 1
    }
}

@Suite(.serialized) struct RecordingWaveformTests {
    @Test func fixedBucketsPreserveEveryImpulseAcrossVariedCallbackSizes() throws {
        var samples = [Int32](repeating: 0, count: 65_539)
        for index in stride(from: 0, to: samples.count, by: 127) { samples[index] = index % 2 == 0 ? 8_388_607 : -8_388_608 }
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "generation")
        try append(samples, to: cache, chunks: [1, 63, 65, 127, 480, 4096])
        for capacity in [1, 7, 128, 4096] {
            let snapshot = try cache.snapshot(maximumBuckets: capacity)
            #expect(snapshot.buckets.count <= capacity)
            #expect(snapshot.writtenFrames == samples.count && snapshot.endFrame == samples.count)
            for (index, bucket) in snapshot.buckets.enumerated() {
                let start = Int(snapshot.firstFrame) + index * Int(snapshot.bucketWidthFrames)
                let end = min(samples.count, start + Int(snapshot.bucketWidthFrames))
                #expect(bucket.minimum == samples[start..<end].min() && bucket.maximum == samples[start..<end].max())
            }
        }
        let zoomed = try cache.snapshot(range: 300..<900, maximumBuckets: 8)
        #expect(zoomed.firstFrame <= 300 && zoomed.endFrame >= 900 && zoomed.buckets.count <= 8)
    }

    @Test func silenceQuietAndClippingRetainTheirActualAmplitude() throws {
        for amplitude: Int32 in [0, 1, 8_388_607] {
            let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "generation", maximumFrames: 128)
            try append([amplitude, -amplitude] + [Int32](repeating: 0, count: 126), to: cache, chunks: [3, 61])
            let snapshot = try cache.snapshot()
            #expect(snapshot.buckets[0].minimum == -amplitude && snapshot.buckets[0].maximum == amplitude)
            #expect(RecordingWaveformCoordinates.displayGain(snapshot: snapshot, fitting: true, recording: true) == 1)
            if amplitude == 0 { #expect(RecordingWaveformCoordinates.displayGain(snapshot: snapshot, fitting: true, recording: false) == 1) }
        }
    }

    @Test func frameCoordinatesRemainExactAcrossZoomAndTrimBounds() throws {
        let coordinates = try RecordingWaveformCoordinates(viewport: 10_000..<58_000, plot: CGRect(x: 12, y: 16, width: 1000, height: 120))
        for frame in [10_000, 10_001, 24_007, 57_999, 58_000] as [Int64] {
            #expect(coordinates.frame(x: coordinates.x(frame: frame)) == frame)
        }
        #expect(coordinates.frame(x: 0, retainedRange: 20_000..<40_000) == 20_000)
        #expect(coordinates.frame(x: 2000, retainedRange: 20_000..<40_000) == 40_000)
        #expect(coordinates.y(amplitude: 0) == 76)
        #expect(RecordingWaveformCoordinates.liveViewport(writtenFrames: 480).upperBound == 192_000)
        #expect(RecordingWaveformCoordinates.liveViewport(writtenFrames: 240_000) == 48_000..<240_000)
    }

    @Test func displayDecibelScaleMapsFullScaleSpeechAndFloorMonotonically() throws {
        let scale = RecordingWaveformAmplitudeScale.self
        #expect(scale.displayFraction(amplitude: 1) == 1 && scale.displayFraction(amplitude: -1) == -1)
        #expect(abs(scale.displayFraction(amplitude: pow(10, -30.0 / 20)) - 0.5) < 1e-12)
        #expect(abs(scale.displayFraction(amplitude: -pow(10, -30.0 / 20)) + 0.5) < 1e-12)
        // P05 UMIK-1 speech peaks (−28 to −33 dBFS) use roughly half the height, not a flat line.
        #expect(scale.displayFraction(amplitude: pow(10, -33.0 / 20)) > 0.44)
        for decibels in [-60.0, -90.0, -138.5] {
            #expect(scale.displayFraction(amplitude: pow(10, decibels / 20)) == scale.floorMagnitude)
        }
        #expect(scale.displayFraction(amplitude: 1.0 / 8_388_608) == scale.floorMagnitude)
        #expect(scale.floorMagnitude > 0 && scale.floorMagnitude < scale.displayFraction(amplitude: pow(10, -50.0 / 20)))
        #expect(scale.displayFraction(amplitude: 0) == 0 && scale.displayFraction(amplitude: -0.0) == 0)
        #expect(scale.displayFraction(amplitude: .nan) == 0)
        #expect(scale.displayFraction(amplitude: 64) == 1 && scale.displayFraction(amplitude: -.infinity) == -1)
        var previous = 0.0
        for step in 1...8_192 {
            let amplitude = Double(step) / 8_192
            let fraction = scale.displayFraction(amplitude: amplitude)
            #expect(fraction >= previous && fraction <= 1 && scale.displayFraction(amplitude: -amplitude) == -fraction)
            previous = fraction
        }
        let coordinates = try RecordingWaveformCoordinates(viewport: 0..<48_000, plot: CGRect(x: 12, y: 16, width: 1000, height: 120))
        #expect(scale.y(amplitude: 0, displayGain: 64, coordinates: coordinates) == 76)
        #expect(scale.y(amplitude: -1, displayGain: 1, coordinates: coordinates) == 136)
        #expect(abs(scale.y(amplitude: pow(10, -60.0 / 20), displayGain: 1000, coordinates: coordinates) - 16) < 1e-9)
        // Explicit Fit gain scales source amplitude before, not after, the logarithmic mapping.
        #expect(abs(scale.y(amplitude: pow(10, -36.0 / 20), displayGain: 2, coordinates: coordinates) - (76 - 60 * (0.4 + log10(2) / 3))) < 1e-9)
        // Display-only transform: the snapshot keeps its source extrema and linear peak.
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "generation", maximumFrames: 128)
        try append([265_180, -265_180] + [Int32](repeating: 0, count: 126), to: cache, chunks: [128])
        let snapshot = try cache.snapshot()
        #expect(snapshot.buckets[0].minimum == -265_180 && snapshot.buckets[0].maximum == 265_180)
        #expect(abs(snapshot.peakAmplitude - 265_180.0 / 8_388_608) < 1e-15)
    }

    @Test func actualSampleAnchorProducesAgeAndRejectsFutureSample() throws {
        let clock = try RecordingWaveformSampleClock(anchorFrame: 0, hostTimeTicks: 1_000_000, secondsPerTick: 0.000001)
        #expect(abs(try clock.ageSeconds(frame: 480, visibleHostTicks: 1_090_000) - 0.08) < 1e-12)
        #expect(throws: RecordingContractError.self) { try clock.ageSeconds(frame: 48_000, visibleHostTicks: 1_010_000) }
    }

    @Test @MainActor func staleTakeGenerationAndWrittenFrameRewindCannotReplaceSelection() throws {
        let model = RecordingWaveformModel(takeID: "take", processGeneration: "current")
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "current", maximumFrames: 128)
        let empty = try cache.snapshot()
        try append([1, -1], to: cache, chunks: [2])
        #expect(model.accept(try cache.snapshot()))
        #expect(!model.accept(empty))
        let stale = try RecordingWaveformCache(takeID: "take", processGeneration: "old", maximumFrames: 128)
        #expect(!model.accept(try stale.snapshot()))
        model.select(takeID: "different", processGeneration: "current", hostClockComparable: false)
        #expect(!model.accept(try cache.snapshot()))
        #expect(try model.capturedSampleAge(visibleHostTicks: 1) == nil)
    }

    @Test func telemetryByteBoundIncludesQuietExtremaLongIdentitiesAndClock() throws {
        let identifier = String(repeating: "a", count: 128)
        let cache = try RecordingWaveformCache(takeID: identifier, processGeneration: identifier, maximumFrames: 524_288)
        let data = pcm([Int32](repeating: 1, count: 4096))
        let clock = try RecordingWaveformSampleClock(anchorFrame: 0, hostTimeTicks: 9_223_372_036_000_000_000, secondsPerTick: 0.00000004166666666666667)
        for index in 0..<128 { _ = try data.withUnsafeBytes { try cache.append(pcm: $0, startFrame: Int64(index * 4096), sampleClock: clock) } }
        let meta: RecordingJSON = .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string(identifier), "process_generation": .string(identifier), "state_revision": .integer(.max), "event_sequence": .integer(.max), "take_id": .string(identifier)])
        let result = try cache.framedSnapshot(meta: meta)
        #expect(result.frame.count - 4 <= RecordingFrameCodec.telemetryMaximumBytes)
        #expect(result.snapshot.buckets.count <= 4096)
        #expect(result.snapshot.buckets.allSatisfy { $0.minimum == 1 && $0.maximum == 1 })
        #expect(result.snapshot.writtenFrames == 524_288)
    }

    @Test func fixedAllocationAndImmutableSlowConsumerDoNotAccumulatePCM() throws {
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "generation")
        #expect(cache.allocatedExtremaBytes < 7_300_000)
        let old = try cache.snapshot()
        let data = pcm([Int32](repeating: -3, count: 4096))
        for index in 0..<128 { _ = try data.withUnsafeBytes { try cache.append(pcm: $0, startFrame: Int64(index * 4096), sampleClock: nil) } }
        #expect(old.writtenFrames == 0 && old.buckets.isEmpty)
        #expect(try cache.snapshot(maximumBuckets: 1).writtenFrames == 524_288)
        #expect(throws: RecordingContractError.self) { _ = try data.withUnsafeBytes { try cache.append(pcm: $0, startFrame: 0, sampleClock: nil) } }
        #expect(throws: RecordingContractError.self) { try cache.snapshot(maximumBuckets: 4097) }
    }
    @Test func sourceBoundArchiveRestoresPartialBucketsWithoutHistoricalHostClock() throws {
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "current", maximumFrames: 128)
        let data = pcm([Int32](repeating: 1234, count: 67))
        let clock = try RecordingWaveformSampleClock(anchorFrame: 0, hostTimeTicks: 100, secondsPerTick: 0.000001)
        _ = try data.withUnsafeBytes { try cache.append(pcm: $0, startFrame: 0, sampleClock: clock) }
        let digest = String(repeating: "a", count: 64)
        let archive = try cache.archive(sourceSHA256: digest, finalWrittenFrames: 67)
        let restored = try RecordingWaveformCache(archive: archive, expectedSourceSHA256: digest, takeID: "historical", processGeneration: "new")
        #expect(try restored.snapshot().buckets == cache.snapshot().buckets)
        #expect(try restored.snapshot().sampleClock == nil)
        #expect(throws: RecordingContractError.self) { try RecordingWaveformCache(archive: archive, expectedSourceSHA256: String(repeating: "b", count: 64), takeID: "take", processGeneration: "new") }
        #expect(throws: RecordingContractError.self) { try RecordingWaveformCache(archive: archive.dropLast(), expectedSourceSHA256: digest, takeID: "take", processGeneration: "new") }
        #expect(throws: RecordingContractError.self) { try cache.archive(sourceSHA256: digest, finalWrittenFrames: 68) }
    }

    @Test func blockedRendererHoldingAnImmutableCopyCannotBlockNewWrittenFrames() throws {
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "generation", maximumFrames: 524_288)
        let copied = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0), finished = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            let snapshot = try? cache.snapshot()
            copied.signal()
            release.wait()
            _ = snapshot?.buckets.count
            finished.signal()
        }
        defer { release.signal() }
        #expect(copied.wait(timeout: .now() + 5) == .success)
        let data = pcm([Int32](repeating: 1, count: 4096))
        for index in 0..<128 { _ = try data.withUnsafeBytes { try cache.append(pcm: $0, startFrame: Int64(index * 4096), sampleClock: nil) } }
        #expect(try cache.snapshot().writtenFrames == 524_288)
        release.signal()
        #expect(finished.wait(timeout: .now() + 5) == .success)
    }

    /// Colour audit (plan Step 8): the plot background is a semantic colour, so a hosted view drawn under a
    /// forced dark appearance renders dark and under a forced light one light; extrema and scale are untouched.
    @Test @MainActor func plotBackgroundFollowsTheHostedAppearance() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "generation", maximumFrames: 128)
        try append([265_180, -265_180] + [Int32](repeating: 0, count: 126), to: cache, chunks: [128])
        let model = RecordingWaveformModel(takeID: "take", processGeneration: "generation", hostClockComparable: false)
        #expect(model.accept(try cache.snapshot()))
        for (name, dark) in [(NSAppearance.Name.darkAqua, true), (.aqua, false)] {
            let size = CGSize(width: 900, height: 180)
            let host = NSHostingView(rootView: RecordingWaveformView(model: model, isRecording: false).environment(\.colorScheme, dark ? .dark : .light))
            host.appearance = NSAppearance(named: name); host.frame = CGRect(origin: .zero, size: size); host.layoutSubtreeIfNeeded()
            let bitmap = try #require(NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 900, pixelsHigh: 180, bitsPerSample: 8, samplesPerPixel: 4,
                hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
            bitmap.size = size
            host.cacheDisplay(in: host.bounds, to: bitmap)
            let corners = [(4, 4), (895, 4)].compactMap { RecordingPalette.renderedLuminance(bitmap, x: $0, y: $1) }
            #expect(corners.count == 2 && corners.allSatisfy { dark ? $0 < 0.5 : $0 > 0.5 }, "\(name.rawValue): \(corners)")
        }
        #expect(try cache.snapshot().buckets[0].maximum == 265_180)
    }

    @Test func archivedExtremaCorruptionRejectsEvenWhenValuesRemainPlausible() throws {
        let sourceDigest = String(repeating: "a", count: 64)
        let cache = try RecordingWaveformCache(takeID: "take", processGeneration: "generation", maximumFrames: 64)
        let pcm = Data(repeating: 0, count: 64 * 3)
        _ = try pcm.withUnsafeBytes { try cache.append(pcm: $0, startFrame: 0, sampleClock: nil) }
        let valid = try cache.archive(sourceSHA256: sourceDigest, finalWrittenFrames: 64)
        #expect(valid.prefix(8) == Data("CVLWAV02".utf8))
        #expect(valid.count == 92)
        var altered = valid
        altered[56] = 1 // maximum +1 LSB is in range and plausible, but not the original silence.
        #expect(throws: RecordingContractError.self) { try RecordingWaveformCache(archive: altered, expectedSourceSHA256: sourceDigest, takeID: "take", processGeneration: "new") }
        var legacy = Data(valid.dropLast(32)); legacy.replaceSubrange(0..<8, with: Data("CVLWAV01".utf8))
        #expect(throws: RecordingContractError.self) { try RecordingWaveformCache(archive: legacy, expectedSourceSHA256: sourceDigest, takeID: "take", processGeneration: "new") }
        #expect(try RecordingWaveformCache(archive: valid, expectedSourceSHA256: sourceDigest, takeID: "take", processGeneration: "new").snapshot().buckets.allSatisfy { $0.minimum == 0 && $0.maximum == 0 })
    }

}
