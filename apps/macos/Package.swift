// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ColeVoiceRecording",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "ColeVoiceRecorderPreview", targets: ["RecordingApp"]),
        .executable(name: "ColeVoiceRecorderHelper", targets: ["RecordingHelper"]),
        .executable(name: "RecordingHarness", targets: ["RecordingHarness"]),
        .executable(name: "RecordingBridgeHarness", targets: ["RecordingBridgeHarness"]),
        .executable(name: "RecordingWaveformHarness", targets: ["RecordingWaveformHarness"]),
    ],
    targets: [
        .target(name: "CaptureBuffer", publicHeadersPath: "include", linkerSettings: [.linkedFramework("AudioToolbox")]),
        .target(name: "RecordingContracts", exclude: ["README.md"]),
        .target(name: "RecordingCore", dependencies: ["RecordingContracts", "CaptureBuffer"], exclude: ["README.md"]),
        .target(name: "RecordingUI", dependencies: ["RecordingCore", "RecordingContracts"], exclude: ["README.md"]),
        .executableTarget(name: "RecordingApp", dependencies: ["RecordingUI", "RecordingCore", "RecordingContracts"]),
        .executableTarget(name: "RecordingHelper", dependencies: ["RecordingCore"]),
        .executableTarget(name: "RecordingHarness", dependencies: ["RecordingUI", "RecordingCore", "RecordingContracts"]),
        .executableTarget(name: "RecordingBridgeHarness", dependencies: ["RecordingCore", "RecordingContracts"]),
        .executableTarget(name: "RecordingWaveformHarness", dependencies: ["RecordingUI", "RecordingCore", "RecordingContracts"]),
        .testTarget(name: "RecordingWaveformTests", dependencies: ["RecordingUI", "RecordingCore", "RecordingContracts"]),
        .testTarget(name: "RecordingBridgeTests", dependencies: ["RecordingCore", "RecordingContracts"]),
        .testTarget(name: "RecordingReviewTests", dependencies: ["RecordingUI", "RecordingCore", "RecordingContracts"]),
        .testTarget(name: "RecordingPlaybackTests", dependencies: ["RecordingCore", "RecordingContracts"]),
        .testTarget(name: "RecordingWorkspaceTests", dependencies: ["RecordingUI", "RecordingCore", "RecordingContracts"]),
        .testTarget(name: "RecordingHarnessTests", dependencies: ["RecordingHarness", "RecordingUI", "RecordingCore", "RecordingContracts"]),
        .testTarget(name: "RecordingCoreTests", dependencies: ["RecordingCore"]),
        .testTarget(name: "RecordingContractsTests", dependencies: ["RecordingContracts"]),
    ],
    swiftLanguageModes: [.v6],
    cLanguageStandard: .c11
)
