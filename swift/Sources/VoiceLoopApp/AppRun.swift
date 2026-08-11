import Foundation
import AppKit
import ServiceManagement
import VoiceLoopEngine
import VoiceLoopCore

/// Entry point for the menu-bar app. The Xcode app target's `main.swift` calls
/// `runVoiceLoopApp()`; keeping the run function in the library (rather than an
/// `@main`) lets the same code be exercised headlessly from tests.
public func runVoiceLoopApp() {
    let app = NSApplication.shared
    // LSUIElement is set in Info.plist; this mirrors it so a plain `swift run`
    // still comes up without a Dock icon.
    app.setActivationPolicy(.accessory)
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}

public final class AppDelegate: NSObject, NSApplicationDelegate {
    private var menuBar: MenuBarController?
    private var eventClient: EventClient?
    private let engine = Engine()

    public func applicationDidFinishLaunching(_ notification: Notification) {
        let menuBar = MenuBarController()
        self.menuBar = menuBar

        _ = engine.start()

        let client = EventClient(path: engine.socket.path)
        client.onEvent = { [weak menuBar] event in
            DispatchQueue.main.async { menuBar?.apply(event) }
        }
        client.start()
        self.eventClient = client

        registerLoginItem()
    }

    /// Register the app as a login item so it starts on boot. Best-effort: a
    /// failure is logged, not fatal.
    func registerLoginItem() {
        if #available(macOS 13.0, *) {
            do {
                if SMAppService.mainApp.status != .enabled {
                    try SMAppService.mainApp.register()
                }
            } catch {
                NSLog("voice-loop: could not register login item: \(error)")
            }
        }
    }

    public func applicationWillTerminate(_ notification: Notification) {
        engine.stop()
    }
}
