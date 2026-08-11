import Foundation

/// Turning "¿la llamo…?" into a name you can say and the recognizer can hear.
///
/// Port of `naming.py`. A name has to survive being spoken by `say`, heard, and
/// transcribed back, which rules out punctuation, accents, casing, hyphens and
/// length: what is left is two to four plain lowercase words.
public enum SessionName {
    public static let minWords = 1
    public static let maxWords = 4
    public static let maxChars = 40

    /// "llamala …", "ponele …" — an instruction to name. Worth nothing inside
    /// the name and everything outside it.
    public static let namingVerbs: Set<String> = [
        "llama", "llamala", "llamalo", "llamale", "ponele", "poneme", "ponle",
        "decile", "nombrala", "nombralo",
    ]

    /// Said before the name in an offer ("la llamo…", "ponele…").
    public static let leadingNoise: Set<String> = namingVerbs.union([
        "la", "el", "lo", "las", "los", "una", "un", "que", "se", "sea", "es",
        "mejor", "aa", "eh", "este", "esta",
    ])

    static func words(_ text: String) -> [String] {
        var words = foldWords(text).split(separator: " ").map(String.init).filter { !$0.isEmpty }
        while let first = words.first, leadingNoise.contains(first) {
            words.removeFirst()
        }
        return words
    }

    /// A spoken name: lowercase words, no accents, no punctuation, at most four.
    public static func slugify(_ text: String) -> String {
        let picked = Array(words(text).prefix(maxWords)).joined(separator: " ")
        return String(picked.prefix(maxChars)).trimmingCharacters(in: .whitespaces)
    }

    /// Does the phrase itself say it is a name? "llamala índice" leaves no doubt.
    public static func saysItIsAName(_ text: String) -> Bool {
        let ws = foldWords(text).split(separator: " ").map(String.init).filter { !$0.isEmpty }
        return !ws.isEmpty && namingVerbs.contains(ws[0])
    }

    /// The name inside an answer to the offer, or "" when there is none. The cap
    /// is checked on what is left once the lead-in is dropped, which is what
    /// keeps "mergealo cuando pasen los tests" (five words) from reading as a name.
    public static func dictated(_ text: String) -> String {
        let ws = words(text)
        guard minWords <= ws.count && ws.count <= maxWords else { return "" }
        return String(ws.joined(separator: " ").prefix(maxChars)).trimmingCharacters(in: .whitespaces)
    }

    /// Could this utterance have been meant as a name at all? Length is the only
    /// signal, and the only one that matters.
    public static func isPlausible(_ text: String) -> Bool {
        let ws = foldWords(text).split(separator: " ").map(String.init).filter { !$0.isEmpty }
        return minWords <= ws.count && ws.count <= maxWords
    }
}
