import XCTest
@testable import VoiceLoopCore

final class VocabularyTests: XCTestCase {
    func testJargonSurvivesStopWords() {
        let messages = [
            "mergealo cuando pasen los tests del pr",
            "abrí un pr y corré los tests",
            "el pr rompe los tests otra vez",
        ]
        let terms = Vocabulary.extract(fromMessages: messages, minCount: 2, limit: 80)
        XCTAssertTrue(terms.contains("pr"))
        XCTAssertTrue(terms.contains("tests"))
        // Ordinary Spanish and everyday verbs are dropped.
        XCTAssertFalse(terms.contains("cuando"))
        XCTAssertFalse(terms.contains("abri"))
        XCTAssertFalse(terms.contains("corre"))
    }

    func testMinCountAndOrdering() {
        let messages = ["stage stage stage draft draft lambda"]
        let terms = Vocabulary.extract(fromMessages: messages, minCount: 2, limit: 80)
        // count desc, then term asc; lambda (1) is below min_count.
        XCTAssertEqual(terms, ["stage", "draft"])
    }

    func testDigitBearingAndShortTokensDropped() {
        let messages = ["issue123 issue123 ok ab", "issue123 issue123 ok"]
        let terms = Vocabulary.extract(fromMessages: messages, minCount: 2)
        XCTAssertFalse(terms.contains("issue123"))
    }

    func testInjectedEnvelopesFiltered() {
        XCTAssertTrue(Vocabulary.isInjected("<command-name>foo</command-name>"))
        XCTAssertTrue(Vocabulary.isInjected("  <system-reminder>x"))
        XCTAssertTrue(Vocabulary.isInjected("Caveat: the messages below"))
        XCTAssertFalse(Vocabulary.isInjected("mergealo cuando pasen los tests"))
    }

    func testUserMessagesParsingFromJsonl() throws {
        let dir = NSTemporaryDirectory() + "vl-vocab-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let path = dir + "/session.jsonl"
        let lines = [
            #"{"type":"user","message":{"content":"mergealo el pr"}}"#,
            #"{"type":"user","isSidechain":true,"message":{"content":"agente interno"}}"#,
            #"{"type":"assistant","message":{"content":"claro"}}"#,
            #"{"type":"user","message":{"content":[{"type":"text","text":"corré los tests"}]}}"#,
            #"{"type":"user","message":{"content":"<command-name>clear</command-name>"}}"#,
        ]
        try lines.joined(separator: "\n").write(toFile: path, atomically: true, encoding: .utf8)
        let messages = Vocabulary.userMessages(atPath: path)
        XCTAssertEqual(messages, ["mergealo el pr", "corré los tests"])
    }
}
