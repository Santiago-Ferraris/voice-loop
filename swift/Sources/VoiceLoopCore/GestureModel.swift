import Foundation

/// The two push-to-talk keys, and what a press of one resolves to.
///
/// ⌥N is *raw* — dictate verbatim into the focused Claude window, no rewrite, no
/// confirmation. ⌥M is *smart* — routed through the LLM. A press is a hold when
/// it lasts past the threshold *or* carries voice; a quick press with no voice
/// is a short tap, which is how ⌥M confirms. ⌥N never confirms anything, so the
/// orchestrator ignores a raw short tap. ⌥B is gone.
public enum GestureMode: String, Equatable, Sendable {
    case raw   // ⌥N
    case smart // ⌥M
}

public enum Gesture: Equatable, Sendable {
    case holdStart(GestureMode)
    case holdEnd(GestureMode)
    case shortTap(GestureMode)
    case cancel
}

public enum GestureDiscriminator {
    /// A press under this and with no voice reads as a tap, not a recording.
    /// Sits in the 250–300ms band the plan calls for; needs real-world tuning.
    public static let defaultHoldThreshold: TimeInterval = 0.28

    /// Resolve a release. `heldSeconds` is press-to-release; `hadVoice` is
    /// whether the mic measured speech energy during the hold; `escaped` is a
    /// press of Esc before release.
    public static func classifyRelease(
        mode: GestureMode,
        heldSeconds: TimeInterval,
        hadVoice: Bool,
        escaped: Bool,
        holdThreshold: TimeInterval = defaultHoldThreshold
    ) -> Gesture {
        if escaped { return .cancel }
        if heldSeconds < holdThreshold && !hadVoice { return .shortTap(mode) }
        return .holdEnd(mode)
    }
}
