import Foundation
import AVFoundation
import VoiceLoopCore
#if canImport(ApplicationServices)
import ApplicationServices
#endif

/// The permission and hardware check. Enumerates inputs *by name*, reports input
/// gain, and drives each TCC prompt (mic, Accessibility, Automation) from a
/// foreground app so the dialogs actually appear (trap #8). CGEventTap without
/// an Accessibility grant is a silent no-op — this is what surfaces that.
public enum Doctor {
    public enum Permission: String, Sendable {
        case granted, denied, undetermined, unknown
    }

    public struct Report: Sendable {
        public var inputDeviceNames: [String]
        public var configuredDeviceResolved: String?
        public var inputGain: Int?
        public var microphone: Permission
        public var accessibility: Permission
        public var automation: Permission
    }

    /// Every audio input on this machine, by `localizedName`. Continuity puts the
    /// iPhone in here the moment it is nearby, which is exactly why devices are
    /// selected by name and never by index (trap #1).
    public static func inputDeviceNames() -> [String] {
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.microphone, .external],
            mediaType: .audio,
            position: .unspecified
        )
        return session.devices.map(\.localizedName)
    }

    public static func microphoneStatus() -> Permission {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .granted
        case .denied, .restricted: return .denied
        case .notDetermined: return .undetermined
        @unknown default: return .unknown
        }
    }

    /// Fires the mic TCC prompt. Async result reported via `completion`.
    public static func requestMicrophone(_ completion: @escaping @Sendable (Bool) -> Void) {
        AVCaptureDevice.requestAccess(for: .audio, completionHandler: completion)
    }

    public static func accessibilityStatus(prompt: Bool = false) -> Permission {
        #if canImport(ApplicationServices)
        // The key literal "AXTrustedCheckOptionPrompt" avoids the non-Sendable
        // global `kAXTrustedCheckOptionPrompt`.
        let options = ["AXTrustedCheckOptionPrompt" as CFString: prompt] as CFDictionary
        return AXIsProcessTrustedWithOptions(options) ? .granted : .denied
        #else
        return .unknown
        #endif
    }

    /// Drives the Automation (AppleEvents → iTerm2) prompt and reads the result.
    public static func automationStatus() -> Permission {
        let (ok, detail) = ITermDispatch.scriptingStatus()
        if ok { return .granted }
        return detail.contains("-1743") ? .denied : .undetermined
    }

    /// System input gain, 0–100. `nil` when it cannot be read. Trap #2: no
    /// threshold works at 27/100, so the number itself is worth surfacing.
    public static func inputGain() -> Int? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", "input volume of (get volume settings)"]
        let out = Pipe()
        process.standardOutput = out
        process.standardError = Pipe()
        do { try process.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return Int(String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "")
    }

    public static func run(configuredDevice: String) -> Report {
        let names = inputDeviceNames()
        return Report(
            inputDeviceNames: names,
            configuredDeviceResolved: MicCapture.resolveDeviceName(configuredDevice, among: names),
            inputGain: inputGain(),
            microphone: microphoneStatus(),
            accessibility: accessibilityStatus(),
            automation: automationStatus()
        )
    }
}
