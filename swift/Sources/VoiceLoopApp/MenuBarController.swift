import AppKit
import VoiceLoopCore

/// The `NSStatusItem`: an icon that tracks Engine state (idle / listening /
/// processing / paused), a MODE indicator (N raw vs M smart), and a small menu
/// with settings, quit, and the last command.
@MainActor
public final class MenuBarController {
    private let statusItem: NSStatusItem
    private let lastCommandItem: NSMenuItem
    private var currentMode: EngineModeTag?

    public init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        lastCommandItem = NSMenuItem(title: "No command yet", action: nil, keyEquivalent: "")
        lastCommandItem.isEnabled = false
        configureMenu()
        render(state: .idle, mode: nil)
    }

    private func configureMenu() {
        let menu = NSMenu()
        menu.addItem(lastCommandItem)
        menu.addItem(.separator())
        let settings = NSMenuItem(title: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)
        let quit = NSMenuItem(title: "Quit VoiceLoop", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        statusItem.menu = menu
    }

    /// Drive the UI from one engine event.
    public func apply(_ event: EngineEvent) {
        switch event {
        case let .state(state, mode):
            render(state: state, mode: mode)
        case let .recordingStarted(mode):
            render(state: .listening, mode: mode)
        case let .finalTranscript(_, normalized):
            lastCommandItem.title = normalized.isEmpty ? "No command yet" : normalized
        case let .actionTaken(op, target, text, ok, _):
            let name = target?.name ?? target?.tty ?? ""
            let body = text ?? ""
            lastCommandItem.title = "\(ok ? "→" : "✗") \(op) \(name): \(body)".trimmingCharacters(in: .whitespaces)
        case let .error(code, message, _):
            lastCommandItem.title = "⚠︎ \(code): \(message)"
        default:
            break
        }
    }

    private func render(state: EngineState, mode: EngineModeTag?) {
        currentMode = mode
        let symbol: String
        switch state {
        case .idle: symbol = "mic"
        case .listening: symbol = "mic.fill"
        case .processing: symbol = "waveform"
        case .speaking: symbol = "speaker.wave.2.fill"
        case .paused: symbol = "pause.circle"
        }
        let button = statusItem.button
        button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: state.rawValue)
        button?.image?.isTemplate = true
        if let mode {
            button?.title = mode == .raw ? " N" : " M"
        } else {
            button?.title = ""
        }
    }

    @objc private func openSettings() {
        SettingsWindow.shared.show()
    }
}
