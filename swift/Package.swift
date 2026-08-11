// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "VoiceLoop",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "VoiceLoopCore", targets: ["VoiceLoopCore"]),
    ],
    targets: [
        // Pure logic, no AppKit. Headless-testable; reusable by another OS.
        .target(
            name: "VoiceLoopCore"
        ),
        .testTarget(
            name: "VoiceLoopCoreTests",
            dependencies: ["VoiceLoopCore"]
        ),
    ]
)
