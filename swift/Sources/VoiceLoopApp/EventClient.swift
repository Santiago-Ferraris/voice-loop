import Foundation
import VoiceLoopCore
import VoiceLoopEngine
#if canImport(Darwin)
import Darwin
#endif

/// Subscribes to the Engine's Unix socket, decodes the JSONL stream, and hands
/// each event to the UI. The app is nothing but a subscriber of this contract —
/// dogfooding the same socket `nc -U` would read.
public final class EventClient: @unchecked Sendable {
    public var onEvent: ((EngineEvent) -> Void)?

    private let path: String
    private var fd: Int32 = -1
    private var running = false

    public init(path: String = EventSocket.defaultPath()) {
        self.path = path
    }

    public func start() {
        running = true
        Thread.detachNewThread { [weak self] in self?.connectLoop() }
    }

    public func stop() {
        running = false
        if fd >= 0 { close(fd); fd = -1 }
    }

    /// Send a command back to the Engine.
    public func send(_ command: EngineCommand) {
        guard fd >= 0 else { return }
        let line = command.jsonLine() + "\n"
        _ = Array(line.utf8).withUnsafeBytes { Darwin.send(fd, $0.baseAddress, $0.count, 0) }
    }

    private func connectLoop() {
        while running {
            let sock = socket(AF_UNIX, SOCK_STREAM, 0)
            guard sock >= 0 else { Thread.sleep(forTimeInterval: 1); continue }
            var addr = sockaddr_un()
            addr.sun_family = sa_family_t(AF_UNIX)
            let capacity = MemoryLayout.size(ofValue: addr.sun_path) - 1
            _ = withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
                let dst = UnsafeMutableRawPointer(ptr).assumingMemoryBound(to: CChar.self)
                path.withCString { cstr in strncpy(dst, cstr, capacity) }
            }
            let size = socklen_t(MemoryLayout<sockaddr_un>.size)
            let ok = withUnsafePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(sock, $0, size) }
            }
            if ok != 0 {
                close(sock)
                Thread.sleep(forTimeInterval: 1)
                continue
            }
            fd = sock
            readLoop(sock)
            close(sock)
            fd = -1
            if running { Thread.sleep(forTimeInterval: 1) }
        }
    }

    private func readLoop(_ sock: Int32) {
        var buffer = [UInt8](repeating: 0, count: 4096)
        var pending = ""
        while running {
            let n = read(sock, &buffer, buffer.count)
            if n <= 0 { break }
            pending += String(decoding: buffer[0..<n], as: UTF8.self)
            while let newline = pending.firstIndex(of: "\n") {
                let line = String(pending[pending.startIndex..<newline])
                pending = String(pending[pending.index(after: newline)...])
                if let envelope = EventEnvelope.decode(line) { onEvent?(envelope.event) }
            }
        }
    }
}
