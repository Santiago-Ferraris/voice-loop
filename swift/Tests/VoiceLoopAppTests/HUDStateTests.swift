import XCTest
@testable import VoiceLoopApp
import VoiceLoopCore

/// The HUD is a pure function of the event stream (HUDPanel just mirrors this
/// state onto an NSPanel). These lock the visibility transitions that the
/// wiring bug hid: a `recording_started` shows it, and any terminal event
/// (action taken / cancelled / error) hides it again.
final class HUDStateTests: XCTestCase {
    func testRecordingStartedShowsAndSetsMode() {
        let state = HUDState()
        XCTAssertFalse(state.visible)
        state.apply(.recordingStarted(mode: .smart))
        XCTAssertTrue(state.visible)
        XCTAssertEqual(state.mode, .smart)
    }

    func testInterimAndFinalTranscriptFillTheLine() {
        let state = HUDState()
        state.apply(.recordingStarted(mode: .raw))
        state.apply(.interimTranscript(text: "corré los"))
        XCTAssertEqual(state.interim, "corré los")
        state.apply(.finalTranscript(text: "corre los tests", normalized: "corré los tests"))
        XCTAssertEqual(state.interim, "corré los tests")
    }

    func testActionTakenHidesTheHUD() {
        let state = HUDState()
        state.apply(.recordingStarted(mode: .raw))
        state.apply(.actionTaken(
            op: "inject",
            target: RouterTarget(kind: .focused, tty: "/dev/ttys003"),
            text: "corré los tests", ok: true, detail: nil
        ))
        XCTAssertFalse(state.visible)
    }

    func testCancelAndErrorHideTheHUD() {
        let cancelled = HUDState()
        cancelled.apply(.recordingStarted(mode: .smart))
        cancelled.apply(.recordingCancelled(reason: "esc"))
        XCTAssertFalse(cancelled.visible)

        let errored = HUDState()
        errored.apply(.recordingStarted(mode: .smart))
        errored.apply(.error(code: "no_claude_window", message: "no window", hint: nil))
        XCTAssertFalse(errored.visible)
    }

    func testRouterResultShowsDeliveryLineAndConfirmation() {
        let state = HUDState()
        state.apply(.recordingStarted(mode: .smart))
        state.apply(.routerResult(
            target: RouterTarget(kind: .named, name: "main-ui"),
            rewritten: "corré los tests",
            actions: [], confidence: 0.4, needsConfirmation: true, prompt: nil
        ))
        XCTAssertTrue(state.deliveryLine.contains("main-ui"))
        XCTAssertFalse(state.confirmation.isEmpty)
    }
}
