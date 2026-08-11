import XCTest
@testable import VoiceLoopCore

final class EnvFileTests: XCTestCase {
    func testParsesAssignments() {
        let text = """
        # a comment
        OPENAI_API_KEY=sk-abc
        export DEEPGRAM_API_KEY="dg-123"
          SPACED = value
        not an assignment
        """
        let values = EnvFile.parse(text)
        XCTAssertEqual(values["OPENAI_API_KEY"], "sk-abc")
        XCTAssertEqual(values["DEEPGRAM_API_KEY"], "dg-123")
        XCTAssertEqual(values["SPACED"], "value")
    }

    func testUnquoteStripsQuotesAndTrailingComment() {
        XCTAssertEqual(EnvFile.unquote("\"quoted\""), "quoted")
        XCTAssertEqual(EnvFile.unquote("'single'"), "single")
        XCTAssertEqual(EnvFile.unquote("bare # trailing"), "bare")
    }
}
