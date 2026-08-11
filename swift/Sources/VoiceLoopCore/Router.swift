import Foundation

/// The LLM that turns "decile a la de los conflictos que corra los tests" into a
/// resolved target and an ordered list of actions. It is a router/dispatcher,
/// never an executor: it decides *what* and *where*, the Engine does it.
///
/// Two implementations behind one protocol — `AnthropicRouter` (Haiku 4.5) and
/// `OpenAIRouter` (gpt-4o-mini). The config default is OpenAI, because that is
/// the key present out of the box; Haiku is a config flip plus an
/// `ANTHROPIC_API_KEY`.

public struct RouterDecision: Equatable, Sendable {
    public var target: RouterTarget
    public var rewritten: String
    public var actions: [RouterAction]
    public var confidence: Double
    public var needsConfirmation: Bool

    public init(target: RouterTarget, rewritten: String, actions: [RouterAction],
                confidence: Double, needsConfirmation: Bool) {
        self.target = target
        self.rewritten = rewritten
        self.actions = actions
        self.confidence = confidence
        self.needsConfirmation = needsConfirmation
    }
}

/// One live window, as the router needs to see it.
public struct RouterSessionInfo: Equatable, Sendable {
    public var name: String
    public var cwd: String
    public var status: String
    public init(name: String, cwd: String, status: String) {
        self.name = name
        self.cwd = cwd
        self.status = status
    }
}

public struct RouterRequest: Sendable {
    public var utterance: String
    public var sessions: [RouterSessionInfo]
    public var focusedIsClaude: Bool
    public init(utterance: String, sessions: [RouterSessionInfo], focusedIsClaude: Bool) {
        self.utterance = utterance
        self.sessions = sessions
        self.focusedIsClaude = focusedIsClaude
    }
}

public enum RouterError: Error, Equatable {
    case notConfigured
    case transport(String)
    case invalidResponse
    case schemaInvalid
}

public protocol Router: Sendable {
    var model: String { get }
    var available: Bool { get }
    func route(_ request: RouterRequest) async throws -> RouterDecision
}

/// The function-call schema both providers are handed. `target.kind` is one of
/// focused | new | named; `actions` are ordered and may compose.
public enum RouterSchema {
    public static let toolName = "route_command"

    public static var parametersJSON: [String: Any] { [
        "type": "object",
        "properties": [
            "target": [
                "type": "object",
                "properties": [
                    "kind": ["type": "string", "enum": ["focused", "new", "named"]],
                    "name": ["type": "string"],
                ],
                "required": ["kind"],
            ],
            "rewritten": ["type": "string"],
            "actions": [
                "type": "array",
                "items": [
                    "type": "object",
                    "properties": [
                        "op": ["type": "string", "enum": ["inject", "open_tab", "rename", "focus"]],
                        "text": ["type": "string"],
                    ],
                    "required": ["op"],
                ],
            ],
            "confidence": ["type": "number"],
            "needs_confirmation": ["type": "boolean"],
        ],
        "required": ["target", "rewritten", "actions", "confidence", "needs_confirmation"],
    ] }

    /// Validate the tool arguments into a decision, or nil if the shape is wrong.
    public static func validate(_ dict: [String: Any]) -> RouterDecision? {
        guard let target = RouterTarget.from(dict["target"]) else { return nil }
        if target.kind == .named && (target.name?.isEmpty ?? true) { return nil }
        guard let rewritten = dict["rewritten"] as? String else { return nil }
        guard let rawActions = dict["actions"] as? [Any] else { return nil }
        let actions = rawActions.compactMap(RouterAction.from)
        if actions.count != rawActions.count { return nil }
        guard let confidence = (dict["confidence"] as? NSNumber)?.doubleValue else { return nil }
        guard let needsConfirmation = dict["needs_confirmation"] as? Bool else { return nil }
        return RouterDecision(
            target: target, rewritten: rewritten, actions: actions,
            confidence: min(max(confidence, 0), 1), needsConfirmation: needsConfirmation
        )
    }
}

/// Runs `produce` (one API round trip returning the tool arguments), validates,
/// and retries once on a schema miss. Shared by both providers.
public func routeWithRetry(
    _ produce: () async throws -> [String: Any]
) async throws -> RouterDecision {
    for attempt in 0..<2 {
        let raw = try await produce()
        if let decision = RouterSchema.validate(raw) { return decision }
        if attempt == 1 { throw RouterError.schemaInvalid }
    }
    throw RouterError.schemaInvalid
}

public func routerSystemPrompt(_ request: RouterRequest) -> String {
    var lines = [
        "You route a spoken instruction to one Claude Code terminal window and describe the actions to take.",
        "Respond ONLY by calling the \(RouterSchema.toolName) function.",
        "target.kind: 'focused' = the window in front; 'named' = a window from the list (set name); 'new' = open a new tab.",
        "rewritten: the instruction cleaned up for Claude, in the language it was spoken.",
        "actions: ordered ops (inject/open_tab/rename/focus); a compound request runs them in order.",
        "needs_confirmation: true only when the target is ambiguous or confidence is low.",
        "Live windows:",
    ]
    if request.sessions.isEmpty {
        lines.append("  (none)")
    } else {
        for session in request.sessions {
            lines.append("  - name=\(session.name) cwd=\(session.cwd) status=\(session.status)")
        }
    }
    lines.append("Focused window is a Claude session: \(request.focusedIsClaude ? "yes" : "no").")
    return lines.joined(separator: "\n")
}
