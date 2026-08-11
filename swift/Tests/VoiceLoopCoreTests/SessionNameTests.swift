import XCTest
@testable import VoiceLoopCore

final class SessionNameTests: XCTestCase {
    func testSlugifyLowercasesAndCapsWords() {
        XCTAssertEqual(SessionName.slugify("Índice de Conflictos"), "indice de conflictos")
        // "la" is leading noise and "llamala" is a naming verb; both drop out.
        XCTAssertEqual(SessionName.slugify("la llamala fecha actual"), "fecha actual")
        // At most four words.
        XCTAssertEqual(SessionName.slugify("uno dos tres cuatro cinco seis"), "uno dos tres cuatro")
    }

    func testDictatedVsSentence() {
        // A short answer is a name; a sentence is not.
        XCTAssertEqual(SessionName.dictated("llamala fecha actual"), "fecha actual")
        XCTAssertEqual(SessionName.dictated("mergealo cuando pasen los tests"), "")
    }

    func testSaysItIsAName() {
        XCTAssertTrue(SessionName.saysItIsAName("llamala índice"))
        XCTAssertTrue(SessionName.saysItIsAName("ponele conflictos"))
        XCTAssertFalse(SessionName.saysItIsAName("dale, mergealo"))
    }

    func testIsPlausible() {
        XCTAssertTrue(SessionName.isPlausible("dos palabras"))
        XCTAssertFalse(SessionName.isPlausible("esto ya son cinco palabras claramente"))
    }
}
