import Foundation
import VoiceLoopCore

/// Naming a fresh tab before it is handed its task. Injecting `/rename <slug>`
/// as the first write, then waiting for the input to be ready before the task
/// goes in, so the window is renamed *first* and the task lands in a named
/// window (verifiable in `~/.claude/sessions/*.json`).
///
/// `/rename` is a Claude Code slash command; it must be reconfirmed on the
/// installed Claude Code build in the manual phase. The fragile fallback is
/// writing `custom-title` straight into the session `.jsonl`.
public enum SessionRename {
    /// The exact first-write string for a rename: `/rename <slug>` + CR.
    public static func renameCommand(_ slug: String) -> String {
        "/rename \(slug)"
    }

    /// Inject the rename, wait for the prompt to settle (echo on screen, like
    /// `openTab` does), then inject the task. Returns whether the rename write
    /// went through.
    @discardableResult
    public static func renameThenTask(
        tty: String,
        slug: String,
        task: String,
        runner: ITermDispatch.Runner? = nil,
        settle: TimeInterval = 0.5,
        echoTimeout: TimeInterval = 10,
        poll: TimeInterval = 0.25,
        sleep: (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
        clock: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) throws -> Bool {
        try ITermDispatch.writeText(tty: tty, text: renameCommand(slug), newline: true, runner: runner)
        sleep(settle)
        _ = ITermDispatch.pollUntil(timeout: echoTimeout, poll: poll, sleep: sleep, clock: clock) {
            // The rename echoes and clears; a settled prompt is enough to type into.
            !ITermDispatch.sessionState(tty: tty, runner: runner).screen.contains("/rename")
        }
        if !task.trimmingCharacters(in: .whitespaces).isEmpty {
            try ITermDispatch.writeText(tty: tty, text: task, newline: false, runner: runner)
            let needle = String(task.trimmingCharacters(in: .whitespaces).prefix(16))
            let echoed = ITermDispatch.pollUntil(timeout: echoTimeout, poll: poll, sleep: sleep, clock: clock) {
                ITermDispatch.sessionState(tty: tty, runner: runner).screen.contains(needle)
            }
            if echoed { try ITermDispatch.sendKeys(tty: tty, keystrokes: [ITermDispatch.enter], runner: runner) }
        }
        return true
    }
}
