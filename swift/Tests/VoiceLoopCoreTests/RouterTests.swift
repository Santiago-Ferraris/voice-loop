import XCTest
@testable import VoiceLoopCore

final class RouterSchemaTests: XCTestCase {
    private let valid: [String: Any] = [
        "target": ["kind": "named", "name": "conflictos"],
        "rewritten": "corré los tests",
        "actions": [["op": "inject", "text": "corré los tests"]],
        "confidence": 0.92,
        "needs_confirmation": false,
    ]

    func testValidatesGoodOutput() {
        let decision = RouterSchema.validate(valid)
        XCTAssertNotNil(decision)
        XCTAssertEqual(decision?.target.kind, .named)
        XCTAssertEqual(decision?.target.name, "conflictos")
        XCTAssertEqual(decision?.actions.first?.op, "inject")
        XCTAssertEqual(decision?.confidence, 0.92)
    }

    func testNamedWithoutNameIsInvalid() {
        var bad = valid
        bad["target"] = ["kind": "named"]
        XCTAssertNil(RouterSchema.validate(bad))
    }

    func testMissingFieldsInvalid() {
        var bad = valid
        bad["rewritten"] = nil as Any?
        XCTAssertNil(RouterSchema.validate(bad))
        bad = valid
        bad["confidence"] = nil as Any?
        XCTAssertNil(RouterSchema.validate(bad))
    }

    func testConfidenceClamped() {
        var over = valid
        over["confidence"] = 1.7
        XCTAssertEqual(RouterSchema.validate(over)?.confidence, 1.0)
    }

    func testRetryOnceThenSucceeds() async throws {
        var calls = 0
        let decision = try await routeWithRetry {
            calls += 1
            return calls == 1 ? ["garbage": true] : self.valid
        }
        XCTAssertEqual(calls, 2)
        XCTAssertEqual(decision.target.name, "conflictos")
    }

    func testRetryExhaustedThrows() async {
        var calls = 0
        do {
            _ = try await routeWithRetry { calls += 1; return ["garbage": true] }
            XCTFail("expected schemaInvalid")
        } catch {
            XCTAssertEqual(error as? RouterError, .schemaInvalid)
            XCTAssertEqual(calls, 2)
        }
    }
}
