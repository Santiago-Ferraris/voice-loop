// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "VoiceLoop",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "VoiceLoopCore", targets: ["VoiceLoopCore"]),
        .library(name: "VoiceLoopEngine", targets: ["VoiceLoopEngine"]),
    ],
    targets: [
        // Pure logic, no AppKit. Headless-testable; reusable by another OS.
        .target(
            name: "VoiceLoopCore"
        ),
        // AppKit / CoreGraphics / AVFoundation, headless-capable.
        .target(
            name: "VoiceLoopEngine",
            dependencies: ["VoiceLoopCore"],
            resources: [.copy("AppleScript")]
        ),
        .testTarget(
            name: "VoiceLoopCoreTests",
            dependencies: ["VoiceLoopCore"]
        ),
        .testTarget(
            name: "VoiceLoopEngineTests",
            dependencies: ["VoiceLoopEngine", "VoiceLoopCore"]
        ),
    ]
)
