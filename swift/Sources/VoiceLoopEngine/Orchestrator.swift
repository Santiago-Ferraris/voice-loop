import Foundation
import VoiceLoopCore

/// The state machine that ties gestures → recording → STT → dispatch.
///
/// ⌥N (raw): dictate verbatim into the focused Claude window, no rewrite, no
/// confirmation. ⌥M (smart): route through the LLM, resolve the target, confirm
/// only when ambiguous, run the actions in order. Every transition is announced
/// on the event socket, which is the whole UI contract.
public final class Orchestrator: @unchecked Sendable {
    private let config: Config
    private let socket: EventSocket
    private let router: Router
    private let tts: Tts
    private let mic: MicCapture
    private let deepgramKey: String?

    private var stream: DeepgramStream?
    private var currentMode: GestureMode?
    private var latestFinal: String = ""
    private var hadVoice = false
    private var pendingConfirmation: (() -> Void)?

    public init(config: Config, socket: EventSocket, router: Router, tts: Tts, deepgramKey: String?) {
        self.config = config
        self.socket = socket
        self.router = router
        self.tts = tts
        self.deepgramKey = deepgramKey
        self.mic = MicCapture(configuredDevice: config.microphoneDevice)
        self.mic.onLevel = { [weak self] level in
            if level > 0.01 { self?.hadVoice = true }
        }
    }

    /// True when the current hold has carried speech energy — the hotkey's
    /// tap-vs-hold discriminator reads this.
    public func voiceProbe() -> Bool { hadVoice }

    public func handle(_ gesture: Gesture) {
        switch gesture {
        case let .holdStart(mode):
            beginRecording(mode)
        case let .holdEnd(mode):
            endRecording(mode)
        case let .shortTap(mode):
            if mode == .smart { confirmPending() }
            // ⌥N confirms nothing; a raw short tap is ignored.
        case .cancel:
            cancelRecording(reason: "esc")
        }
    }

    public func handle(_ command: EngineCommand) {
        switch command {
        case .confirm: confirmPending()
        case .cancel: cancelRecording(reason: "esc")
        case .getState: socket.broadcast(.state(state: .idle, mode: nil))
        case .pause, .resume, .quit: break
        }
    }

    // MARK: - recording

    private func beginRecording(_ mode: GestureMode) {
        currentMode = mode
        hadVoice = false
        latestFinal = ""
        let tag: EngineModeTag = mode == .raw ? .raw : .smart
        socket.broadcast(.recordingStarted(mode: tag))
        socket.broadcast(.state(state: .listening, mode: tag))

        if let deepgramKey {
            let keyterms = liveKeyterms()
            let stream = DeepgramStream(apiKey: deepgramKey)
            stream.onInterim = { [weak self] text in self?.socket.broadcast(.interimTranscript(text: text)) }
            stream.onFinal = { [weak self] text in self?.latestFinal = text }
            stream.connect(keyterms: keyterms)
            self.stream = stream
            mic.onFrames = { [weak self] frames in
                self?.stream?.send(pcm: pcm16(from: frames))
            }
            try? mic.start()
        }
    }

    private func endRecording(_ mode: GestureMode) {
        stream?.finalizeStream()
        mic.stop()
        let tag: EngineModeTag = mode == .raw ? .raw : .smart
        socket.broadcast(.state(state: .processing, mode: tag))
        let raw = latestFinal
        let normalized = SpokenNormalizer.normalize(raw)
        socket.broadcast(.finalTranscript(text: raw, normalized: normalized))
        stream?.disconnect()
        stream = nil
        currentMode = nil

        if normalized.trimmingCharacters(in: .whitespaces).isEmpty {
            socket.broadcast(.recordingCancelled(reason: "no_speech"))
            socket.broadcast(.state(state: .idle, mode: nil))
            return
        }
        switch mode {
        case .raw: dispatchRaw(normalized)
        case .smart: dispatchSmart(normalized)
        }
    }

    private func cancelRecording(reason: String) {
        stream?.finalizeStream()
        stream?.disconnect()
        stream = nil
        mic.stop()
        currentMode = nil
        socket.broadcast(.recordingCancelled(reason: reason))
        socket.broadcast(.state(state: .idle, mode: nil))
    }

    // MARK: - ⌥N raw

    private func dispatchRaw(_ text: String) {
        guard TtyService.frontmostIsITerm2(), let tty = ITermDispatch.currentSessionTty() else {
            socket.broadcast(.error(code: "no_claude_window", message: "No Claude window in front", hint: nil))
            socket.broadcast(.state(state: .idle, mode: nil))
            return
        }
        let rosterPids = Set(Roster.load(interactiveOnly: true).values.map(\.pid))
        guard TtyService.claudePid(onTty: tty, rosterPids: rosterPids) != nil else {
            socket.broadcast(.error(code: "no_claude_window", message: "Focused session is not Claude", hint: nil))
            socket.broadcast(.state(state: .idle, mode: nil))
            return
        }
        inject(tty: tty, text: text)
    }

    // MARK: - ⌥M smart

    private func dispatchSmart(_ text: String) {
        let sessions = Roster.load(interactiveOnly: true).values
        let request = RouterRequest(
            utterance: text,
            sessions: sessions.map { RouterSessionInfo(name: $0.name, cwd: $0.cwd, status: $0.status) },
            focusedIsClaude: TtyService.frontmostIsITerm2()
        )
        Task { [weak self] in
            guard let self else { return }
            do {
                let decision = try await self.router.route(request)
                self.applyDecision(decision, sessions: Array(sessions))
            } catch {
                self.socket.broadcast(.error(code: "router_failed", message: "\(error)", hint: nil))
                self.socket.broadcast(.state(state: .idle, mode: nil))
            }
        }
    }

    private func applyDecision(_ decision: RouterDecision, sessions: [Roster.Session]) {
        socket.broadcast(.routerResult(
            target: decision.target, rewritten: decision.rewritten, actions: decision.actions,
            confidence: decision.confidence, needsConfirmation: decision.needsConfirmation,
            prompt: nil
        ))
        let windows = sessions.map { TargetResolver.Window(name: $0.name, tty: TtyService.pidToTty($0.pid)) }
        let focusedTty = ITermDispatch.currentSessionTty()
        let resolved = TargetResolver.resolve(
            target: decision.target, windows: windows,
            focusedTty: focusedTty, focusedIsClaude: TtyService.frontmostIsITerm2(),
            confidence: decision.confidence, needsConfirmation: decision.needsConfirmation
        )
        switch resolved {
        case let .tty(tty):
            runActions(decision, tty: tty)
        case .newTab:
            runNewTab(decision)
        case let .needsConfirmation(name):
            let prompt = decision.rewritten
            pendingConfirmation = { [weak self] in self?.applyResolvedConfirmed(decision, sessions: sessions) }
            let preview = name.isEmpty ? prompt : "\(name): \(prompt)"
            tts.speak(preview)
        case .notClaude:
            socket.broadcast(.recordingCancelled(reason: "not_claude"))
            socket.broadcast(.state(state: .idle, mode: nil))
        }
    }

    private func applyResolvedConfirmed(_ decision: RouterDecision, sessions: [Roster.Session]) {
        // On confirmation, act on the best target we have.
        if decision.target.kind == .new { runNewTab(decision); return }
        let windows = sessions.map { TargetResolver.Window(name: $0.name, tty: TtyService.pidToTty($0.pid)) }
        if decision.target.kind == .named,
           let match = windows.first(where: { foldWords($0.name) == foldWords(decision.target.name ?? "") }),
           let tty = match.tty {
            runActions(decision, tty: tty)
        } else if let tty = ITermDispatch.currentSessionTty() {
            runActions(decision, tty: tty)
        } else {
            socket.broadcast(.state(state: .idle, mode: nil))
        }
    }

    private func confirmPending() {
        guard let pending = pendingConfirmation else { return }
        pendingConfirmation = nil
        pending()
    }

    private func runActions(_ decision: RouterDecision, tty: String) {
        for action in decision.actions {
            switch action.op {
            case "inject":
                inject(tty: tty, text: action.text ?? decision.rewritten)
            case "focus":
                let ok = ITermDispatch.focus(tty: tty)
                socket.broadcast(.actionTaken(op: "focus", target: RouterTarget(kind: .focused, tty: tty), text: nil, ok: ok, detail: nil))
            default:
                inject(tty: tty, text: action.text ?? decision.rewritten)
            }
        }
        if decision.actions.isEmpty {
            inject(tty: tty, text: decision.rewritten)
        }
        socket.broadcast(.state(state: .idle, mode: nil))
    }

    private func runNewTab(_ decision: RouterDecision) {
        let task = decision.rewritten
        let slug = SessionName.slugify(decision.target.name ?? task)
        do {
            if let tty = try ITermDispatch.openTab(command: config.newTabCommand) {
                try SessionRename.renameThenTask(tty: tty, slug: slug, task: task)
                socket.broadcast(.actionTaken(op: "open_tab", target: RouterTarget(kind: .new, name: slug, tty: tty), text: task, ok: true, detail: nil))
            }
        } catch {
            socket.broadcast(.error(code: "session_gone", message: "\(error)", hint: nil))
        }
        socket.broadcast(.state(state: .idle, mode: nil))
    }

    private func inject(tty: String, text: String) {
        do {
            try ITermDispatch.writeText(tty: tty, text: text, newline: false)
            try ITermDispatch.sendKeys(tty: tty, keystrokes: [ITermDispatch.enter])
            socket.broadcast(.actionTaken(op: "inject", target: RouterTarget(kind: .focused, tty: tty), text: text, ok: true, detail: nil))
        } catch {
            socket.broadcast(.error(code: "session_gone", message: "\(error)", hint: nil))
        }
        socket.broadcast(.state(state: .idle, mode: nil))
    }

    private func liveKeyterms() -> [String] {
        let names = Roster.load(interactiveOnly: true).values.compactMap { $0.announceName }
        let vocab = Vocabulary.load(stateDir: NSString(string: "~/.local/state/voice-loop").expandingTildeInPath)
        return names + vocab
    }
}

/// Float samples (−1…1) to 16-bit little-endian PCM.
func pcm16(from frames: [Float]) -> Data {
    var data = Data(capacity: frames.count * 2)
    for sample in frames {
        let clamped = max(-1, min(1, sample))
        let value = Int16(clamped * Float(Int16.max))
        withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
    }
    return data
}
