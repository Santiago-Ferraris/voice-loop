import Foundation
import VoiceLoopCore

/// The headless engine: config + socket + router + orchestrator + hotkeys,
/// wired together. The app starts this and subscribes to its socket; it runs
/// without any UI at all, which is the point.
public final class Engine: @unchecked Sendable {
    public let config: Config
    public let socket: EventSocket
    private let router: Router
    private let tts: Tts
    private let orchestrator: Orchestrator
    private let hotkeys: HotkeyTap

    public init(config: Config = .load()) {
        self.config = config
        self.tts = Tts()
        self.router = Engine.makeRouter(config)
        self.socket = EventSocket()
        self.orchestrator = Orchestrator(
            config: config, socket: socket, router: router, tts: tts, deepgramKey: Secrets.deepgramKey
        )
        self.hotkeys = HotkeyTap(holdThreshold: GestureDiscriminator.defaultHoldThreshold)
        socket.onCommandHandler = { [weak self] command in self?.orchestrator.handle(command) }
        hotkeys.voiceProbe = { [weak self] in self?.orchestrator.voiceProbe() ?? false }
        hotkeys.onGesture = { [weak self] gesture in self?.orchestrator.handle(gesture) }
    }

    /// The router config default is OpenAI (the key present out of the box);
    /// Anthropic/Haiku is a config flip plus an ANTHROPIC_API_KEY.
    public static func makeRouter(_ config: Config) -> Router {
        switch config.router {
        case .anthropic:
            return AnthropicRouter(apiKey: Secrets.anthropicKey, model: config.anthropicModel)
        case .openai:
            return OpenAIRouter(apiKey: Secrets.openAIKey, model: config.openAIModel)
        }
    }

    /// Whether the hotkey tap installed — false almost always means a missing
    /// Accessibility grant (the Doctor is what surfaces that).
    @discardableResult
    public func start() -> Bool {
        try? socket.start()
        return hotkeys.start()
    }

    public func stop() {
        hotkeys.stop()
        socket.stop()
    }

    public func runDoctor() -> Doctor.Report {
        Doctor.run(configuredDevice: config.microphoneDevice)
    }
}
