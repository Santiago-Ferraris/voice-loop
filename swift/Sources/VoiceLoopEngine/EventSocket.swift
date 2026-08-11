import Foundation
import VoiceLoopCore
#if canImport(Darwin)
import Darwin
#endif

/// The Engine's Unix-domain socket at `~/.local/state/voice-loop/engine.sock`
/// (0600): a JSONL fan-out of the event stream, plus an inbound command channel.
/// The UI subscribes here; so does `nc -U`. This is the contract the whole
/// design dogfoods.
public final class EventSocket: @unchecked Sendable {
    public static func defaultPath() -> String {
        let home = NSString(string: "~").expandingTildeInPath
        return "\(home)/.local/state/voice-loop/engine.sock"
    }

    public let path: String
    public let logPath: String
    public var onCommandHandler: ((EngineCommand) -> Void)?
    private let lock = NSLock()
    private var clientFDs: [Int32] = []
    private var listenFD: Int32 = -1
    private var running = false
    private var seq = 0
    private var logHandle: FileHandle?

    public init(path: String = EventSocket.defaultPath(), onCommand: ((EngineCommand) -> Void)? = nil) {
        self.path = path
        self.onCommandHandler = onCommand
        let dir = (path as NSString).deletingLastPathComponent
        self.logPath = "\(dir)/logs/events.log"
    }

    public func start() throws {
        let dir = (path as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        unlink(path)

        // A durable JSONL log of every event, so a bug can be read after the
        // fact instead of only over a live socket (the v1 daemon.log lesson).
        let logDir = (logPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        logHandle = FileHandle(forWritingAtPath: logPath)
        try? logHandle?.seekToEnd()

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw SocketError.create(errno) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path) - 1
        _ = withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            let dst = UnsafeMutableRawPointer(ptr).assumingMemoryBound(to: CChar.self)
            path.withCString { cstr in strncpy(dst, cstr, capacity) }
        }
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, size) }
        }
        guard bound == 0 else { close(fd); throw SocketError.bind(errno) }
        chmod(path, 0o600)
        guard listen(fd, 8) == 0 else { close(fd); throw SocketError.listen(errno) }

        listenFD = fd
        running = true
        Thread.detachNewThread { [weak self] in self?.acceptLoop() }
    }

    public func stop() {
        running = false
        lock.lock()
        for fd in clientFDs { close(fd) }
        clientFDs.removeAll()
        if listenFD >= 0 { close(listenFD); listenFD = -1 }
        try? logHandle?.close()
        logHandle = nil
        lock.unlock()
        unlink(path)
    }

    /// Broadcast an event to every subscriber, stamping ts + a monotonic seq.
    public func broadcast(_ event: EngineEvent, ts: Int = EventSocket.nowMillis()) {
        lock.lock()
        seq += 1
        let envelope = EventEnvelope(event: event, ts: ts, seq: seq)
        let line = envelope.jsonLine() + "\n"
        if let data = line.data(using: .utf8) { try? logHandle?.write(contentsOf: data) }
        let dead = writeToAll(line)
        for fd in dead { close(fd); clientFDs.removeAll { $0 == fd } }
        lock.unlock()
    }

    public static func nowMillis() -> Int {
        Int(Date().timeIntervalSince1970 * 1000)
    }

    // MARK: - internals

    private func writeToAll(_ line: String) -> [Int32] {
        var dead: [Int32] = []
        let bytes = Array(line.utf8)
        for fd in clientFDs {
            let written = bytes.withUnsafeBytes { send(fd, $0.baseAddress, bytes.count, 0) }
            if written < 0 { dead.append(fd) }
        }
        return dead
    }

    private func acceptLoop() {
        while running {
            let client = accept(listenFD, nil, nil)
            if client < 0 {
                if !running { break }
                continue
            }
            lock.lock()
            clientFDs.append(client)
            // Greet the new subscriber.
            let hello = EventEnvelope(
                event: .hello(version: engineSchemaVersion, modes: ["raw", "smart"], capabilities: ["hud", "tts"]),
                ts: EventSocket.nowMillis(), seq: 0
            ).jsonLine() + "\n"
            _ = Array(hello.utf8).withUnsafeBytes { send(client, $0.baseAddress, $0.count, 0) }
            lock.unlock()
            Thread.detachNewThread { [weak self] in self?.readLoop(client) }
        }
    }

    private func readLoop(_ fd: Int32) {
        var buffer = [UInt8](repeating: 0, count: 4096)
        var pending = ""
        while running {
            let n = read(fd, &buffer, buffer.count)
            if n <= 0 { break }
            pending += String(decoding: buffer[0..<n], as: UTF8.self)
            while let newline = pending.firstIndex(of: "\n") {
                let line = String(pending[pending.startIndex..<newline])
                pending = String(pending[pending.index(after: newline)...])
                if let command = EngineCommand.decode(line) { onCommandHandler?(command) }
            }
        }
        lock.lock()
        clientFDs.removeAll { $0 == fd }
        lock.unlock()
        close(fd)
    }

    public enum SocketError: Error, Equatable {
        case create(Int32), bind(Int32), listen(Int32)
    }
}
