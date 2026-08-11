import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public typealias RouterTransport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

public let defaultRouterTransport: RouterTransport = { request in
    try await URLSession.shared.data(for: request)
}

/// gpt-4o-mini via OpenAI chat-completions function calling. The config default:
/// the only key present out of the box.
public struct OpenAIRouter: Router {
    public let model: String
    let apiKey: String?
    let endpoint: URL
    let transport: RouterTransport

    public init(
        apiKey: String?,
        model: String = "gpt-4o-mini",
        endpoint: URL = URL(string: "https://api.openai.com/v1/chat/completions")!,
        transport: @escaping RouterTransport = defaultRouterTransport
    ) {
        self.apiKey = apiKey
        self.model = model
        self.endpoint = endpoint
        self.transport = transport
    }

    public var available: Bool { !(apiKey ?? "").isEmpty }

    public func route(_ request: RouterRequest) async throws -> RouterDecision {
        guard available else { throw RouterError.notConfigured }
        return try await routeWithRetry { try await callOnce(request) }
    }

    func callOnce(_ request: RouterRequest) async throws -> [String: Any] {
        let body: [String: Any] = [
            "model": model,
            "messages": [
                ["role": "system", "content": routerSystemPrompt(request)],
                ["role": "user", "content": request.utterance],
            ],
            "tools": [[
                "type": "function",
                "function": [
                    "name": RouterSchema.toolName,
                    "parameters": RouterSchema.parametersJSON,
                ],
            ]],
            "tool_choice": [
                "type": "function",
                "function": ["name": RouterSchema.toolName],
            ],
        ]
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(apiKey ?? "")", forHTTPHeaderField: "Authorization")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, _) = try await transport(req)
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let choices = root["choices"] as? [[String: Any]],
              let message = choices.first?["message"] as? [String: Any],
              let toolCalls = message["tool_calls"] as? [[String: Any]],
              let function = toolCalls.first?["function"] as? [String: Any],
              let arguments = function["arguments"] as? String,
              let argData = arguments.data(using: .utf8),
              let dict = (try? JSONSerialization.jsonObject(with: argData)) as? [String: Any]
        else { throw RouterError.invalidResponse }
        return dict
    }
}

/// Claude Haiku 4.5 via the Anthropic messages API, tool-calling. Behind a
/// config flip and an `ANTHROPIC_API_KEY`.
public struct AnthropicRouter: Router {
    public let model: String
    let apiKey: String?
    let endpoint: URL
    let transport: RouterTransport

    public init(
        apiKey: String?,
        model: String = "claude-haiku-4-5",
        endpoint: URL = URL(string: "https://api.anthropic.com/v1/messages")!,
        transport: @escaping RouterTransport = defaultRouterTransport
    ) {
        self.apiKey = apiKey
        self.model = model
        self.endpoint = endpoint
        self.transport = transport
    }

    public var available: Bool { !(apiKey ?? "").isEmpty }

    public func route(_ request: RouterRequest) async throws -> RouterDecision {
        guard available else { throw RouterError.notConfigured }
        return try await routeWithRetry { try await callOnce(request) }
    }

    func callOnce(_ request: RouterRequest) async throws -> [String: Any] {
        let body: [String: Any] = [
            "model": model,
            "max_tokens": 1024,
            "system": routerSystemPrompt(request),
            "messages": [["role": "user", "content": request.utterance]],
            "tools": [[
                "name": RouterSchema.toolName,
                "description": "Route the spoken instruction to a Claude window.",
                "input_schema": RouterSchema.parametersJSON,
            ]],
            "tool_choice": ["type": "tool", "name": RouterSchema.toolName],
        ]
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(apiKey ?? "", forHTTPHeaderField: "x-api-key")
        req.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, _) = try await transport(req)
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let content = root["content"] as? [[String: Any]] else {
            throw RouterError.invalidResponse
        }
        for block in content where block["type"] as? String == "tool_use" {
            if let input = block["input"] as? [String: Any] { return input }
        }
        throw RouterError.invalidResponse
    }
}
