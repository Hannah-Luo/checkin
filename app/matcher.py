from __future__ import annotations

import re
from dataclasses import dataclass, field

_CONFUSABLE_GROUPS = [
    "0ODQ",
    "1ILJ7T",
    "2Z",
    "5S",
    "6G",
    "8B",
    "9Q",
    "4A",
    "UV",
    "MN",
    "CG",
    "KX",
]
CONFUSION_COST = 0.3
_CHEAP: set[frozenset[str]] = {
    frozenset((a, b))
    for grp in _CONFUSABLE_GROUPS
    for i, a in enumerate(grp)
    for b in grp[i + 1:]
}

CANDIDATE_RE = re.compile(r"^[A-Z0-9]{7,11}$")
MIN_DIGITS = 5

MAX_DISTANCE = 1.2
MIN_MARGIN = 0.8


def normalise(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return CONFUSION_COST if frozenset((a, b)) in _CHEAP else 1.0


def weighted_distance(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if not a or not b:
        return float(len(a) or len(b))

    prev = [float(j) for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1.0,
                cur[j - 1] + 1.0,
                prev[j - 1] + _sub_cost(ca, cb),
            ))
        prev = cur
    return prev[-1]


def is_candidate(token: str) -> bool:
    return bool(CANDIDATE_RE.match(token)) and sum(c.isdigit() for c in token) >= MIN_DIGITS


def extract_candidates(ocr_texts: list[str]) -> list[str]:
    seen, out = set(), []
    for raw in ocr_texts:
        tok = normalise(raw)
        if tok and tok not in seen and is_candidate(tok):
            seen.add(tok)
            out.append(tok)
    return out


@dataclass
class MatchResult:
    token: str
    best: str | None = None
    best_distance: float = float("inf")
    runner_up: str | None = None
    runner_up_distance: float = float("inf")
    accepted: bool = False
    reason: str = ""
    ranked: list[tuple[str, float]] = field(default_factory=list)

    @property
    def margin(self) -> float:
        return self.runner_up_distance - self.best_distance


def match(token: str, roster: list[str],
          max_distance: float = MAX_DISTANCE,
          min_margin: float = MIN_MARGIN) -> MatchResult:
    tok = normalise(token)
    res = MatchResult(token=tok)
    if not roster:
        res.reason = "empty roster"
        return res

    ranked = sorted(((r, weighted_distance(tok, normalise(r))) for r in roster),
                    key=lambda kv: kv[1])
    res.ranked = ranked[:3]
    res.best, res.best_distance = ranked[0]
    if len(ranked) > 1:
        res.runner_up, res.runner_up_distance = ranked[1]

    if res.best_distance == 0.0:
        res.accepted = True
        res.reason = "exact match"
    elif res.best_distance > max_distance:
        res.reason = f"no roster entry within {max_distance} (best {res.best_distance:.2f})"
    elif res.margin < min_margin:
        res.reason = (f"ambiguous: {res.best} @{res.best_distance:.2f} vs "
                      f"{res.runner_up} @{res.runner_up_distance:.2f} "
                      f"(margin {res.margin:.2f} < {min_margin})")
    else:
        res.accepted = True
        res.reason = f"accepted @{res.best_distance:.2f}, margin {res.margin:.2f}"
    return res


def roster_warnings(roster: list[str], threshold: float = MIN_MARGIN) -> list[tuple[str, str, float]]:
    norm_roster = [(r, normalise(r)) for r in roster]
    out = []
    for i, (ra, na) in enumerate(norm_roster):
        for rb, nb in norm_roster[i + 1:]:
            d = weighted_distance(na, nb)
            if d < threshold:
                out.append((ra, rb, d))
    return sorted(out, key=lambda t: t[2])


def best_match(ocr_texts: list[str], roster: list[str], **kw) -> MatchResult | None:
    results = [match(c, roster, **kw) for c in extract_candidates(ocr_texts)]
    if not results:
        return None
    accepted = [r for r in results if r.accepted]
    pool = accepted or results
    return min(pool, key=lambda r: r.best_distance)
