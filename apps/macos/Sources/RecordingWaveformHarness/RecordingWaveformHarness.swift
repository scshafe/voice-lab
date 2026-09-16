import AppKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

@main struct RecordingWaveformHarness {
    @MainActor static func main() throws {
        umask(0o077)
        guard CommandLine.arguments.count == 2 else { throw failure("One private external evidence directory is required") }
        let directory = URL(fileURLWithPath: CommandLine.arguments[1])
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        NSApplication.shared.setActivationPolicy(.prohibited)
        // TA1: a Light preference registered in this process (volatile, never written to disk) must not
        // change the hosted view; only the hosting view's forced appearance decides what is drawn.
        UserDefaults.standard.register(defaults: [RecordingAppearancePreference.defaultsKey: RecordingAppearancePreference.light.rawValue])
        let memoryBefore = try residentBytes()
        let cache = try RecordingWaveformCache(takeID: "synthetic-ten-minute", processGeneration: "synthetic-generation")
        let chunk = pcm(frames: 4096) { index in index % 127 == 0 ? -8_388_608 : (index % 131 == 0 ? 8_388_607 : 0) }
        let throughputStart = ContinuousClock.now
        var frames: Int64 = 0
        var appendSeconds: [Double] = []
        while frames < RecordingWaveformCache.maximumFrames {
            let count = min(Int64(4096), RecordingWaveformCache.maximumFrames - frames)
            let start = ContinuousClock.now
            _ = try chunk.withUnsafeBytes { try cache.append(pcm: UnsafeRawBufferPointer(rebasing: $0[..<(Int(count) * 3)]), startFrame: frames, sampleClock: nil) }
            appendSeconds.append(seconds(start.duration(to: .now)))
            frames += count
        }
        let elapsed = seconds(throughputStart.duration(to: .now))
        let overview = try cache.snapshot()
        guard overview.writtenFrames == 28_800_000, overview.endFrame == 28_800_000,
              overview.buckets.count <= 4096, overview.buckets.allSatisfy({ $0.minimum == -8_388_608 && $0.maximum == 8_388_607 }) else {
            throw failure("Ten-minute source extrema or final frame agreement failed")
        }
        let memoryAdded = max(0, try residentBytes() - memoryBefore)
        guard memoryAdded <= 64 * 1024 * 1024 else { throw failure("Ten-minute waveform working memory exceeds 64 MiB") }
        var renderSeconds: [Double] = []
        var imageCount = 0
        for fixture in ["silence", "quiet", "sine", "impulses", "clipping"] {
            let source = try RecordingWaveformCache(takeID: fixture, processGeneration: "synthetic-generation", maximumFrames: 96_000)
            var offset = 0
            while offset < 96_000 {
                let count = min(4096, 96_000 - offset)
                let base = offset
                let data = pcm(frames: count) { index in
                    let frame = base + index
                    switch fixture {
                    case "silence": return 0
                    case "quiet": return frame % 2 == 0 ? 1 : -1
                    case "sine": return Int32(sin(Double(frame) * 2 * .pi * 220 / 48_000) * 3_000_000)
                    case "impulses": return frame % 4001 == 0 ? 8_388_607 : (frame % 4003 == 0 ? -8_388_608 : 0)
                    default: return frame % 400 < 200 ? 8_388_607 : -8_388_608
                    }
                }
                _ = try data.withUnsafeBytes { try source.append(pcm: $0, startFrame: Int64(offset), sampleClock: nil) }
                offset += count
            }
            let snapshot = try source.snapshot()
            let model = RecordingWaveformModel(takeID: fixture, processGeneration: "synthetic-generation", hostClockComparable: false)
            guard model.accept(snapshot) else { throw failure("Fixture snapshot was rejected") }
            for dark in [false, true] {
                for scale in [1, 2] {
                    for recording in [false, true] {
                        let size = CGSize(width: 900, height: 180)
                        let view = NSHostingView(rootView: RecordingWaveformView(model: model, isRecording: recording,
                            trimRange: 4800..<91_200, renderObserver: { renderSeconds.append($0.workSeconds) })
                            .environment(\.colorScheme, dark ? .dark : .light)
                            .environment(\.displayScale, CGFloat(scale)))
                        view.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                        view.frame = CGRect(origin: .zero, size: size)
                        view.layoutSubtreeIfNeeded()
                        let destination = directory.appendingPathComponent("\(fixture)-\(dark ? "dark" : "light")-\(scale)x-\(recording ? "live" : "stopped").png")
                        let bitmap = try render(view, size: size, scale: scale, to: destination)
                        // The plot background is semantic, so the top corners must follow the forced appearance.
                        let corners = [(4, 4), (bitmap.pixelsWide - 5, 4)].compactMap { RecordingPalette.renderedLuminance(bitmap, x: $0, y: $1) }
                        guard corners.count == 2, corners.allSatisfy({ dark ? $0 < 0.5 : $0 > 0.5 }) else {
                            throw failure("Waveform background does not follow the \(dark ? "dark" : "light") appearance: \(destination.lastPathComponent) \(corners)")
                        }
                        imageCount += 1
                    }
                }
            }
        }
        guard renderSeconds.count >= imageCount else { throw failure("Canvas did not execute for every offscreen snapshot") }
        let p95 = percentile(renderSeconds, 0.95)
        guard p95 <= 0.0167 else { throw failure("Actual Canvas drawing work exceeds the 16.7 ms p95 gate") }
        let report: [String: Any] = [
            "synthetic_only": true, "microphone_opened": false, "visible_window": false,
            "accelerated_frames": frames, "accelerated_append_seconds": elapsed,
            "append_p95_seconds": percentile(appendSeconds, 0.95), "allocated_extrema_bytes": cache.allocatedExtremaBytes,
            "measured_resident_growth_bytes": memoryAdded, "final_frame_agreement": true,
            "canvas_render_count": renderSeconds.count, "canvas_render_p95_seconds": p95,
            "configured_live_target_hz": 60, "configured_energy_saving_hz": 30,
            "physical_display_cadence_measured": false, "captured_sample_visible_age_measured": false,
            "png_snapshots": imageCount, "historical_wav_reads": 0,
        ]
        let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: directory.appendingPathComponent("waveform-result.json"), options: .atomic)
        print(String(decoding: data, as: UTF8.self))
    }

    private static func pcm(frames: Int, value: (Int) -> Int32) -> Data {
        var bytes = [UInt8](repeating: 0, count: frames * 3)
        for index in 0..<frames {
            let sample = value(index)
            bytes[index * 3] = UInt8(truncatingIfNeeded: sample)
            bytes[index * 3 + 1] = UInt8(truncatingIfNeeded: sample >> 8)
            bytes[index * 3 + 2] = UInt8(truncatingIfNeeded: sample >> 16)
        }
        return Data(bytes)
    }
    private static func seconds(_ duration: Duration) -> Double {
        let parts = duration.components; return Double(parts.seconds) + Double(parts.attoseconds) / 1e18
    }
    private static func percentile(_ values: [Double], _ quantile: Double) -> Double {
        values.sorted()[min(values.count - 1, Int(Double(values.count - 1) * quantile))]
    }
    private static func residentBytes() throws -> Int64 {
        var info = mach_task_basic_info()
        var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<natural_t>.size)
        let status = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &count) }
        }
        guard status == KERN_SUCCESS else { throw failure("Cannot measure native resident memory") }
        return Int64(info.resident_size)
    }
    @MainActor private static func render(_ view: NSView, size: CGSize, scale: Int, to destination: URL) throws -> NSBitmapImageRep {
        guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width) * scale,
            pixelsHigh: Int(size.height) * scale, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
            isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { throw failure("Cannot allocate private waveform snapshot") }
        bitmap.size = size
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw failure("Cannot encode native waveform snapshot") }
        try png.write(to: destination, options: .atomic)
        return bitmap
    }
    private static func failure(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.WAVEFORM_VERIFICATION", message: message)
    }
}
