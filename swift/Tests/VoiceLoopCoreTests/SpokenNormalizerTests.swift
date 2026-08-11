import XCTest
@testable import VoiceLoopCore

final class SpokenNormalizerTests: XCTestCase {
    func testVersionAfterModelName() {
        XCTAssertEqual(SpokenNormalizer.normalize("opus cuatro punto ocho"), "opus 4.8")
        XCTAssertEqual(SpokenNormalizer.normalize("sonnet tres punto cinco"), "sonnet 3.5")
    }

    func testFuzzyModelNameRepaired() {
        // "opu" is one edit from "opus"; with a version behind it, it is that model.
        XCTAssertEqual(SpokenNormalizer.normalize("opu cuatro punto ocho"), "opus 4.8")
        XCTAssertEqual(SpokenNormalizer.modelName("opu"), "opus")
        XCTAssertEqual(SpokenNormalizer.modelName("sonet"), "sonnet")
    }

    func testNonModelNamesAreNotRepaired() {
        XCTAssertNil(SpokenNormalizer.modelName("opas"))     // 0.75, below threshold
        XCTAssertNil(SpokenNormalizer.modelName("claudia"))  // 0.77
    }

    func testMenuNumberLeftIntact() {
        // "la dos" picks option 2 out of a menu; it must not become "la 2".
        XCTAssertEqual(SpokenNormalizer.normalize("la dos"), "la dos")
        XCTAssertEqual(SpokenNormalizer.normalize("esperá cinco minutos"), "esperá cinco minutos")
    }

    func testVersionWordLicensesNumber() {
        XCTAssertEqual(SpokenNormalizer.normalize("la versión cuatro punto dos"), "la versión 4.2")
    }

    func testCasingPreservedOnExactMatch() {
        XCTAssertEqual(SpokenNormalizer.normalize("Opus cuatro"), "Opus 4")
    }

    func testEmptyIsUntouched() {
        XCTAssertEqual(SpokenNormalizer.normalize(""), "")
        XCTAssertEqual(SpokenNormalizer.normalize("   "), "   ")
    }
}

final class SequenceRatioTests: XCTestCase {
    func testKnownRatios() {
        XCTAssertEqual(sequenceRatio("opu", "opus"), 6.0 / 7.0, accuracy: 1e-9)
        XCTAssertEqual(sequenceRatio("sonet", "sonnet"), 10.0 / 11.0, accuracy: 1e-9)
        XCTAssertEqual(sequenceRatio("opas", "opus"), 0.75, accuracy: 1e-9)
        XCTAssertEqual(sequenceRatio("claudia", "claude"), 10.0 / 13.0, accuracy: 1e-9)
    }

    func testIdenticalAndEmpty() {
        XCTAssertEqual(sequenceRatio("haiku", "haiku"), 1.0, accuracy: 1e-9)
        XCTAssertEqual(sequenceRatio("", ""), 1.0, accuracy: 1e-9)
    }
}
