import Foundation
import VoiceLoopCore

/// Deepgram `nova-3` streaming over a WebSocket. Port of the parameter contract
/// in `stt/deepgram.py`, moved from batch REST to `wss://api.deepgram.com/v1/listen`.
///
/// Four parameters carry the whole result and three are not defaults:
/// `language=multi` (Spanish with English technical terms in one breath),
/// `keyterm=` repeated (without it "mergealo" -> "MGalo", capped ~100), and
/// `smart_format=false` + `numerals=false` (numbers are normalized afterward by
/// `SpokenNormalizer`). Endpointing is *soltar la tecla*, not VAD: on `holdEnd`
/// send `Finalize` and flush. Interims go to the HUD; the final lands on release.
public final class DeepgramStream: NSObject, @unchecked Sendable {
    public static let host = "wss://api.deepgram.com/v1/listen"
    public static let maxKeyterms = 100

    /// Trim, drop blanks, de-duplicate case-insensitively, keep order, cap.
    public static func normalizeKeyterms(_ terms: [String]) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for term in terms {
            let cleaned = term.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            if cleaned.isEmpty { continue }
            let key = cleaned.lowercased()
            if seen.contains(key) { continue }
            seen.insert(key)
            out.append(cleaned)
            if out.count >= maxKeyterms { break }
        }
        return out
    }

    /// The streaming URL. Same non-default params as the REST engine, plus
    /// `encoding`/`sample_rate`/`interim_results` for the socket.
    public static func buildURL(
        model: String = "nova-3",
        language: String = "multi",
        sampleRate: Int = 16000,
        keyterms: [String] = []
    ) -> URL {
        var components = URLComponents(string: host)!
        var items: [URLQueryItem] = [
            URLQueryItem(name: "model", value: model),
            URLQueryItem(name: "language", value: language),
            URLQueryItem(name: "smart_format", value: "false"),
            URLQueryItem(name: "numerals", value: "false"),
            URLQueryItem(name: "interim_results", value: "true"),
            URLQueryItem(name: "encoding", value: "linear16"),
            URLQueryItem(name: "sample_rate", value: String(sampleRate)),
        ]
        for term in normalizeKeyterms(keyterms) {
            items.append(URLQueryItem(name: "keyterm", value: term))
        }
        components.queryItems = items
        return components.url!
    }

    public var onInterim: ((String) -> Void)?
    public var onFinal: ((String) -> Void)?

    private let apiKey: String
    private var task: URLSessionWebSocketTask?
    private var session: URLSession?
    private var finals: [String] = []

    public init(apiKey: String) {
        self.apiKey = apiKey
    }

    public func connect(keyterms: [String], sampleRate: Int = 16000) {
        var request = URLRequest(url: DeepgramStream.buildURL(sampleRate: sampleRate, keyterms: keyterms))
        request.setValue("Token \(apiKey)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .default)
        self.session = session
        let task = session.webSocketTask(with: request)
        self.task = task
        finals = []
        task.resume()
        receive()
    }

    /// Feed 16-bit PCM to the recognizer.
    public func send(pcm: Data) {
        task?.send(.data(pcm)) { _ in }
    }

    /// The tell that the take is over: flush and close the stream server-side.
    public func finalizeStream() {
        task?.send(.string("{\"type\":\"Finalize\"}")) { _ in }
        task?.send(.string("{\"type\":\"CloseStream\"}")) { _ in }
    }

    public func disconnect() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        session = nil
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                return
            case .success(let message):
                if case let .string(text) = message { self.handle(text) }
                self.receive()
            }
        }
    }

    private func handle(_ json: String) {
        guard let data = json.data(using: .utf8),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let channel = root["channel"] as? [String: Any],
              let alternatives = channel["alternatives"] as? [[String: Any]],
              let transcript = alternatives.first?["transcript"] as? String,
              !transcript.isEmpty else { return }
        let isFinal = root["is_final"] as? Bool ?? false
        if isFinal {
            finals.append(transcript)
            onFinal?(finals.joined(separator: " "))
        } else {
            onInterim?(transcript)
        }
    }
}
