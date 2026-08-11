import XCTest
@testable import VoiceLoopApp

/// Trap #11: the suite must exercise what actually ships. The installed-bundle
/// check is manual (Hito 8), but the Info.plist that becomes that bundle is
/// checked here at the source so a dropped `LSUIElement` or a missing usage
/// string fails a test rather than a silent TCC hang.
final class BundleInfoTests: XCTestCase {
    private func infoPlist() throws -> [String: Any] {
        // .../swift/Tests/VoiceLoopAppTests/BundleInfoTests.swift -> .../swift/App/Info.plist
        let here = URL(fileURLWithPath: #filePath)
        let swiftDir = here.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let plist = swiftDir.appendingPathComponent("App/Info.plist")
        let data = try Data(contentsOf: plist)
        let obj = try PropertyListSerialization.propertyList(from: data, options: [], format: nil)
        return try XCTUnwrap(obj as? [String: Any])
    }

    func testLSUIElementIsSet() throws {
        let info = try infoPlist()
        XCTAssertEqual(info["LSUIElement"] as? Bool, true, "menu-bar app must be LSUIElement (no Dock icon)")
    }

    func testUsageDescriptionsPresent() throws {
        let info = try infoPlist()
        XCTAssertFalse((info["NSMicrophoneUsageDescription"] as? String ?? "").isEmpty)
        XCTAssertFalse((info["NSAppleEventsUsageDescription"] as? String ?? "").isEmpty)
    }

    func testBundleIdentifier() throws {
        let info = try infoPlist()
        XCTAssertEqual(info["CFBundleIdentifier"] as? String, "com.voiceloop.app")
    }
}
