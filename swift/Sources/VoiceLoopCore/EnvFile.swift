import Foundation

/// The file the keys live in. Port of `envfile.py`, kept because the Keychain
/// read falls back to `~/.config/voice-loop/env` so an install with the existing
/// keys works with nothing typed in by hand.
///
/// A parser, not a shell: `KEY=value`, an optional `export`, `#` comments, and
/// one level of quoting. Anything cleverer is ignored here.
public enum EnvFile {
    public static let defaultPath = "~/.config/voice-loop/env"

    /// Strip one matching pair of quotes, and an unquoted trailing comment.
    public static func unquote(_ raw: String) -> String {
        let value = raw.trimmingCharacters(in: .whitespaces)
        if value.count >= 2, let first = value.first, let last = value.last,
           first == last, first == "\"" || first == "'" {
            return String(value.dropFirst().dropLast())
        }
        if let range = value.range(of: " #") {
            return String(value[value.startIndex..<range.lowerBound]).trimmingCharacters(in: .whitespaces)
        }
        return value
    }

    public static func parse(_ text: String) -> [String: String] {
        var values: [String: String] = [:]
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(rawLine)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            guard let (key, value) = assignment(line) else { continue }
            values[key] = unquote(value)
        }
        return values
    }

    /// Match `^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$`.
    static func assignment(_ line: String) -> (String, String)? {
        var rest = Substring(line).drop(while: { $0 == " " || $0 == "\t" })
        if rest.hasPrefix("export ") || rest.hasPrefix("export\t") {
            rest = rest.dropFirst("export".count).drop(while: { $0 == " " || $0 == "\t" })
        }
        guard let first = rest.first, first.isLetter || first == "_" else { return nil }
        var name = ""
        var index = rest.startIndex
        while index < rest.endIndex {
            let ch = rest[index]
            if ch.isLetter || ch.isNumber || ch == "_" {
                name.append(ch)
                index = rest.index(after: index)
            } else { break }
        }
        var after = rest[index...].drop(while: { $0 == " " || $0 == "\t" })
        guard after.first == "=" else { return nil }
        after = after.dropFirst()
        let value = String(after.drop(while: { $0 == " " || $0 == "\t" }))
        return (name, value)
    }

    public static func read(path: String = defaultPath) -> [String: String] {
        let expanded = NSString(string: path).expandingTildeInPath
        guard let text = try? String(contentsOfFile: expanded, encoding: .utf8) else { return [:] }
        return parse(text)
    }
}
