import Foundation

/// Pure parsers of `ps` output. The tty is the only stable handle we have on
/// "the window this Claude session runs in", but the roster does not carry it,
/// so it is resolved through the process table — shelled out by the Engine,
/// parsed here where it can be tested without a machine.
public enum TtyResolve {
    /// Parse `ps -o tty= -p <pid>` into `/dev/ttysNNN`, or nil when the process
    /// has no controlling terminal (`ps` prints `?` or `??`).
    public static func pidToTty(_ raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty || value == "?" || value == "??" { return nil }
        if value.hasPrefix("/dev/") { return value }
        return "/dev/" + value
    }

    public struct Process: Equatable, Sendable {
        public let pid: Int
        public let comm: String
        public init(pid: Int, comm: String) {
            self.pid = pid
            self.comm = comm
        }
    }

    /// Parse `ps -t ttysNNN -o pid=,comm=` — one `<pid> <command>` per line.
    public static func parseProcessList(_ raw: String) -> [Process] {
        var out: [Process] = []
        for line in raw.split(separator: "\n", omittingEmptySubsequences: true) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard let space = trimmed.firstIndex(where: { $0 == " " || $0 == "\t" }) else { continue }
            guard let pid = Int(trimmed[trimmed.startIndex..<space]) else { continue }
            let comm = trimmed[trimmed.index(after: space)...].trimmingCharacters(in: .whitespaces)
            out.append(Process(pid: pid, comm: comm))
        }
        return out
    }

    /// The Claude process on a tty: a pid the roster already knows about wins;
    /// failing that, one whose command names Claude. `nil` when neither is found.
    public static func claudePid(onTty raw: String, rosterPids: Set<Int>) -> Int? {
        let processes = parseProcessList(raw)
        if let known = processes.first(where: { rosterPids.contains($0.pid) }) {
            return known.pid
        }
        return processes.first(where: { $0.comm.lowercased().contains("claude") })?.pid
    }
}
