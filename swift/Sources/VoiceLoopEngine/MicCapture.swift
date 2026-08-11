import Foundation
import AVFoundation
import CoreAudio
import VoiceLoopCore

/// Microphone capture, selecting the input device **by name** (from config, e.g.
/// `:MacBook Pro Microphone`) and never by index — `:0` is whatever CoreAudio
/// enumerates first, and Continuity puts the iPhone there the moment it is
/// nearby (trap #1). The device is re-resolved by name on a route change.
///
/// The by-name resolution is pure and tested; the AVAudioEngine capture is
/// verified live.
public final class MicCapture: @unchecked Sendable {
    /// Match a configured name (optionally `:`-prefixed) against the live device
    /// names. Exact case-insensitive match wins, then containment either way.
    /// Never falls back to an index — an unmatched name returns nil, loudly.
    public static func resolveDeviceName(_ configured: String, among names: [String]) -> String? {
        var wanted = configured
        if wanted.hasPrefix(":") { wanted.removeFirst() }
        wanted = wanted.trimmingCharacters(in: .whitespaces)
        if wanted.isEmpty { return names.first }
        let lowered = wanted.lowercased()
        if let exact = names.first(where: { $0.lowercased() == lowered }) { return exact }
        if let contains = names.first(where: { $0.lowercased().contains(lowered) }) { return contains }
        if let within = names.first(where: { lowered.contains($0.lowercased()) }) { return within }
        return nil
    }

    /// The CoreAudio device id whose name matches, or nil.
    public static func deviceID(forName configured: String) -> AudioDeviceID? {
        let devices = audioInputDevices()
        let names = devices.map(\.name)
        guard let resolved = resolveDeviceName(configured, among: names),
              let match = devices.first(where: { $0.name == resolved }) else { return nil }
        return match.id
    }

    struct Device { let id: AudioDeviceID; let name: String }

    static func audioInputDevices() -> [Device] {
        var size = UInt32(0)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else {
            return []
        }
        let count = Int(size) / MemoryLayout<AudioDeviceID>.size
        var ids = [AudioDeviceID](repeating: 0, count: count)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids) == noErr else {
            return []
        }
        return ids.compactMap { id in
            guard hasInputStreams(id), let name = deviceName(id) else { return nil }
            return Device(id: id, name: name)
        }
    }

    static func hasInputStreams(_ id: AudioDeviceID) -> Bool {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var size = UInt32(0)
        guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr, size > 0 else { return false }
        let buffer = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { buffer.deallocate() }
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, buffer) == noErr else { return false }
        let list = buffer.assumingMemoryBound(to: AudioBufferList.self)
        let buffers = UnsafeMutableAudioBufferListPointer(list)
        return buffers.contains { $0.mNumberChannels > 0 }
    }

    static func deviceName(_ id: AudioDeviceID) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var name: CFString = "" as CFString
        var size = UInt32(MemoryLayout<CFString>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &name) == noErr else { return nil }
        return name as String
    }

    // MARK: - capture

    public private(set) var level: Float = 0
    public var onFrames: (([Float]) -> Void)?
    public var onLevel: ((Float) -> Void)?

    private let engine = AVAudioEngine()
    private let configuredDevice: String
    private var capturing = false

    public init(configuredDevice: String) {
        self.configuredDevice = configuredDevice
    }

    /// Start capturing from the configured device. Selects it by name on the
    /// input node's audio unit; re-resolves on device-list changes.
    public func start() throws {
        guard !capturing else { return }
        if let id = MicCapture.deviceID(forName: configuredDevice) {
            var deviceID = id
            let unit = engine.inputNode.audioUnit
            if let unit {
                AudioUnitSetProperty(
                    unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                    &deviceID, UInt32(MemoryLayout<AudioDeviceID>.size)
                )
            }
        }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.consume(buffer)
        }
        engine.prepare()
        try engine.start()
        capturing = true
    }

    public func stop() {
        guard capturing else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        capturing = false
    }

    private func consume(_ buffer: AVAudioPCMBuffer) {
        guard let channel = buffer.floatChannelData?[0] else { return }
        let count = Int(buffer.frameLength)
        var frames = [Float](repeating: 0, count: count)
        var sumSquares: Float = 0
        for i in 0..<count {
            let sample = channel[i]
            frames[i] = sample
            sumSquares += sample * sample
        }
        let rms = count > 0 ? (sumSquares / Float(count)).squareRoot() : 0
        level = rms
        onLevel?(rms)
        onFrames?(frames)
    }
}
