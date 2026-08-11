import Foundation
#if canImport(Darwin)
import Darwin
#endif

/// The words you actually use, taken from the words you actually used. Port of
/// `vocabulary.py`.
///
/// `keyterms` is what stops the recognizer inventing domain vocabulary — without
/// it "mergealo" comes back as "MGalo". It is derived from your own Claude Code
/// prompts under `~/.claude/projects/*/*.jsonl`: count the words in your
/// messages, drop ordinary Spanish, and what survives is the jargon.
public enum Vocabulary {
    public static let defaultTranscriptGlob = "~/.claude/projects/*/*.jsonl"
    public static let defaultLimit = 80
    public static let defaultMinCount = 3
    public static let minTermChars = 2
    public static let maxTermChars = 24
    public static let defaultFiles = 200

    /// Lines Claude writes into the user's own turn: slash-command envelopes, the
    /// resumed-session caveat, hook output. None of it was said by anybody.
    static let injectedPrefixes: [String] = [
        "<command-name", "<command-message", "<command-args", "<local-command",
        "<user-prompt-submit-hook", "<system-reminder", "<bash-input", "caveat:",
    ]

    static func isInjected(_ text: String) -> Bool {
        let lowered = text.drop(while: { $0.isWhitespace }).lowercased()
        return injectedPrefixes.contains { lowered.hasPrefix($0) }
    }

    /// Ordinary Spanish and the English glue around it. What survives is jargon.
    static let common: Set<String> = Set("""
    a acá ahi ahora al algo alguna algunas alguno algunos alla alli alto ambos and
    antes any aquel aquella aquello aqui arriba asi aun aunque bajo bien but cada
    casi como con contra cosa cosas creo cual cuales cuando cuanto cual da dale dar
    de debe debajo decir dejar del demas demasiado dentro desde despues dice dicho
    dio donde dos e el ella ellas ello ellos en encima entonces entre era eran eres
    es esa esas ese eso esos esta estaba estan estar estas este esto estos estoy
    falta fin for fue fueron gracias ha habia hace hacen hacer hacia hago han hasta
    hay he hecho hizo hola hoy igual in is la las le les listo lo los luego mas me
    mejor menos mi mientras mio misma mismo mucho muchos muy nada nadie ni no nos
    nosotros nuestra nuestro nueva nuevo nunca o of ok on or otra otras otro otros
    para pero pesar poco podemos poder podes podria por porque pues que queda quedo
    queres quien quiere quiero se sea segun sen ser si sido siempre sin sino sobre
    solo son soy su sus tal tambien tampoco tan tanto te tenemos tener tenes tengo
    the tiene tienen toda todas todo todos tu tus un una uno unos usa usar va vamos
    van vas ver vez viene vos voy y ya yo
    """.split(whereSeparator: { $0.isWhitespace }).map { fold(String($0)) })

    /// Words that are ordinary Spanish *here* — said to a terminal all day.
    static let commonWork: Set<String> = Set("""
    abri abrir acordate agrega agregar anda andar arregla arreglar borra borrar
    busca buscar cambia cambiar cerra cerrar chequea checa corre correr corri
    dejalo escribi fijate hace haga hagamos leelo lee leer mandale manda mira mostra
    mostrame necesito pone poner probalo proba probar quiero revisa revisar saca
    sacar segui seguir termina terminar tira tirar usa vemos veo
    """.split(whereSeparator: { $0.isWhitespace }).map { fold(String($0)) })

    static let stop: Set<String> = common.union(commonWork)

    /// The jargon tokens in one message: folded, non-word split, stop-words and
    /// digit-bearing tokens dropped, length-bounded.
    static func terms(in text: String) -> [String] {
        var out: [String] = []
        for token in foldWords(text).split(separator: " ").map(String.init) {
            guard minTermChars <= token.count && token.count <= maxTermChars else { continue }
            if stop.contains(token) { continue }
            if token.contains(where: { $0.isNumber }) { continue }
            out.append(token)
        }
        return out
    }

    /// The vocabulary a batch of user messages is written in, most used first.
    public static func extract(
        fromMessages messages: [String],
        minCount: Int = defaultMinCount,
        limit: Int = defaultLimit
    ) -> [String] {
        var tally: [String: Int] = [:]
        for message in messages {
            for term in terms(in: message) { tally[term, default: 0] += 1 }
        }
        let ranked = tally
            .filter { $0.value >= minCount }
            .sorted { lhs, rhs in
                lhs.value != rhs.value ? lhs.value > rhs.value : lhs.key < rhs.key
            }
            .map(\.key)
        return Array(ranked.prefix(limit))
    }

    // MARK: - transcript IO

    /// The text of one user turn: a plain string, or the joined `type:text`
    /// blocks. Tool results and images are not speech.
    static func contentText(_ content: Any?) -> String {
        if let text = content as? String { return text }
        if let blocks = content as? [[String: Any]] {
            let parts = blocks.compactMap { block -> String? in
                (block["type"] as? String) == "text" ? (block["text"] as? String) : nil
            }.filter { !$0.isEmpty }
            return parts.joined(separator: "\n")
        }
        return ""
    }

    /// Everything *you* typed in one transcript, envelopes and sidechains out.
    public static func userMessages(atPath path: String) -> [String] {
        guard let content = try? String(contentsOfFile: path, encoding: .utf8) else { return [] }
        var out: [String] = []
        for line in content.split(separator: "\n", omittingEmptySubsequences: true) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8),
                  let entry = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else { continue }
            guard (entry["type"] as? String) == "user" else { continue }
            if entry["isSidechain"] as? Bool == true || entry["isMeta"] as? Bool == true { continue }
            guard let message = entry["message"] as? [String: Any] else { continue }
            let text = contentText(message["content"]).trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty || isInjected(text) { continue }
            out.append(text)
        }
        return out
    }

    /// Every Claude transcript on this machine, newest first, capped.
    public static func transcriptPaths(glob: String = defaultTranscriptGlob, limit: Int = defaultFiles) -> [String] {
        let expanded = NSString(string: glob).expandingTildeInPath
        let matches = globMatch(expanded)
        let sorted = matches.sorted { lhs, rhs in
            mtime(lhs) > mtime(rhs)
        }
        return Array(sorted.prefix(limit))
    }

    static func mtime(_ path: String) -> TimeInterval {
        let attrs = try? FileManager.default.attributesOfItem(atPath: path)
        return (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
    }

    /// Recompute from the newest transcripts.
    public static func refresh(
        glob: String = defaultTranscriptGlob,
        minCount: Int = defaultMinCount,
        limit: Int = defaultLimit,
        files: Int = defaultFiles
    ) -> [String] {
        let paths = transcriptPaths(glob: glob, limit: files)
        let messages = paths.flatMap { userMessages(atPath: $0) }
        return extract(fromMessages: messages, minCount: minCount, limit: limit)
    }

    // MARK: - persistence

    public static func storePath(stateDir: String) -> String {
        (stateDir as NSString).appendingPathComponent("keyterms.json")
    }

    public static func save(stateDir: String, terms: [String], now: TimeInterval) throws {
        let path = storePath(stateDir: stateDir)
        try FileManager.default.createDirectory(
            atPath: (path as NSString).deletingLastPathComponent,
            withIntermediateDirectories: true
        )
        let payload: [String: Any] = ["generated_at": now, "terms": terms]
        let data = try JSONSerialization.data(withJSONObject: payload)
        try data.write(to: URL(fileURLWithPath: path))
    }

    /// The last extraction, or nothing. Never throws: this is a nicety.
    public static func load(stateDir: String) -> [String] {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: storePath(stateDir: stateDir))),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let terms = obj["terms"] as? [String]
        else { return [] }
        return terms.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
    }
}

/// A tiny glob over `dir/*/*.ext` style patterns, enough for the transcript path.
func globMatch(_ pattern: String) -> [String] {
    var globResult = glob_t()
    defer { globfree(&globResult) }
    guard glob(pattern, 0, nil, &globResult) == 0 else { return [] }
    var out: [String] = []
    let count = Int(globResult.gl_matchc)
    for i in 0..<count {
        if let cString = globResult.gl_pathv[i] {
            out.append(String(cString: cString))
        }
    }
    return out
}
