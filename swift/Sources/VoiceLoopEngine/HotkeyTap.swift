import Foundation
import Carbon.HIToolbox
import VoiceLoopCore

/// The ⌥N / ⌥M push-to-talk keys, via Carbon `RegisterEventHotKey`.
///
/// This deliberately does NOT use a global `CGEventTap`: a listen tap needs the
/// Input Monitoring TCC grant, and that grant is pinned to the app's code
/// signature — every ad-hoc rebuild orphaned it and the keys went dead in
/// silence. `RegisterEventHotKey` registers just the two combos we own with the
/// window server and needs **no permission at all**, so it survives rebuilds.
///
/// A press emits `holdStart` (open the mic); the release is classified into
/// `holdEnd` (a real recording) or `shortTap` (a quick press with no voice — how
/// ⌥M confirms) by `GestureDiscriminator`. Esc-to-cancel is not wired here (a
/// global Esc hotkey would swallow every Escape); a hold simply ends on release.
public final class HotkeyTap: @unchecked Sendable {
    public var onGesture: ((Gesture) -> Void)?
    /// Whether the mic has measured speech since the current hold began.
    public var voiceProbe: (() -> Bool)?

    private let holdThreshold: TimeInterval
    private var refN: EventHotKeyRef?
    private var refM: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private var activeMode: GestureMode?
    private var pressStart: TimeInterval = 0

    private static let signature: OSType = 0x564C_4F50 // 'VLOP'
    private static let idN: UInt32 = 1
    private static let idM: UInt32 = 2

    public init(holdThreshold: TimeInterval = GestureDiscriminator.defaultHoldThreshold) {
        self.holdThreshold = holdThreshold
    }

    /// Register the two hotkeys and the press/release handler. Returns false only
    /// when a combo could not be registered (e.g. already claimed by another app)
    /// — never for a missing permission, because there is none to miss.
    @discardableResult
    public func start() -> Bool {
        let eventTypes = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)),
        ]
        let callback: EventHandlerUPP = { _, eventRef, userData in
            guard let userData, let eventRef else { return noErr }
            let me = Unmanaged<HotkeyTap>.fromOpaque(userData).takeUnretainedValue()
            var hkID = EventHotKeyID()
            GetEventParameter(
                eventRef, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                nil, MemoryLayout<EventHotKeyID>.size, nil, &hkID
            )
            me.handle(id: hkID.id, pressed: GetEventKind(eventRef) == UInt32(kEventHotKeyPressed))
            return noErr
        }
        InstallEventHandler(
            GetApplicationEventTarget(), callback, eventTypes.count, eventTypes,
            Unmanaged.passUnretained(self).toOpaque(), &handler
        )

        let optionMask = UInt32(optionKey)
        let okN = RegisterEventHotKey(
            UInt32(kVK_ANSI_N), optionMask,
            EventHotKeyID(signature: HotkeyTap.signature, id: HotkeyTap.idN),
            GetApplicationEventTarget(), 0, &refN
        )
        let okM = RegisterEventHotKey(
            UInt32(kVK_ANSI_M), optionMask,
            EventHotKeyID(signature: HotkeyTap.signature, id: HotkeyTap.idM),
            GetApplicationEventTarget(), 0, &refM
        )
        return okN == noErr && okM == noErr
    }

    public func stop() {
        if let refN { UnregisterEventHotKey(refN) }
        if let refM { UnregisterEventHotKey(refM) }
        if let handler { RemoveEventHandler(handler) }
        refN = nil
        refM = nil
        handler = nil
        activeMode = nil
    }

    private func handle(id: UInt32, pressed: Bool) {
        let mode: GestureMode = id == HotkeyTap.idN ? .raw : .smart
        if pressed {
            guard activeMode == nil else { return }
            activeMode = mode
            pressStart = ProcessInfo.processInfo.systemUptime
            onGesture?(.holdStart(mode))
        } else {
            guard let active = activeMode, active == mode else { return }
            let held = ProcessInfo.processInfo.systemUptime - pressStart
            activeMode = nil
            let gesture = GestureDiscriminator.classifyRelease(
                mode: mode, heldSeconds: held,
                hadVoice: voiceProbe?() ?? false, escaped: false,
                holdThreshold: holdThreshold
            )
            onGesture?(gesture)
        }
    }
}
