import AudioToolbox
import CaptureBuffer
import CoreAudio
import Foundation

/// Device lifecycle belongs exclusively to the controller's serial device executor.
/// Backend implementations must not retain the context past successful disposal.
public protocol NativeCaptureBackend: AnyObject, Sendable {
    var isDisposed: Bool { get }
    func open(deviceUID: String, context: OpaquePointer) throws
    func start() throws
    func checkHealth() throws
    func stopAndDispose() throws
}

/// Production Audio Queue backend; construction and discovery do not open a microphone.
/// Only explicit controller start invokes open/start. All callback work is canonical C.
public final class CoreAudioCaptureBackend: NativeCaptureBackend, @unchecked Sendable {
    private var queue: AudioQueueRef?
    private var uid = ""
    private var context: OpaquePointer?
    private var started = false
    private let bufferCount = 8

    public init() {}

    public var isDisposed: Bool { queue == nil }

    private func check(_ status: OSStatus, _ operation: String) throws {
        guard status == noErr else {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Core Audio \(operation) failed",
                                       details: ["os_status=\(status)"])
        }
    }

    private static func fourCC(_ text: String) -> AudioQueuePropertyID {
        text.utf8.reduce(0) { ($0 << 8) | UInt32($1) }
    }

    private func queueValue<T>(_ selector: AudioQueuePropertyID, into value: inout T) throws {
        guard let queue else { throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "No native input queue exists") }
        var size = UInt32(MemoryLayout<T>.size)
        try withUnsafeMutablePointer(to: &value) { pointer in
            try check(AudioQueueGetProperty(queue, selector, pointer, &size), "read queue property")
        }
        guard size == MemoryLayout<T>.size else {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Core Audio property size changed")
        }
    }

    /// Bounded read-only identity preflight shared by UI preparation and actual open.
    /// It enumerates Core Audio properties only; no input queue or permission request is created.
    public func validateSelectedDevice(_ uid: String) throws {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                                mScope: kAudioObjectPropertyScopeGlobal,
                                                mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        try check(AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size), "enumerate device sizes")
        guard size > 0, size <= 1_048_576, size % UInt32(MemoryLayout<AudioDeviceID>.size) == 0 else {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Core Audio device list is empty or exceeds its bound")
        }
        var devices = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        try devices.withUnsafeMutableBytes { bytes in
            try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                                0, nil, &size, bytes.baseAddress!), "enumerate devices")
        }
        var matches = 0
        for device in devices {
            func string(_ selector: AudioObjectPropertySelector) throws -> String {
                var property = AudioObjectPropertyAddress(mSelector: selector,
                    mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
                var value: Unmanaged<CFString>?
                var count = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
                try check(AudioObjectGetPropertyData(device, &property, 0, nil, &count, &value), "read device identity")
                guard let value else { throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Core Audio device identity is missing") }
                // AudioObject CF-valued properties are owned by the caller.
                return value.takeRetainedValue() as String
            }
            if try string(kAudioDevicePropertyDeviceUID) == uid {
                guard try string(kAudioObjectPropertyName).lowercased().contains("umik-1") else {
                    throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native capture requires the selected UMIK-1")
                }
                var rateAddress = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate,
                    mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
                var rate: Float64 = 0
                var rateSize = UInt32(MemoryLayout<Float64>.size)
                try check(AudioObjectGetPropertyData(device, &rateAddress, 0, nil, &rateSize, &rate), "read selected device sample rate")
                guard rateSize == MemoryLayout<Float64>.size, rate == 48_000 else {
                    throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Selected microphone must be configured at 48 kHz; resampling is disabled")
                }
                matches += 1
            }
        }
        guard matches == 1 else {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Selected microphone UID is unavailable or ambiguous")
        }
    }

    public func open(deviceUID: String, context: OpaquePointer) throws {
        guard queue == nil else { throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "Native input queue is already open") }
        try validateSelectedDevice(deviceUID)
        self.uid = deviceUID
        self.context = context
        var format = AudioStreamBasicDescription(mSampleRate: 48_000, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
            mBytesPerPacket: 3, mFramesPerPacket: 1, mBytesPerFrame: 3,
            mChannelsPerFrame: 1, mBitsPerChannel: 24, mReserved: 0)
        var created: AudioQueueRef?
        try check(AudioQueueNewInput(&format, cvl_capture_input_callback(),
                                    UnsafeMutableRawPointer(context), nil, nil, 0, &created), "create input queue")
        guard let created else { throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Core Audio created a null input queue") }
        queue = created
        let selected: CFString = deviceUID as CFString
        var selectedPointer = Unmanaged.passUnretained(selected).toOpaque()
        try withExtendedLifetime(selected) {
            try check(AudioQueueSetProperty(created, Self.fourCC("aqcd"), &selectedPointer,
                                            UInt32(MemoryLayout<UnsafeMutableRawPointer>.size)), "select microphone UID")
        }
        var actual = AudioStreamBasicDescription()
        try queueValue(Self.fourCC("aqft"), into: &actual)
        guard cvl_capture_format_is_canonical(&actual) else {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native input is not canonical mono 48 kHz 24-bit PCM")
        }
        try checkHealth()
        for _ in 0..<bufferCount {
            var input: AudioQueueBufferRef?
            try check(AudioQueueAllocateBuffer(created, UInt32(CVL_CAPTURE_DEFAULT_CALLBACK_FRAMES * 3), &input), "allocate input buffer")
            guard let input else { throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Core Audio allocated a null input buffer") }
            try check(AudioQueueEnqueueBuffer(created, input, 0, nil), "enqueue input buffer")
        }
    }

    public func start() throws {
        guard let queue else { throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "Native input queue is not prepared") }
        try check(AudioQueueStart(queue, nil), "start input")
        started = true
    }

    public func checkHealth() throws {
        var selected: Unmanaged<CFString>?
        try queueValue(Self.fourCC("aqcd"), into: &selected)
        guard let selected else { throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native microphone UID disappeared") }
        guard selected.takeRetainedValue() as String == uid else {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native microphone UID changed")
        }
        var rate: Float64 = 0
        try queueValue(Self.fourCC("aqsr"), into: &rate)
        guard rate == 48_000 else { throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Microphone sample rate changed; resampling is disabled") }
        var format = AudioStreamBasicDescription()
        try queueValue(Self.fourCC("aqft"), into: &format)
        guard cvl_capture_format_is_canonical(&format) else { throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native capture format changed") }
        var converter: UInt32 = 0
        try queueValue(Self.fourCC("qcve"), into: &converter)
        guard converter == 0 else { throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native input converter failed", details: ["os_status=\(converter)"]) }
        var running: UInt32 = 0
        try queueValue(Self.fourCC("aqrn"), into: &running)
        if let context, started, cvl_capture_snapshot(context).received_frames > 0,
           !cvl_capture_snapshot(context).stop_requested, running == 0 {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native microphone stopped unexpectedly")
        }
    }

    public func stopAndDispose() throws {
        guard let queue else { context = nil; return }
        var stopStatus: OSStatus = noErr
        if started { stopStatus = AudioQueueStop(queue, true) }
        let disposeStatus = AudioQueueDispose(queue, true)
        if disposeStatus == noErr {
            self.queue = nil
            self.context = nil
            started = false
        }
        try check(disposeStatus, "dispose input")
        try check(stopStatus, "stop input")
    }
}
