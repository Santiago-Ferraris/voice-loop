import Foundation

/// The event schema carried over the Engine↔UI Unix socket, as JSONL. The UI is
/// a subscriber of this contract and nothing more, which is what lets "works
/// without a UI" and "another OS can write its own" both be true. See
/// `docs/event-schema.md`.
///
/// Wire envelope: `{"v":1,"type":<str>,"ts":<ms>,"seq":<n>, …payload}`. The
/// payload fields sit alongside the envelope, flattened, so a subscriber reads
/// `type` and then the fields it names.

public let engineSchemaVersion = 1

public enum EngineState: String, Codable, Equatable, Sendable {
    case idle, listening, processing, speaking, paused
}

/// Which key is live: `raw` (⌥N), `smart` (⌥M), or none.
public enum EngineModeTag: String, Codable, Equatable, Sendable {
    case raw, smart
}

public struct RouterTarget: Equatable, Sendable, Codable {
    public enum Kind: String, Codable, Equatable, Sendable {
        case focused, new, named
    }
    public var kind: Kind
    public var name: String?
    public var tty: String?

    public init(kind: Kind, name: String? = nil, tty: String? = nil) {
        self.kind = kind
        self.name = name
        self.tty = tty
    }

    func toDict() -> [String: Any] {
        var out: [String: Any] = ["kind": kind.rawValue]
        if let name { out["name"] = name }
        if let tty { out["tty"] = tty }
        return out
    }

    static func from(_ any: Any?) -> RouterTarget? {
        guard let dict = any as? [String: Any],
              let kindRaw = dict["kind"] as? String,
              let kind = Kind(rawValue: kindRaw) else { return nil }
        return RouterTarget(kind: kind, name: dict["name"] as? String, tty: dict["tty"] as? String)
    }
}

public struct RouterAction: Equatable, Sendable, Codable {
    /// inject | open_tab | rename | focus
    public var op: String
    public var target: RouterTarget?
    public var text: String?

    public init(op: String, target: RouterTarget? = nil, text: String? = nil) {
        self.op = op
        self.target = target
        self.text = text
    }

    func toDict() -> [String: Any] {
        var out: [String: Any] = ["op": op]
        if let target { out["target"] = target.toDict() }
        if let text { out["text"] = text }
        return out
    }

    static func from(_ any: Any?) -> RouterAction? {
        guard let dict = any as? [String: Any], let op = dict["op"] as? String else { return nil }
        return RouterAction(op: op, target: RouterTarget.from(dict["target"]), text: dict["text"] as? String)
    }
}

/// Engine → subscriber. One case per `type`.
public enum EngineEvent: Equatable, Sendable {
    case hello(version: Int, modes: [String], capabilities: [String])
    case state(state: EngineState, mode: EngineModeTag?)
    case recordingStarted(mode: EngineModeTag)
    case interimTranscript(text: String)
    case finalTranscript(text: String, normalized: String)
    case routerResult(target: RouterTarget, rewritten: String, actions: [RouterAction],
                      confidence: Double, needsConfirmation: Bool, prompt: String?)
    case namingPrompt(suggested: String, taskPreview: String)
    case actionTaken(op: String, target: RouterTarget?, text: String?, ok: Bool, detail: String?)
    case ttsStarted(text: String)
    case ttsFinished(text: String)
    case recordingCancelled(reason: String)
    case error(code: String, message: String, hint: String?)

    public var typeName: String {
        switch self {
        case .hello: return "hello"
        case .state: return "state"
        case .recordingStarted: return "recording_started"
        case .interimTranscript: return "interim_transcript"
        case .finalTranscript: return "final_transcript"
        case .routerResult: return "router_result"
        case .namingPrompt: return "naming_prompt"
        case .actionTaken: return "action_taken"
        case .ttsStarted: return "tts_started"
        case .ttsFinished: return "tts_finished"
        case .recordingCancelled: return "recording_cancelled"
        case .error: return "error"
        }
    }

    func payloadDict() -> [String: Any] {
        switch self {
        case let .hello(version, modes, capabilities):
            return ["version": version, "modes": modes, "capabilities": capabilities]
        case let .state(state, mode):
            var d: [String: Any] = ["state": state.rawValue]
            d["mode"] = mode?.rawValue as Any? ?? NSNull()
            return d
        case let .recordingStarted(mode):
            return ["mode": mode.rawValue]
        case let .interimTranscript(text):
            return ["text": text]
        case let .finalTranscript(text, normalized):
            return ["text": text, "normalized": normalized]
        case let .routerResult(target, rewritten, actions, confidence, needsConfirmation, prompt):
            var d: [String: Any] = [
                "target": target.toDict(), "rewritten": rewritten,
                "actions": actions.map { $0.toDict() },
                "confidence": confidence, "needs_confirmation": needsConfirmation,
            ]
            if let prompt { d["prompt"] = prompt }
            return d
        case let .namingPrompt(suggested, taskPreview):
            return ["suggested": suggested, "task_preview": taskPreview]
        case let .actionTaken(op, target, text, ok, detail):
            var d: [String: Any] = ["op": op, "ok": ok]
            if let target { d["target"] = target.toDict() }
            if let text { d["text"] = text }
            if let detail { d["detail"] = detail }
            return d
        case let .ttsStarted(text), let .ttsFinished(text):
            return ["text": text]
        case let .recordingCancelled(reason):
            return ["reason": reason]
        case let .error(code, message, hint):
            var d: [String: Any] = ["code": code, "message": message]
            if let hint { d["hint"] = hint }
            return d
        }
    }

    static func from(type: String, fields d: [String: Any]) -> EngineEvent? {
        switch type {
        case "hello":
            return .hello(
                version: d["version"] as? Int ?? engineSchemaVersion,
                modes: d["modes"] as? [String] ?? [],
                capabilities: d["capabilities"] as? [String] ?? []
            )
        case "state":
            guard let raw = d["state"] as? String, let state = EngineState(rawValue: raw) else { return nil }
            let mode = (d["mode"] as? String).flatMap(EngineModeTag.init(rawValue:))
            return .state(state: state, mode: mode)
        case "recording_started":
            guard let raw = d["mode"] as? String, let mode = EngineModeTag(rawValue: raw) else { return nil }
            return .recordingStarted(mode: mode)
        case "interim_transcript":
            return .interimTranscript(text: d["text"] as? String ?? "")
        case "final_transcript":
            return .finalTranscript(text: d["text"] as? String ?? "", normalized: d["normalized"] as? String ?? "")
        case "router_result":
            guard let target = RouterTarget.from(d["target"]) else { return nil }
            let actions = (d["actions"] as? [Any] ?? []).compactMap(RouterAction.from)
            return .routerResult(
                target: target,
                rewritten: d["rewritten"] as? String ?? "",
                actions: actions,
                confidence: (d["confidence"] as? NSNumber)?.doubleValue ?? 0,
                needsConfirmation: d["needs_confirmation"] as? Bool ?? false,
                prompt: d["prompt"] as? String
            )
        case "naming_prompt":
            return .namingPrompt(suggested: d["suggested"] as? String ?? "", taskPreview: d["task_preview"] as? String ?? "")
        case "action_taken":
            return .actionTaken(
                op: d["op"] as? String ?? "",
                target: RouterTarget.from(d["target"]),
                text: d["text"] as? String,
                ok: d["ok"] as? Bool ?? false,
                detail: d["detail"] as? String
            )
        case "tts_started":
            return .ttsStarted(text: d["text"] as? String ?? "")
        case "tts_finished":
            return .ttsFinished(text: d["text"] as? String ?? "")
        case "recording_cancelled":
            return .recordingCancelled(reason: d["reason"] as? String ?? "")
        case "error":
            return .error(code: d["code"] as? String ?? "", message: d["message"] as? String ?? "", hint: d["hint"] as? String)
        default:
            return nil
        }
    }
}

/// Envelope + event, the unit written to the socket.
public struct EventEnvelope: Equatable, Sendable {
    public var v: Int
    public var ts: Int
    public var seq: Int
    public var event: EngineEvent

    public init(event: EngineEvent, ts: Int, seq: Int, v: Int = engineSchemaVersion) {
        self.v = v
        self.ts = ts
        self.seq = seq
        self.event = event
    }

    /// One JSONL line (no trailing newline; the socket writer adds it).
    public func jsonLine() -> String {
        var dict: [String: Any] = ["v": v, "type": event.typeName, "ts": ts, "seq": seq]
        for (key, value) in event.payloadDict() { dict[key] = value }
        guard let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
              let line = String(data: data, encoding: .utf8) else { return "{}" }
        return line
    }

    public static func decode(_ line: String) -> EventEnvelope? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = trimmed.data(using: .utf8),
              let dict = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let type = dict["type"] as? String,
              let event = EngineEvent.from(type: type, fields: dict) else { return nil }
        return EventEnvelope(
            event: event,
            ts: dict["ts"] as? Int ?? 0,
            seq: dict["seq"] as? Int ?? 0,
            v: dict["v"] as? Int ?? engineSchemaVersion
        )
    }
}

/// Subscriber → Engine.
public enum EngineCommand: String, Equatable, Sendable, Codable {
    case confirm, cancel, pause, resume, quit
    case getState = "get_state"

    public func jsonLine() -> String {
        #"{"cmd":"# + "\"\(rawValue)\"" + "}"
    }

    public static func decode(_ line: String) -> EngineCommand? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = trimmed.data(using: .utf8),
              let dict = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let cmd = dict["cmd"] as? String else { return nil }
        return EngineCommand(rawValue: cmd)
    }
}
