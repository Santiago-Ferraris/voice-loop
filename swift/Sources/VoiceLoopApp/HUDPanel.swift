import SwiftUI
import AppKit
import VoiceLoopCore

/// The HUD that drops in while ⌥N/⌥M is held: the live transcript, the
/// `→ [window]: «…»` delivery line, and confirmations. It closes on release or
/// once the action is taken, and shows the current mode. It is driven entirely
/// by the event stream.
public final class HUDState: ObservableObject {
    @Published public var visible = false
    @Published public var mode: EngineModeTag?
    @Published public var interim = ""
    @Published public var deliveryLine = ""
    @Published public var confirmation = ""

    public init() {}

    public func apply(_ event: EngineEvent) {
        switch event {
        case let .recordingStarted(mode):
            self.mode = mode
            interim = ""
            deliveryLine = ""
            confirmation = ""
            visible = true
        case let .interimTranscript(text):
            interim = text
        case let .finalTranscript(_, normalized):
            interim = normalized
        case let .routerResult(target, rewritten, _, _, needsConfirmation, _):
            let name = target.name ?? (target.kind == .new ? "new tab" : "focused")
            deliveryLine = "→ [\(name)]: «\(rewritten)»"
            if needsConfirmation { confirmation = "¿\(rewritten)?" }
        case let .actionTaken(_, target, text, _, _):
            let name = target?.name ?? target?.tty ?? ""
            deliveryLine = "→ [\(name)]: «\(text ?? "")»"
            visible = false
        case .recordingCancelled, .error:
            visible = false
        default:
            break
        }
    }
}

struct HUDView: View {
    @ObservedObject var state: HUDState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Circle()
                    .fill(state.mode == .raw ? Color.blue : Color.purple)
                    .frame(width: 8, height: 8)
                Text(state.mode == .raw ? "N · raw" : "M · smart")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !state.interim.isEmpty {
                Text(state.interim).font(.title3)
            }
            if !state.deliveryLine.isEmpty {
                Text(state.deliveryLine).font(.callout).foregroundStyle(.secondary)
            }
            if !state.confirmation.isEmpty {
                Text(state.confirmation).font(.callout).bold()
            }
        }
        .padding(16)
        .frame(width: 420, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
    }
}

/// The floating, non-activating HUD panel.
@MainActor
public final class HUDPanel {
    private let panel: NSPanel
    private let state = HUDState()

    public init() {
        let hosting = NSHostingController(rootView: HUDView(state: state))
        panel = NSPanel(contentViewController: hosting)
        panel.styleMask = [.nonactivatingPanel, .borderless]
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
    }

    public func apply(_ event: EngineEvent) {
        state.apply(event)
        if state.visible { showCentered() } else { panel.orderOut(nil) }
    }

    private func showCentered() {
        if let screen = NSScreen.main {
            let frame = panel.frame
            let x = screen.frame.midX - frame.width / 2
            let y = screen.frame.height * 0.28
            panel.setFrameOrigin(NSPoint(x: x, y: y))
        }
        panel.orderFrontRegardless()
    }
}
