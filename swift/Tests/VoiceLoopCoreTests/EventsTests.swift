import XCTest
@testable import VoiceLoopCore

final class EventsTests: XCTestCase {
    private func roundTrip(_ event: EngineEvent) -> EngineEvent? {
        let envelope = EventEnvelope(event: event, ts: 123, seq: 7)
        let line = envelope.jsonLine()
        return EventEnvelope.decode(line)?.event
    }

    func testEnvelopeCarriesVTypeTsSeq() {
        let line = EventEnvelope(event: .interimTranscript(text: "hola"), ts: 42, seq: 3).jsonLine()
        XCTAssertTrue(line.contains("\"v\":1"))
        XCTAssertTrue(line.contains("\"type\":\"interim_transcript\""))
        XCTAssertTrue(line.contains("\"ts\":42"))
        XCTAssertTrue(line.contains("\"seq\":3"))
    }

    func testRoundTripEveryEvent() {
        let target = RouterTarget(kind: .named, name: "conflictos", tty: "/dev/ttys003")
        let actions = [RouterAction(op: "inject", target: target, text: "corré los tests")]
        let events: [EngineEvent] = [
            .hello(version: 1, modes: ["raw", "smart"], capabilities: ["hud"]),
            .state(state: .listening, mode: .smart),
            .state(state: .idle, mode: nil),
            .recordingStarted(mode: .raw),
            .interimTranscript(text: "opu cuatro"),
            .finalTranscript(text: "opu cuatro", normalized: "opus 4"),
            .routerResult(target: target, rewritten: "corré los tests", actions: actions,
                          confidence: 0.9, needsConfirmation: false, prompt: nil),
            .namingPrompt(suggested: "conflictos", taskPreview: "arreglá el login"),
            .actionTaken(op: "inject", target: target, text: "corré los tests", ok: true, detail: nil),
            .ttsStarted(text: "listo"),
            .ttsFinished(text: "listo"),
            .recordingCancelled(reason: "esc"),
            .error(code: "no_claude_window", message: "nope", hint: "focus iTerm2"),
        ]
        for event in events {
            XCTAssertEqual(roundTrip(event), event, "round-trip failed for \(event.typeName)")
        }
    }

    func testCommandParsing() {
        XCTAssertEqual(EngineCommand.decode(#"{"cmd":"confirm"}"#), .confirm)
        XCTAssertEqual(EngineCommand.decode(#"{"cmd":"cancel"}"#), .cancel)
        XCTAssertEqual(EngineCommand.decode(#"{"cmd":"pause"}"#), .pause)
        XCTAssertEqual(EngineCommand.decode(#"{"cmd":"get_state"}"#), .getState)
        XCTAssertNil(EngineCommand.decode(#"{"cmd":"nonsense"}"#))
    }

    func testCommandRoundTrip() {
        for command in [EngineCommand.confirm, .cancel, .pause, .resume, .quit, .getState] {
            XCTAssertEqual(EngineCommand.decode(command.jsonLine()), command)
        }
    }
}
