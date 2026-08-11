import Foundation
import VoiceLoopCore
#if canImport(AppKit)
import AppKit
#endif

/// Shelling `ps` for the tty resolution the roster does not carry, and asking
/// AppKit whether iTerm2 is the frontmost app. The parsing lives in
/// `VoiceLoopCore.TtyResolve`; this is only the side-effecting half.
public enum TtyService {
    public static let iterm2BundleID = "com.googlecode.iterm2"

    static func runPs(_ args: [String]) -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = args
        let stdout = Pipe()
        process.standardOutput = stdout
        process.standardError = Pipe()
        do { try process.run() } catch { return "" }
        let data = stdout.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }

    /// `/dev/ttysNNN` for a pid, or nil when it has no controlling terminal.
    public static func pidToTty(_ pid: Int) -> String? {
        TtyResolve.pidToTty(runPs(["-o", "tty=", "-p", String(pid)]))
    }

    /// The Claude pid running on a tty, cross-referenced against the roster.
    public static func claudePid(onTty tty: String, rosterPids: Set<Int>) -> Int? {
        let device = tty.hasPrefix("/dev/") ? String(tty.dropFirst("/dev/".count)) : tty
        return TtyResolve.claudePid(onTty: runPs(["-t", device, "-o", "pid=,comm="]), rosterPids: rosterPids)
    }

    /// Is the app the user is looking at right now iTerm2?
    public static func frontmostIsITerm2() -> Bool {
        #if canImport(AppKit)
        return NSWorkspace.shared.frontmostApplication?.bundleIdentifier == iterm2BundleID
        #else
        return false
        #endif
    }
}
