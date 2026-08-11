import Foundation

/// Turning a router `target` into a concrete window to act on — or a decision to
/// ask first. Pure so the gating ("claro → directo, ambiguo → confirmar") is
/// testable against a roster fixture without a machine.
public enum TargetResolver {
    /// One window the resolver can pick, with its tty already resolved.
    public struct Window: Equatable, Sendable {
        public var name: String
        public var tty: String?
        public init(name: String, tty: String?) {
            self.name = name
            self.tty = tty
        }
    }

    public enum Resolved: Equatable, Sendable {
        case tty(String)                // act directly
        case newTab                     // open a new tab
        case needsConfirmation(String)  // ambiguous — read back and wait
        case notClaude                  // focused window is not Claude
    }

    /// Below this the router's own confidence forces a confirmation even when the
    /// target itself is unambiguous.
    public static let lowConfidence = 0.5

    public static func resolve(
        target: RouterTarget,
        windows: [Window],
        focusedTty: String?,
        focusedIsClaude: Bool,
        confidence: Double,
        needsConfirmation: Bool
    ) -> Resolved {
        // The router asking for confirmation, or low confidence, is decisive
        // once we know the target is at all resolvable.
        let uncertain = needsConfirmation || confidence < lowConfidence

        switch target.kind {
        case .new:
            return .newTab

        case .named:
            let wanted = foldWords(target.name ?? "")
            let matches = windows.filter { foldWords($0.name) == wanted }
            if matches.count == 1, let tty = matches[0].tty {
                return uncertain ? .needsConfirmation(matches[0].name) : .tty(tty)
            }
            // Nothing matched, several matched, or the one match had no tty.
            return .needsConfirmation(target.name ?? "")

        case .focused:
            guard focusedIsClaude, let tty = focusedTty else {
                return .notClaude
            }
            return uncertain ? .needsConfirmation("") : .tty(tty)
        }
    }
}
