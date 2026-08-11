import Foundation
import VoiceLoopCore

/// Addressing an iTerm2 session by its tty and typing into it. Port of
/// `iterm.py`.
///
/// Every script is run as `osascript - <args>` against an `on run argv` handler,
/// so the tty, the text and the keystrokes travel as arguments — nothing is ever
/// interpolated into the script source, because the "user input" here is
/// whatever a speech recognizer just heard. Keystrokes cross the boundary as
/// comma-separated decimal code points (`27,91,66`); only digits and commas do.
public enum ITermDispatch {
    public enum AppleScriptError: Error, Equatable {
        case compileOrRun(String)
        /// The tty no longer belongs to any iTerm2 session — the window closed.
        case sessionGone(String)
        case scriptMissing(String)
    }

    // Keystrokes, as the raw sequences a terminal sends.
    public static let arrowUp = "\u{1b}[A"
    public static let arrowDown = "\u{1b}[B"
    public static let arrowRight = "\u{1b}[C"
    public static let arrowLeft = "\u{1b}[D"
    public static let enter = "\r"
    public static let escape = "\u{1b}"

    static let scriptNames = [
        "write_text", "send_keys", "find_session", "session_state",
        "open_tab", "focus", "current_session_tty", "ping",
    ]

    /// Runs `osascript - arg…` with `script` on stdin. Injectable for tests.
    public typealias Runner = (_ script: String, _ args: [String]) throws -> String

    static func scriptSource(_ name: String) throws -> String {
        guard let url = Bundle.module.url(forResource: name, withExtension: "applescript", subdirectory: "AppleScript"),
              let source = try? String(contentsOf: url, encoding: .utf8) else {
            throw AppleScriptError.scriptMissing(name)
        }
        return source
    }

    static var defaultRunner: Runner { { script, args in
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-"] + args
        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
        try process.run()
        stdin.fileHandleForWriting.write(Data(script.utf8))
        stdin.fileHandleForWriting.closeFile()
        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        let errData = stderr.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            let message = String(data: errData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            throw AppleScriptError.compileOrRun((message?.isEmpty == false ? message! : "osascript failed"))
        }
        return String(data: outData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    } }

    static func run(_ name: String, _ args: [String], runner: Runner? = nil) throws -> String {
        let source = try scriptSource(name)
        return try (runner ?? defaultRunner)(source, args)
    }

    /// `ESC [ B` -> `"27,91,66"`. Digits and commas are all that reach argv.
    public static func encodeKeystroke(_ sequence: String) throws -> String {
        guard !sequence.isEmpty else { throw AppleScriptError.compileOrRun("empty keystroke") }
        return sequence.unicodeScalars.map { String($0.value) }.joined(separator: ",")
    }

    // MARK: - operations

    public static func sessionExists(tty: String, runner: Runner? = nil) -> Bool {
        guard !tty.isEmpty else { return false }
        return (try? run("find_session", [tty], runner: runner)) == "found"
    }

    public static func sendKeys(tty: String, keystrokes: [String], runner: Runner? = nil) throws {
        guard !tty.isEmpty else { throw AppleScriptError.sessionGone("no tty") }
        if keystrokes.isEmpty { return }
        let args = [tty] + (try keystrokes.map(encodeKeystroke))
        let result = try run("send_keys", args, runner: runner)
        if result != "sent" { throw AppleScriptError.sessionGone("no iTerm2 session on \(tty)") }
    }

    /// Type `text`, and submit it unless `newline` is off. The Enter is a
    /// separate keystroke — see the AppleScript header. Trap #10.
    public static func writeText(tty: String, text: String, newline: Bool = true, runner: Runner? = nil) throws {
        guard !tty.isEmpty else { throw AppleScriptError.sessionGone("no tty") }
        if !text.isEmpty {
            let result = try run("write_text", [tty, text], runner: runner)
            if result != "sent" { throw AppleScriptError.sessionGone("no iTerm2 session on \(tty)") }
        }
        if newline { try sendKeys(tty: tty, keystrokes: [enter], runner: runner) }
    }

    /// (foreground job, visible screen). `("", "")` when there is no such session.
    public static func sessionState(tty: String, runner: Runner? = nil) -> (job: String, screen: String) {
        guard !tty.isEmpty, let raw = try? run("session_state", [tty], runner: runner) else { return ("", "") }
        let parts = raw.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
        let job = parts.first.map(String.init)?.trimmingCharacters(in: .whitespaces) ?? ""
        let screen = parts.count > 1 ? String(parts[1]) : ""
        return (job, screen)
    }

    public static func currentSessionTty(runner: Runner? = nil) -> String? {
        guard let raw = try? run("current_session_tty", [], runner: runner) else { return nil }
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }

    public static func focus(tty: String, runner: Runner? = nil) -> Bool {
        guard !tty.isEmpty else { return false }
        return (try? run("focus", [tty], runner: runner)) == "focused"
    }

    /// (can we drive iTerm2, why not). Automation denial is error -1743.
    public static func scriptingStatus(runner: Runner? = nil) -> (ok: Bool, detail: String) {
        do {
            let windows = try run("ping", [], runner: runner)
            return (true, "iTerm2 reachable, \(windows) window(s)")
        } catch {
            return (false, "\(error)")
        }
    }

    /// A new tab in the window you already have, optionally running `command` and
    /// then typing `text` into it. Returns the tty of the new tab. The text waits
    /// for the launch command to take the shell over and for the text to appear
    /// on screen before the Enter — both are ceilings, not fixed waits.
    @discardableResult
    public static func openTab(
        command: String = "",
        text: String = "",
        runner: Runner? = nil,
        launchTimeout: TimeInterval = 20,
        echoTimeout: TimeInterval = 10,
        poll: TimeInterval = 0.25,
        settle: TimeInterval = 0.5,
        sleep: (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
        clock: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) throws -> String? {
        let opened = try run("open_tab", [command], runner: runner)
        let parts = opened.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
        let tty = parts.first.map(String.init)?.trimmingCharacters(in: .whitespaces) ?? ""
        let launcher = parts.count > 1 ? String(parts[1]).trimmingCharacters(in: .whitespaces) : ""
        guard !tty.isEmpty else { return nil }
        if text.trimmingCharacters(in: .whitespaces).isEmpty { return tty }

        if !command.isEmpty && !launcher.isEmpty {
            let started = pollUntil(timeout: launchTimeout, poll: poll, sleep: sleep, clock: clock) {
                let job = sessionState(tty: tty, runner: runner).job
                return job != "" && job != launcher
            }
            if !started {
                try writeText(tty: tty, text: text, newline: false, runner: runner)
                return tty
            }
        }
        sleep(settle)
        try writeText(tty: tty, text: text, newline: false, runner: runner)
        let needle = String(text.trimmingCharacters(in: .whitespaces).prefix(16))
        let echoed = pollUntil(timeout: echoTimeout, poll: poll, sleep: sleep, clock: clock) {
            sessionState(tty: tty, runner: runner).screen.contains(needle)
        }
        if echoed { try sendKeys(tty: tty, keystrokes: [enter], runner: runner) }
        return tty
    }

    static func pollUntil(
        timeout: TimeInterval, poll: TimeInterval,
        sleep: (TimeInterval) -> Void, clock: () -> TimeInterval,
        predicate: () -> Bool
    ) -> Bool {
        let deadline = clock() + timeout
        while true {
            if predicate() { return true }
            if clock() >= deadline { return false }
            sleep(poll)
        }
    }
}
