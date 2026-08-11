import Foundation

/// Lowercase + unaccented, the base form every vocabulary set is written in.
///
/// The Python codebase grew three folds that differ only in how they treat
/// non-alphanumerics; they are reproduced here exactly so the ported logic
/// matches byte for byte.

/// NFD-decompose, drop combining marks, lowercase. Nothing else touched.
/// (== `vocabulary.fold`)
public func fold(_ text: String) -> String {
    var out = String.UnicodeScalarView()
    for scalar in text.decomposedStringWithCanonicalMapping.unicodeScalars
    where scalar.properties.canonicalCombiningClass == .notReordered {
        out.append(scalar)
    }
    return String(out).lowercased()
}

/// `fold`, then collapse every run outside `[0-9a-z]` (whitespace and
/// punctuation alike) to a single space, and strip. This is the shared output
/// of both `intents.fold` (used by the normalizer) and `naming.fold` (used by
/// slugify) — they differ only in intermediate steps and land on the same text.
public func foldWords(_ text: String) -> String {
    let base = fold(text)
    var out = ""
    var lastWasSpace = false
    for ch in base {
        let keep = ch.isASCII && (ch.isNumber || ("a"..."z").contains(ch))
        if keep {
            out.append(ch)
            lastWasSpace = false
        } else {
            if !lastWasSpace { out.append(" ") }
            lastWasSpace = true
        }
    }
    return out.trimmingCharacters(in: .whitespaces)
}
