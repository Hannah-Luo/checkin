from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.config import RosterCfg, resolve_path
from app.matcher import normalise, roster_warnings


_COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "timestamp":         ("timestamp",),
    "email":             ("emailaddress", "email"),
    "name":              ("name",),
    "matric":            ("matricutionnumber", "matriculationnumber", "matricnumber", "matric"),
    "phone":             ("phonenumber", "phone", "mobile"),
    "gender":            ("gender", "sex"),
    "session_selection": ("sessionselection", "sessionpreference", "availability"),
    "repeat":            ("repeat",),
    "session":           ("allocatedtimeslot", "allocatedsession", "allocatedslot"),
    "shortlist_sent":    ("shortlistemailsent", "shortlistsent"),
    "confirmed":         ("confirmed", "confirmation"),
    "showup":            ("showup", "shownup", "attended"),
}

_RESOLVE_ORDER = ("timestamp", "email", "matric", "session_selection", "session",
                  "shortlist_sent", "confirmed", "showup", "repeat", "phone",
                  "gender", "name")

_FALSE_VALUES = {"", "0", "no", "n", "false", "not confirmed", "pending"}

MATRIC_SHAPE = re.compile(r"^[A-Z]\d{7}[A-Z]$")

_DATE_RE = re.compile(
    r"(?:[A-Z][a-z]{2,9}\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,)?\s*\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]{2,9}\s*,?\s*\d{4}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})")
_SESSION_MARKER_RE = re.compile(r"\bSESSION\s*0*(\d+)\b", re.I)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def map_columns(headers: list[str]) -> dict[str, str]:
    normed = [(h, _norm_header(h)) for h in headers]
    taken: set[str] = set()
    out: dict[str, str] = {}
    for field_name in _RESOLVE_ORDER:
        for hint in _COLUMN_HINTS[field_name]:
            hit = next((h for h, n in normed if h not in taken and hint in n), None)
            if hit:
                out[field_name] = hit
                taken.add(hit)
                break
    return out


def parse_session_options(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    if not text:
        return out
    markers = list(_SESSION_MARKER_RE.finditer(text))
    if not markers:
        return out

    anchors = [m.start() for m in _DATE_RE.finditer(text)]
    if anchors and anchors[0] < markers[0].start():
        starts = []
        for m in markers:
            prior = [a for a in anchors if a <= m.start()]
            starts.append(prior[-1] if prior else m.start())
    else:
        starts = [m.start() for m in markers]

    for idx, m in enumerate(markers):
        start = starts[idx]
        end = starts[idx + 1] if idx + 1 < len(markers) else len(text)
        if end <= start:
            start = m.start()
            end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
        label = text[start:end].strip().strip(",;|/ \t")
        num = int(m.group(1))
        out.setdefault(num, label or f"Session {num}")
    return out


@dataclass
class Issue:
    level: str
    code: str
    message: str
    row: int | None = None

    def __str__(self) -> str:
        where = f" (row {self.row})" if self.row else ""
        return f"[{self.level.upper():5}] {self.message}{where}"


@dataclass
class Participant:
    row: int
    matric: str
    name: str = ""
    session: int | None = None
    confirmed: bool = False
    matric_raw: str = ""
    repeat: str = ""
    shortlist_sent: str = ""
    showup_raw: str = ""
    session_selection: str = ""
    ticked_sessions: list[int] = field(default_factory=list)

    @property
    def masked(self) -> str:
        return mask_matric(self.matric)

    def as_dict(self) -> dict:
        return {"row": self.row, "matric": self.matric, "masked": self.masked,
                "name": self.name, "session": self.session,
                "confirmed": self.confirmed}


def mask_matric(matric: str, mask_char: str = "•") -> str:
    if len(matric) <= 5:
        return matric
    return matric[:4] + mask_char * (len(matric) - 5) + matric[-1]


def mask_name(name: str) -> str:
    parts = [p for p in re.sub(r"\s+", " ", name or "").strip().split(" ") if p]
    return " ".join(p[0] + "." for p in parts)


@dataclass
class SessionRoster:
    number: int
    label: str
    participants: list[Participant]

    @property
    def confirmed(self) -> list[Participant]:
        return [p for p in self.participants if p.confirmed]

    @property
    def unconfirmed(self) -> list[Participant]:
        return [p for p in self.participants if not p.confirmed]

    @property
    def matrics(self) -> list[str]:
        return [p.matric for p in self.participants]

    def by_matric(self, matric: str) -> Participant | None:
        key = normalise(matric)
        return next((p for p in self.participants if p.matric == key), None)

    def warnings(self) -> list[tuple[str, str, float]]:
        return roster_warnings(self.matrics)


@dataclass
class Roster:
    participants: list[Participant]
    session_labels: dict[int, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    source: str = ""
    columns: dict[str, str] = field(default_factory=dict)

    def sessions(self) -> list[int]:
        return sorted({p.session for p in self.participants if p.session is not None})

    def label_for(self, number: int) -> str:
        return self.session_labels.get(number, f"Session {number}")

    def for_session(self, number: int) -> SessionRoster:
        members = [p for p in self.participants if p.session == number]
        return SessionRoster(number, self.label_for(number), members)

    def by_matric(self, matric: str) -> Participant | None:
        key = normalise(matric)
        return next((p for p in self.participants if p.matric == key), None)

    def counts(self) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = defaultdict(lambda: {"total": 0, "confirmed": 0})
        for p in self.participants:
            if p.session is None:
                continue
            out[p.session]["total"] += 1
            out[p.session]["confirmed"] += int(p.confirmed)
        return dict(sorted(out.items()))

    def find(self, query: str, session: int | None = None, limit: int = 8) -> list[Participant]:
        q = (query or "").strip()
        if not q:
            return []
        pool = [p for p in self.participants if session is None or p.session == session]
        qn = normalise(q)
        qname = re.sub(r"\s+", " ", q.lower()).strip()

        scored: list[tuple[int, Participant]] = []
        for p in pool:
            name = p.name.lower()
            if qn and p.matric == qn:
                rank = 0
            elif qn and p.matric.endswith(qn):
                rank = 1
            elif qn and qn in p.matric:
                rank = 2
            elif qname and name.startswith(qname):
                rank = 3
            elif qname and qname in name:
                rank = 4
            elif qname and all(t in name for t in qname.split()):
                rank = 5
            else:
                continue
            scored.append((rank, p))
        scored.sort(key=lambda rp: (rp[0], rp[1].name))
        return [p for _, p in scored[:limit]]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def report(self) -> str:
        lines = [f"Roster: {len(self.participants)} rows from {self.source}"]
        missing = [f for f in ("matric", "name", "session", "confirmed")
                   if f not in self.columns]
        if missing:
            lines.append(f"  MISSING COLUMNS: {', '.join(missing)}")
        lines.append("  sessions: " + (", ".join(
            f"{n}: {c['total']} rows ({c['confirmed']} confirmed)"
            for n, c in self.counts().items()) or "none"))
        by_level = Counter(i.level for i in self.issues)
        lines.append(f"  issues: {by_level.get('error', 0)} error, "
                     f"{by_level.get('warn', 0)} warn, {by_level.get('info', 0)} info")
        for i in self.issues:
            lines.append("    " + str(i))
        return "\n".join(lines)


def _read_csv_text(text: str) -> tuple[list[dict], list[str]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = [r for r in reader]
    return rows, list(reader.fieldnames or [])


def _fetch(url: str, timeout: float) -> str:
    import requests
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def load_roster_text(cfg: RosterCfg) -> tuple[str, str, list[Issue]]:
    issues: list[Issue] = []

    if cfg.csv_path:
        path = resolve_path(cfg.csv_path)
        if path.exists():
            return path.read_text(encoding="utf-8-sig"), f"file {path}", issues
        issues.append(Issue("warn", "csv_missing",
                            f"roster.csv_path set but not found: {path}"))

    cache = resolve_path(cfg.cache_path) if cfg.cache_path else None
    url = cfg.csv_url
    if url:
        try:
            return _fetch(url, cfg.timeout_seconds), f"sheet {cfg.sheet_id}", issues
        except Exception as exc:
            issues.append(Issue("warn", "sheet_unreachable",
                                f"could not read the sheet ({exc.__class__.__name__}: {exc})"))

    if cache and cache.exists():
        issues.append(Issue("warn", "using_cache",
                            f"USING CACHED ROSTER from {cache} -- it may be stale"))
        return cache.read_text(encoding="utf-8-sig"), f"cache {cache}", issues

    issues.append(Issue("error", "no_roster",
                        "no roster source available (set roster.csv_path or roster.sheet_id)"))
    return "", "none", issues


def parse_roster(text: str, cfg: RosterCfg | None = None, source: str = "") -> Roster:
    cfg = cfg or RosterCfg()
    true_values = {v.strip().lower() for v in cfg.confirmed_true_values}
    rows, headers = _read_csv_text(text)
    cols = map_columns(headers)
    issues: list[Issue] = []

    for required in ("matric", "session", "confirmed"):
        if required not in cols:
            issues.append(Issue("error", "missing_column",
                                f"no column found for '{required}' in: {headers}"))

    def cell(row: dict, key: str) -> str:
        col = cols.get(key)
        return (row.get(col) or "").strip() if col else ""

    participants: list[Participant] = []
    label_votes: dict[int, Counter] = defaultdict(Counter)

    for idx, row in enumerate(rows, start=2):
        matric_raw = cell(row, "matric")
        matric = normalise(matric_raw)
        name = re.sub(r"\s+", " ", cell(row, "name"))

        if not matric:
            issues.append(Issue("error", "no_matric",
                                f"no matric number for {mask_name(name) or '(no name)'} "
                                f"-- row skipped", idx))
            continue
        if not MATRIC_SHAPE.match(matric):
            issues.append(Issue("warn", "matric_shape",
                                f"{matric} for {mask_name(name)} is not letter+7 digits+letter",
                                idx))

        raw_session = cell(row, "session")
        session: int | None = None
        if raw_session:
            m = re.search(r"\d+", raw_session)
            if m:
                session = int(m.group())
            else:
                issues.append(Issue("warn", "bad_session",
                                    f"{matric}: cannot read a session number from "
                                    f"'{raw_session}'", idx))
        else:
            issues.append(Issue("warn", "no_session",
                                f"{matric} ({mask_name(name)}) has no allocated time slot", idx))

        raw_confirmed = cell(row, "confirmed")
        confirmed = raw_confirmed.strip().lower() in true_values
        if not confirmed and raw_confirmed.strip().lower() not in _FALSE_VALUES:
            issues.append(Issue("warn", "bad_confirmed",
                                f"{matric}: unrecognised Confirmed value "
                                f"'{raw_confirmed}' -- treated as NOT confirmed", idx))

        selection = cell(row, "session_selection")
        options = parse_session_options(selection)
        for num, label in options.items():
            label_votes[num][label] += 1

        if session is not None and options and session not in options:
            issues.append(Issue("info", "slot_not_ticked",
                                f"{matric} ({mask_name(name)}) is allocated to session "
                                f"{session} but ticked {sorted(options)}", idx))

        participants.append(Participant(
            row=idx, matric=matric, matric_raw=matric_raw, name=name,
            session=session, confirmed=confirmed, repeat=cell(row, "repeat"),
            shortlist_sent=cell(row, "shortlist_sent"), showup_raw=cell(row, "showup"),
            session_selection=selection, ticked_sessions=sorted(options),
        ))

    seen: dict[str, int] = {}
    for p in participants:
        if p.matric in seen:
            issues.append(Issue("error", "duplicate_matric",
                                f"{p.matric} appears twice (rows {seen[p.matric]} and {p.row})",
                                p.row))
        else:
            seen[p.matric] = p.row

    labels = {num: votes.most_common(1)[0][0] for num, votes in label_votes.items()}

    roster = Roster(participants=participants, session_labels=labels,
                    issues=issues, source=source, columns=cols)

    for number in roster.sessions():
        for a, b, d in roster.for_session(number).warnings():
            issues.append(Issue("warn", "confusable_pair",
                                f"session {number}: {a} and {b} differ by only {d:.2f} -- "
                                f"any imperfect read of either will need manual confirmation"))
    return roster


CACHE_HEADERS = ("Matricution Number", "Name", "Allocated Time Slot", "Confirmed",
                 "Repeat", "Shortlist Sent", "Show-up", "Session Selection")


def roster_csv(roster: Roster, true_value: str = "1") -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CACHE_HEADERS)
    for p in roster.participants:
        writer.writerow([p.matric_raw or p.matric, p.name,
                         "" if p.session is None else p.session,
                         true_value if p.confirmed else "0",
                         p.repeat, p.shortlist_sent, p.showup_raw,
                         p.session_selection])
    return buf.getvalue()


def cache_if_usable(cfg: RosterCfg, text: str, source: str, roster: Roster) -> bool:
    if not (cfg.cache_path and text and source.startswith("sheet")
            and roster.participants):
        return False
    true_value = next((v for v in cfg.confirmed_true_values if v.strip()), "1")
    path = resolve_path(cfg.cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(roster_csv(roster, true_value), encoding="utf-8")
    return True


def load_roster(cfg: RosterCfg | None = None) -> Roster:
    cfg = cfg or RosterCfg()
    text, source, issues = load_roster_text(cfg)
    roster = parse_roster(text, cfg, source) if text else Roster([], source=source)
    roster.issues = issues + roster.issues
    cache_if_usable(cfg, text, source, roster)
    return roster


if __name__ == "__main__":
    import sys

    from app.config import load_config

    conf = load_config()
    r = load_roster(conf.roster)
    print(r.report())
    number = int(sys.argv[1]) if len(sys.argv) > 1 else conf.session.number
    sr = r.for_session(number)
    print(f"\nSession {number}: {sr.label}")
    print(f"  {len(sr.participants)} allocated, {len(sr.confirmed)} confirmed, "
          f"{len(sr.unconfirmed)} unconfirmed")
    for p in sr.participants:
        print(f"    {'C' if p.confirmed else ' '}  {p.matric}  {p.name}")
