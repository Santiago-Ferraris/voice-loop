import Foundation
import CoreGraphics
import VoiceLoopCore

/// The ⌥N / ⌥M push-to-talk keys, via a global `CGEventTap`.
///
/// A press emits `holdStart` immediately (open the mic); the release is
/// classified into `holdEnd` (a real recording) or `shortTap` (a quick press
/// with no voice — how ⌥M confirms) by `GestureDiscriminator`. Esc during a hold
/// emits `cancel`. The tap re-enables itself after
/// `kCGEventTapDisabledByTimeout`, and does nothing at all without an
/// Accessibility grant — which the Doctor is what detects.
public final class HotkeyTap: @unchecked Sendable {
    public static let keyN: Int64 = 45
    public static let keyM: Int64 = 46
    public static let keyEscape: Int64 = 53

    public var onGesture: ((Gesture) -> Void)?
    /// Whether the mic has measured speech since the current hold began.
    public var voiceProbe: (() -> Bool)?

    private var tap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var activeMode: GestureMode?
    private var pressStart: TimeInterval = 0
    private let holdThreshold: TimeInterval

    public init(holdThreshold: TimeInterval = GestureDiscriminator.defaultHoldThreshold) {
        self.holdThreshold = holdThreshold
    }

    /// Install the tap. Returns false when the tap could not be created — almost
    /// always a missing Accessibility permission.
    @discardableResult
    public func start() -> Bool {
        let mask = (1 << CGEventType.keyDown.rawValue)
            | (1 << CGEventType.keyUp.rawValue)
            | (1 << CGEventType.flagsChanged.rawValue)
        let callback: CGEventTapCallBack = { _, type, event, refcon in
            guard let refcon else { return Unmanaged.passUnretained(event) }
            let me = Unmanaged<HotkeyTap>.fromOpaque(refcon).takeUnretainedValue()
            me.handle(type: type, event: event)
            return Unmanaged.passUnretained(event)
        }
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: CGEventMask(mask),
            callback: callback,
            userInfo: Unmanaged.passUnretained(self).toOpaque()
        ) else { return false }
        self.tap = tap
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        self.runLoopSource = source
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        return true
    }

    public func stop() {
        if let tap { CGEvent.tapEnable(tap: tap, enable: false) }
        if let source = runLoopSource { CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, .commonModes) }
        tap = nil
        runLoopSource = nil
        activeMode = nil
    }

    private func handle(type: CGEventType, event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return
        }
        let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
        let optionHeld = event.flags.contains(.maskAlternate)

        if type == .keyDown {
            if keyCode == HotkeyTap.keyEscape, activeMode != nil {
                emitCancel()
                return
            }
            guard optionHeld, activeMode == nil else { return }
            if keyCode == HotkeyTap.keyN { begin(.raw) }
            else if keyCode == HotkeyTap.keyM { begin(.smart) }
        } else if type == .keyUp {
            guard let mode = activeMode else { return }
            if keyCode == HotkeyTap.keyN || keyCode == HotkeyTap.keyM {
                end(mode)
            }
        }
    }

    private func begin(_ mode: GestureMode) {
        activeMode = mode
        pressStart = ProcessInfo.processInfo.systemUptime
        onGesture?(.holdStart(mode))
    }

    private func end(_ mode: GestureMode) {
        let held = ProcessInfo.processInfo.systemUptime - pressStart
        activeMode = nil
        let gesture = GestureDiscriminator.classifyRelease(
            mode: mode, heldSeconds: held,
            hadVoice: voiceProbe?() ?? false, escaped: false,
            holdThreshold: holdThreshold
        )
        onGesture?(gesture)
    }

    private func emitCancel() {
        activeMode = nil
        onGesture?(.cancel)
    }
}
