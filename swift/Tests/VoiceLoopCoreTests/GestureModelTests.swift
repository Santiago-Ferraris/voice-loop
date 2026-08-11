import XCTest
@testable import VoiceLoopCore

final class GestureModelTests: XCTestCase {
    func testShortPressWithoutVoiceIsTap() {
        let g = GestureDiscriminator.classifyRelease(mode: .smart, heldSeconds: 0.12, hadVoice: false, escaped: false)
        XCTAssertEqual(g, .shortTap(.smart))
    }

    func testLongPressIsHold() {
        let g = GestureDiscriminator.classifyRelease(mode: .raw, heldSeconds: 1.5, hadVoice: false, escaped: false)
        XCTAssertEqual(g, .holdEnd(.raw))
    }

    func testShortPressWithVoiceIsHold() {
        // Energy present: even a quick press is a recording, never a confirm.
        let g = GestureDiscriminator.classifyRelease(mode: .smart, heldSeconds: 0.10, hadVoice: true, escaped: false)
        XCTAssertEqual(g, .holdEnd(.smart))
    }

    func testEscapeDuringHoldCancels() {
        let g = GestureDiscriminator.classifyRelease(mode: .smart, heldSeconds: 2.0, hadVoice: true, escaped: true)
        XCTAssertEqual(g, .cancel)
    }
}
