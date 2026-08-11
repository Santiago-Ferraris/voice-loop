import Foundation

/// A serialized wrapper around macOS `say`. The mic is closed for the duration
/// of a locution (push-to-talk plus a closed mic during TTS is what evaporates
/// the echo the old system fought). Only ⌥M produces speech; there are never
/// spontaneous announcements.
public final class Tts: @unchecked Sendable {
    public var onStart: ((String) -> Void)?
    public var onFinish: ((String) -> Void)?
    /// Called around a locution so the caller can close and reopen the mic.
    public var willSpeak: (() -> Void)?
    public var didSpeak: (() -> Void)?

    private let queue = DispatchQueue(label: "com.voiceloop.tts")
    private var process: Process?

    public init() {}

    /// Speak `text`. Serialized: a second call waits for the first to finish.
    public func speak(_ text: String, voice: String? = nil) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        queue.async { [weak self] in
            guard let self else { return }
            self.willSpeak?()
            self.onStart?(trimmed)
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/say")
            var args: [String] = []
            if let voice { args += ["-v", voice] }
            args.append(trimmed)
            process.arguments = args
            self.process = process
            do {
                try process.run()
                process.waitUntilExit()
            } catch {
                // A missing `say` is not worth crashing the daemon over.
            }
            self.process = nil
            self.onFinish?(trimmed)
            self.didSpeak?()
        }
    }

    /// Interrupt the current locution (barge-in).
    public func stop() {
        queue.async { [weak self] in
            self?.process?.terminate()
        }
    }
}
