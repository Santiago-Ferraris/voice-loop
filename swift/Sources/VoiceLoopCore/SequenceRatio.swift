import Foundation

/// A faithful port of CPython `difflib.SequenceMatcher(None, a, b).ratio()` with
/// `autojunk=False` and no junk heuristic — the Ratcliff/Obershelp similarity
/// the spoken-model repair leans on. Reproduced rather than approximated so the
/// same thresholds hold: "opu"/"opus" == 0.857, "sonet"/"sonnet" == 0.909,
/// "opas"/"opus" == 0.75, "claudia"/"claude" == 0.769.
func sequenceRatio(_ a: [Character], _ b: [Character]) -> Double {
    let total = a.count + b.count
    if total == 0 { return 1.0 }
    let matches = matchingBlockSizes(a, b, 0, a.count, 0, b.count)
    return 2.0 * Double(matches) / Double(total)
}

func sequenceRatio(_ a: String, _ b: String) -> Double {
    sequenceRatio(Array(a), Array(b))
}

private struct Match { let i: Int; let j: Int; let size: Int }

private func b2jMap(_ b: [Character]) -> [Character: [Int]] {
    var map: [Character: [Int]] = [:]
    for (index, ch) in b.enumerated() { map[ch, default: []].append(index) }
    return map
}

private func findLongestMatch(
    _ a: [Character], _ b: [Character], _ b2j: [Character: [Int]],
    _ alo: Int, _ ahi: Int, _ blo: Int, _ bhi: Int
) -> Match {
    var besti = alo, bestj = blo, bestsize = 0
    var j2len: [Int: Int] = [:]
    for i in alo..<ahi {
        var newj2len: [Int: Int] = [:]
        for j in b2j[a[i]] ?? [] {
            if j < blo { continue }
            if j >= bhi { break }
            let k = (j2len[j - 1] ?? 0) + 1
            newj2len[j] = k
            if k > bestsize {
                besti = i - k + 1
                bestj = j - k + 1
                bestsize = k
            }
        }
        j2len = newj2len
    }
    return Match(i: besti, j: bestj, size: bestsize)
}

private func matchingBlockSizes(
    _ a: [Character], _ b: [Character], _ alo: Int, _ ahi: Int, _ blo: Int, _ bhi: Int
) -> Int {
    let b2j = b2jMap(b)
    var total = 0
    var queue: [(Int, Int, Int, Int)] = [(alo, ahi, blo, bhi)]
    while let (alo, ahi, blo, bhi) = queue.popLast() {
        let m = findLongestMatch(a, b, b2j, alo, ahi, blo, bhi)
        if m.size > 0 {
            total += m.size
            if alo < m.i && blo < m.j {
                queue.append((alo, m.i, blo, m.j))
            }
            if m.i + m.size < ahi && m.j + m.size < bhi {
                queue.append((m.i + m.size, ahi, m.j + m.size, bhi))
            }
        }
    }
    return total
}
