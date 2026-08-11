import Foundation
import VoiceLoopCore
import Security

/// Runtime configuration and the secret store.
///
/// Config lives at `~/Library/Application Support/VoiceLoop/config.json` (device
/// *by name*, thresholds, router choice). Keys live in the Keychain, but the
/// read falls back to `~/.config/voice-loop/env` so the existing install works
/// with nothing typed in by hand (directive 4).
public struct Config: Codable, Equatable, Sendable {
    /// The router the daemon uses. Default is OpenAI: the only key present out of
    /// the box, so voice-loop is testable without an ANTHROPIC_API_KEY.
    public enum RouterKind: String, Codable, Sendable {
        case openai
        case anthropic
    }

    public var microphoneDevice: String
    public var silenceMinSeconds: Double
    public var silenceNoiseDb: Double
    public var silenceMinSpeechSeconds: Double
    public var newTabCommand: String
    public var router: RouterKind
    public var openAIModel: String
    public var anthropicModel: String

    public static let `default` = Config(
        microphoneDevice: ":MacBook Pro Microphone",
        silenceMinSeconds: 3.0,
        silenceNoiseDb: -40,
        silenceMinSpeechSeconds: 0.8,
        newTabCommand: "cd ~/Documents/darwin && claude",
        router: .openai,
        openAIModel: "gpt-4o-mini",
        anthropicModel: "claude-haiku-4-5"
    )

    public static func supportDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSString(string: "~/Library/Application Support").expandingTildeInPath)
        return base.appendingPathComponent("VoiceLoop", isDirectory: true)
    }

    public static func path() -> URL {
        supportDirectory().appendingPathComponent("config.json")
    }

    public static func load() -> Config {
        guard let data = try? Data(contentsOf: path()),
              let config = try? JSONDecoder().decode(Config.self, from: data) else {
            return .default
        }
        return config
    }

    public func save() throws {
        try FileManager.default.createDirectory(at: Config.supportDirectory(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(self).write(to: Config.path())
    }
}

/// The two secrets voice-loop needs, from the Keychain if present, otherwise
/// from the legacy env file.
public enum Secrets {
    public static let openAIKeyName = "OPENAI_API_KEY"
    public static let deepgramKeyName = "DEEPGRAM_API_KEY"
    public static let anthropicKeyName = "ANTHROPIC_API_KEY"

    /// Keychain first, then `~/.config/voice-loop/env`. `nil` when neither has it.
    public static func value(_ name: String) -> String? {
        if let fromKeychain = Keychain.read(name), !fromKeychain.isEmpty { return fromKeychain }
        let fromFile = EnvFile.read()[name]
        return (fromFile?.isEmpty == false) ? fromFile : nil
    }

    public static var openAIKey: String? { value(openAIKeyName) }
    public static var deepgramKey: String? { value(deepgramKeyName) }
    public static var anthropicKey: String? { value(anthropicKeyName) }
}

/// A minimal generic-password Keychain wrapper.
public enum Keychain {
    static let service = "com.voiceloop.keys"

    public static func read(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    public static func write(_ account: String, _ value: String) -> Bool {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
    }
}
