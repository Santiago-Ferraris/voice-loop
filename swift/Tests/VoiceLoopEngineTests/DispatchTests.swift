import XCTest
@testable import VoiceLoopEngine
import VoiceLoopCore

final class KeystrokeEncodingTests: XCTestCase {
    func testEncodeArrowDown() throws {
        // ESC [ B -> "27,91,66"
        XCTAssertEqual(try ITermDispatch.encodeKeystroke(ITermDispatch.arrowDown), "27,91,66")
        XCTAssertEqual(try ITermDispatch.encodeKeystroke(ITermDispatch.enter), "13")
        XCTAssertEqual(try ITermDispatch.encodeKeystroke(ITermDispatch.escape), "27")
    }

    func testEmptyKeystrokeThrows() {
        XCTAssertThrowsError(try ITermDispatch.encodeKeystroke(""))
    }
}

final class MockRunnerTests: XCTestCase {
    func testWriteTextUsesFrontIsClaudeConfirmation() throws {
        // The runner is injected; verify write_text sends body then the CR
        // keystroke as a separate call (trap #10).
        var calls: [[String]] = []
        let runner: ITermDispatch.Runner = { _, args in
            calls.append(args)
            return "sent"
        }
        try ITermDispatch.writeText(tty: "/dev/ttys003", text: "hola mundo", newline: true, runner: runner)
        XCTAssertEqual(calls.count, 2)
        XCTAssertEqual(calls[0], ["/dev/ttys003", "hola mundo"])   // write_text, newline no
        XCTAssertEqual(calls[1], ["/dev/ttys003", "13"])           // separate CR
    }

    func testSessionGoneWhenMissing() {
        let runner: ITermDispatch.Runner = { _, _ in "missing" }
        XCTAssertThrowsError(try ITermDispatch.writeText(tty: "/dev/ttys003", text: "x", newline: false, runner: runner))
    }
}

final class MicResolveTests: XCTestCase {
    func testPicksConfiguredNameNotIndex() {
        // Continuity has put the iPhone first; the configured Mac mic must win.
        let names = ["Santiago's iPhone Microphone", "MacBook Pro Microphone"]
        XCTAssertEqual(MicCapture.resolveDeviceName(":MacBook Pro Microphone", among: names), "MacBook Pro Microphone")
    }

    func testContainmentMatch() {
        let names = ["External USB Audio", "MacBook Pro Microphone"]
        XCTAssertEqual(MicCapture.resolveDeviceName(":MacBook Pro", among: names), "MacBook Pro Microphone")
    }

    func testUnmatchedReturnsNil() {
        XCTAssertNil(MicCapture.resolveDeviceName(":Nonexistent Device", among: ["MacBook Pro Microphone"]))
    }
}

final class DeepgramURLTests: XCTestCase {
    func testStreamingParamsMatchRestContract() {
        let url = DeepgramStream.buildURL(keyterms: ["mergealo", "draft mode"]).absoluteString
        XCTAssertTrue(url.contains("model=nova-3"))
        XCTAssertTrue(url.contains("language=multi"))
        XCTAssertTrue(url.contains("smart_format=false"))
        XCTAssertTrue(url.contains("numerals=false"))
        XCTAssertTrue(url.contains("keyterm=mergealo"))
        XCTAssertTrue(url.contains("keyterm=draft%20mode"))
    }

    func testNormalizeKeytermsDedupesAndCaps() {
        let terms = DeepgramStream.normalizeKeyterms(["  pr ", "PR", "test", ""])
        XCTAssertEqual(terms, ["pr", "test"])
    }
}
