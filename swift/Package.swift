// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "VoiceLoop",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "VoiceLoopCore", targets: ["VoiceLoopCore"]),
        .library(name: "VoiceLoopEngine", targets: ["VoiceLoopEngine"]),
        .library(name: "VoiceLoopApp", targets: ["VoiceLoopApp"]),
        .executable(name: "VoiceLoop", targets: ["VoiceLoop"]),
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
        // UI: NSStatusItem + SwiftUI HUD. A subscriber of the Engine socket.
        .target(
            name: "VoiceLoopApp",
            dependencies: ["VoiceLoopEngine", "VoiceLoopCore"]
        ),
        // The app bundle's thin entry point. `build-app.sh` compiles this and
        // assembles VoiceLoop.app around it (Info.plist, entitlements, ad-hoc
        // signing). The Xcode project builds the same target.
        .executableTarget(
            name: "VoiceLoop",
            dependencies: ["VoiceLoopApp", "VoiceLoopEngine", "VoiceLoopCore"],
            path: "App",
            exclude: ["Info.plist", "VoiceLoop.entitlements"]
        ),
        .testTarget(
            name: "VoiceLoopCoreTests",
            dependencies: ["VoiceLoopCore"]
        ),
        .testTarget(
            name: "VoiceLoopEngineTests",
            dependencies: ["VoiceLoopEngine", "VoiceLoopCore"]
        ),
        .testTarget(
            name: "VoiceLoopAppTests",
            dependencies: ["VoiceLoopApp"]
        ),
    ]
)
