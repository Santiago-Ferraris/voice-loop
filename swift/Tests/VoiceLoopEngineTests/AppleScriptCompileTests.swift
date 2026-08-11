import XCTest
@testable import VoiceLoopEngine

/// Trap #9 / #11: the old `open_tab` script used the reserved word `startup` and
/// never compiled once, and no test saw it because they all mocked the runner.
/// These tests compile every shipped `.applescript` for real with `osacompile`,
/// so a reserved-word or syntax regression fails here instead of at runtime.
final class AppleScriptCompileTests: XCTestCase {
    private let scripts = [
        "write_text", "send_keys", "find_session", "session_state",
        "open_tab", "focus", "current_session_tty", "ping",
    ]

    func testEveryScriptIsPresentInBundle() throws {
        for name in scripts {
            let url = Bundle.module.url(forResource: name, withExtension: "applescript", subdirectory: "AppleScript")
            XCTAssertNotNil(url, "missing AppleScript resource: \(name)")
        }
    }

    func testEveryScriptCompilesWithOsacompile() throws {
        for name in scripts {
            guard let url = Bundle.module.url(forResource: name, withExtension: "applescript", subdirectory: "AppleScript") else {
                XCTFail("missing AppleScript resource: \(name)")
                continue
            }
            let out = NSTemporaryDirectory() + "vl-\(name)-\(UUID().uuidString).scpt"
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osacompile")
            process.arguments = ["-o", out, url.path]
            let stderr = Pipe()
            process.standardError = stderr
            try process.run()
            process.waitUntilExit()
            let errText = String(data: stderr.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            XCTAssertEqual(process.terminationStatus, 0, "\(name).applescript failed to compile: \(errText)")
            try? FileManager.default.removeItem(atPath: out)
        }
    }
}
