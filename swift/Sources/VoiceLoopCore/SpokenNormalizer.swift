import Foundation

/// Model names and version numbers, back into the form they are written in.
///
/// Dictating "opus 4.8" arrives as `opu cuatro punto ocho`, and that string is
/// what Claude would receive. Port of `spoken.py`: the repair is deliberately
/// narrow — a number becomes digits *only* when it follows a model name or the
/// word "versión". "la dos" (menu answer) and "esperá cinco minutos" are left
/// exactly as said.
public enum SpokenNormalizer {
    /// The names worth repairing and pinning into the recognizer's vocabulary.
    public static let modelNames: [String] = ["opus", "sonnet", "haiku", "claude", "gemini", "gpt"]

    static let versionWords: Set<String> = ["version"]
    static let dotWords: Set<String> = ["punto"]

    /// Cardinals only, up to twenty — far past every model that exists. Not the
    /// menu-number map: "primero"/"cuarta" are not how anybody says a version.
    static let digits: [String: String] = [
        "cero": "0", "uno": "1", "un": "1", "una": "1", "dos": "2", "tres": "3",
        "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7", "ocho": "8",
        "nueve": "9", "diez": "10", "once": "11", "doce": "12", "trece": "13",
        "catorce": "14", "quince": "15", "dieciseis": "16", "diecisiete": "17",
        "dieciocho": "18", "diecinueve": "19", "veinte": "20",
    ]

    static let nearEnough = 0.8
    static let minHeardChars = 3
    static let minNameChars = 4

    /// The model this token is, spelled properly — or `nil` if it is not one.
    public static func modelName(_ token: String) -> String? {
        let folded = foldWords(token)
        if folded.isEmpty { return nil }
        if modelNames.contains(folded) { return folded }
        if folded.count < minHeardChars { return nil }
        var best: String?
        var score = nearEnough
        for name in modelNames where name.count >= minNameChars {
            let ratio = sequenceRatio(folded, name)
            if ratio >= score {
                best = name
                score = ratio
            }
        }
        return best
    }

    /// `opu cuatro punto ocho` -> `opus 4.8`. Everything else is left alone.
    public static func normalize(_ text: String) -> String {
        if text.trimmingCharacters(in: .whitespaces).isEmpty { return text }
        let pieces = splitWords(text)
        var out: [String] = []
        var index = 0
        while index < pieces.count {
            let piece = pieces[index]
            if !isWord(piece) {
                out.append(piece)
                index += 1
                continue
            }
            let name = modelName(piece)
            let folded = foldWords(piece)
            if name == nil && !versionWords.contains(folded) {
                out.append(piece)
                index += 1
                continue
            }
            let (version, consumed) = versionAfter(pieces, index + 1)
            if version.isEmpty {
                out.append(piece)
                index += 1
                continue
            }
            out.append(name != nil ? spelled(piece, name!) : piece)
            out.append(" ")
            out.append(version)
            index += 1 + consumed
        }
        return out.joined()
    }

    // MARK: - internals

    /// Keep what was heard when it was already right; repairs are lowercase.
    static func spelled(_ heard: String, _ name: String) -> String {
        if foldWords(heard) == name { return heard }
        let firstUpper = heard.first.map { $0.isUppercase } ?? false
        return firstUpper ? name.prefix(1).uppercased() + name.dropFirst() : name
    }

    static func digitsFor(_ piece: String) -> String? {
        let folded = foldWords(piece)
        if let mapped = digits[folded] { return mapped }
        if !folded.isEmpty && folded.allSatisfy({ $0.isNumber }) { return folded }
        return nil
    }

    /// The version number that begins at `start`, and how many pieces it ate.
    /// Only spaces may separate its parts: a number past a comma is another clause.
    static func versionAfter(_ pieces: [String], _ start: Int) -> (String, Int) {
        var parts: [String] = []
        var index = start
        var consumed = 0

        func takeGap(_ at: Int) -> Int? {
            (at < pieces.count && isSpace(pieces[at])) ? at + 1 : nil
        }

        while true {
            guard let afterGap = takeGap(index), afterGap < pieces.count else { break }
            let digit = isWord(pieces[afterGap]) ? digitsFor(pieces[afterGap]) : nil
            guard let digit else { break }
            parts.append(digit)
            index = afterGap + 1
            consumed = afterGap + 1 - start
            guard let dot = takeGap(index), dot < pieces.count,
                  dotWords.contains(foldWords(pieces[dot])) else { break }
            index = dot + 1
        }
        return (parts.joined(separator: "."), consumed)
    }

    /// Split into alternating word / non-word runs, preserving every character
    /// (== Python `re.compile(r"\w+|\W+")`).
    static func splitWords(_ text: String) -> [String] {
        var pieces: [String] = []
        var current = ""
        var currentIsWord: Bool?
        for ch in text {
            let isW = ch.isLetter || ch.isNumber || ch == "_"
            if currentIsWord == nil {
                currentIsWord = isW
                current.append(ch)
            } else if currentIsWord == isW {
                current.append(ch)
            } else {
                pieces.append(current)
                current = String(ch)
                currentIsWord = isW
            }
        }
        if !current.isEmpty { pieces.append(current) }
        return pieces
    }

    static func isWord(_ piece: String) -> Bool {
        guard let first = piece.first else { return false }
        return first.isLetter || first.isNumber
    }

    static func isSpace(_ piece: String) -> Bool {
        !piece.isEmpty && piece.allSatisfy { $0.isWhitespace }
    }
}
