import XCTest
@testable import VoiceLoopCore

final class TargetResolverTests: XCTestCase {
    private let windows = [
        TargetResolver.Window(name: "conflictos", tty: "/dev/ttys003"),
        TargetResolver.Window(name: "draft mode", tty: "/dev/ttys004"),
    ]

    func testNamedResolvesUniquely() {
        let target = RouterTarget(kind: .named, name: "conflictos")
        let resolved = TargetResolver.resolve(target: target, windows: windows,
                                              focusedTty: nil, focusedIsClaude: false,
                                              confidence: 0.9, needsConfirmation: false)
        XCTAssertEqual(resolved, .tty("/dev/ttys003"))
    }

    func testNamedUnknownNeedsConfirmation() {
        let target = RouterTarget(kind: .named, name: "no existe")
        let resolved = TargetResolver.resolve(target: target, windows: windows,
                                              focusedTty: nil, focusedIsClaude: false,
                                              confidence: 0.9, needsConfirmation: false)
        XCTAssertEqual(resolved, .needsConfirmation("no existe"))
    }

    func testNewTab() {
        let target = RouterTarget(kind: .new)
        let resolved = TargetResolver.resolve(target: target, windows: windows,
                                              focusedTty: nil, focusedIsClaude: false,
                                              confidence: 0.9, needsConfirmation: false)
        XCTAssertEqual(resolved, .newTab)
    }

    func testFocusedWhenClaude() {
        let target = RouterTarget(kind: .focused)
        let resolved = TargetResolver.resolve(target: target, windows: windows,
                                              focusedTty: "/dev/ttys009", focusedIsClaude: true,
                                              confidence: 0.9, needsConfirmation: false)
        XCTAssertEqual(resolved, .tty("/dev/ttys009"))
    }

    func testFocusedNotClaude() {
        let target = RouterTarget(kind: .focused)
        let resolved = TargetResolver.resolve(target: target, windows: windows,
                                              focusedTty: nil, focusedIsClaude: false,
                                              confidence: 0.9, needsConfirmation: false)
        XCTAssertEqual(resolved, .notClaude)
    }

    func testLowConfidenceForcesConfirmation() {
        let target = RouterTarget(kind: .named, name: "conflictos")
        let resolved = TargetResolver.resolve(target: target, windows: windows,
                                              focusedTty: nil, focusedIsClaude: false,
                                              confidence: 0.3, needsConfirmation: false)
        XCTAssertEqual(resolved, .needsConfirmation("conflictos"))
    }

    func testRouterConfirmationFlagRespected() {
        let target = RouterTarget(kind: .focused)
        let resolved = TargetResolver.resolve(target: target, windows: windows,
                                              focusedTty: "/dev/ttys009", focusedIsClaude: true,
                                              confidence: 0.99, needsConfirmation: true)
        XCTAssertEqual(resolved, .needsConfirmation(""))
    }
}
