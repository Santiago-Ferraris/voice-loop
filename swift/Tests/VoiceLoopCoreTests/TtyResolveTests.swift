import XCTest
@testable import VoiceLoopCore

final class TtyResolveTests: XCTestCase {
    func testPidToTty() {
        XCTAssertEqual(TtyResolve.pidToTty("ttys003\n"), "/dev/ttys003")
        XCTAssertEqual(TtyResolve.pidToTty("  ttys012 "), "/dev/ttys012")
        XCTAssertEqual(TtyResolve.pidToTty("/dev/ttys004"), "/dev/ttys004")
        XCTAssertNil(TtyResolve.pidToTty("??"))
        XCTAssertNil(TtyResolve.pidToTty("?"))
        XCTAssertNil(TtyResolve.pidToTty(""))
    }

    func testParseProcessList() {
        let raw = "50177 claude\n50180 node\n  50190   login  \n"
        let processes = TtyResolve.parseProcessList(raw)
        XCTAssertEqual(processes, [
            .init(pid: 50177, comm: "claude"),
            .init(pid: 50180, comm: "node"),
            .init(pid: 50190, comm: "login"),
        ])
    }

    func testClaudePidPrefersRoster() {
        let raw = "50177 node\n50180 claude"
        // Roster knows the node-launched claude pid.
        XCTAssertEqual(TtyResolve.claudePid(onTty: raw, rosterPids: [50177]), 50177)
        // No roster hit: fall back to a comm that names claude.
        XCTAssertEqual(TtyResolve.claudePid(onTty: raw, rosterPids: []), 50180)
        XCTAssertNil(TtyResolve.claudePid(onTty: "50177 node", rosterPids: []))
    }
}
