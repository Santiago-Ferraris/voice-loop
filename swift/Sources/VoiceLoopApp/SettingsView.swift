import SwiftUI
import AppKit
import VoiceLoopEngine
import VoiceLoopCore

/// Settings + first-run onboarding: pick the input device *by name*, drop the
/// keys into the Keychain, set the thresholds, and run the Doctor to fire the
/// TCC prompts while the user is sitting there (trap #8).
struct SettingsView: View {
    @State private var config = Config.load()
    @State private var deviceNames: [String] = Doctor.inputDeviceNames()
    @State private var openAIKey = ""
    @State private var deepgramKey = ""
    @State private var report: Doctor.Report?

    var body: some View {
        Form {
            Section("Microphone") {
                Picker("Input device", selection: $config.microphoneDevice) {
                    ForEach(deviceNames, id: \.self) { name in
                        Text(name).tag(":\(name)")
                    }
                }
                Button("Refresh devices") { deviceNames = Doctor.inputDeviceNames() }
            }
            Section("Keys (stored in Keychain)") {
                SecureField("OPENAI_API_KEY", text: $openAIKey)
                SecureField("DEEPGRAM_API_KEY", text: $deepgramKey)
                Button("Save keys") {
                    if !openAIKey.isEmpty { Keychain.write(Secrets.openAIKeyName, openAIKey) }
                    if !deepgramKey.isEmpty { Keychain.write(Secrets.deepgramKeyName, deepgramKey) }
                }
            }
            Section("Thresholds") {
                HStack {
                    Text("Silence min (s)")
                    Slider(value: $config.silenceMinSeconds, in: 1...8)
                    Text(String(format: "%.1f", config.silenceMinSeconds))
                }
                HStack {
                    Text("Noise floor (dB)")
                    Slider(value: $config.silenceNoiseDb, in: -60 ... -20)
                    Text(String(format: "%.0f", config.silenceNoiseDb))
                }
            }
            Section("Router") {
                Picker("LLM", selection: $config.router) {
                    Text("OpenAI gpt-4o-mini (default)").tag(Config.RouterKind.openai)
                    Text("Anthropic Haiku 4.5").tag(Config.RouterKind.anthropic)
                }
            }
            Section("Onboarding") {
                Button("Run Doctor (grant permissions)") {
                    Doctor.requestMicrophone { _ in }
                    _ = Doctor.accessibilityStatus(prompt: true)
                    report = Doctor.run(configuredDevice: config.microphoneDevice)
                }
                if let report {
                    DoctorReportView(report: report)
                }
            }
            HStack {
                Spacer()
                Button("Save") { try? config.save() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 460)
    }
}

struct DoctorReportView: View {
    let report: Doctor.Report

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            row("Microphone", report.microphone.rawValue)
            row("Accessibility", report.accessibility.rawValue)
            row("Automation", report.automation.rawValue)
            row("Input gain", report.inputGain.map(String.init) ?? "unknown")
            row("Resolved device", report.configuredDeviceResolved ?? "not found")
        }
        .font(.caption)
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack { Text(label); Spacer(); Text(value).foregroundStyle(.secondary) }
    }
}

/// A single reusable settings window.
@MainActor
final class SettingsWindow {
    static let shared = SettingsWindow()
    private var window: NSWindow?

    func show() {
        if window == nil {
            let hosting = NSHostingController(rootView: SettingsView())
            let win = NSWindow(contentViewController: hosting)
            win.title = "VoiceLoop Settings"
            win.styleMask = [.titled, .closable]
            window = win
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
