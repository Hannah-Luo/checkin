from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from app.config import Config
from app.matcher import normalise
from app.roster import Participant, Roster, SessionRoster

SEAT_HOLDING = ("seated", "promoted")

QUEUE_TIERS = {0: "allocated, confirmed", 1: "allocated, not confirmed",
               2: "walk-in"}


class Status(str, Enum):
    SEATED = "seated"
    PROMOTED = "promoted"
    WAITING = "waiting"
    RELEASED = "released"
    DISMISSED = "dismissed"


@dataclass
class Entry:
    matric: str
    name: str = ""
    confirmed: bool = False
    allocated_session: int | None = None
    status: Status = Status.WAITING
    table: int | None = None
    group: int | None = None
    subject_id: str = ""
    arrival_seq: int = 0
    arrival_time: str = ""
    method: str = "manual"
    in_roster: bool = True
    confidence: float | None = None
    note: str = ""

    @property
    def holds_table(self) -> bool:
        return self.table is not None and self.status.value in SEAT_HOLDING

    @property
    def sheet_show_up(self) -> int:
        return 0 if self.status is Status.RELEASED else 1

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["status"] = self.status.value
        return d


@dataclass
class CheckInResult:
    outcome: str
    entry: Entry | None = None
    reason: str = ""
    wrong_session: bool = False
    in_roster: bool = True
    waiting_position: int | None = None
    already_released: bool = False

    @property
    def display(self) -> str:
        if self.outcome in ("seated", "already") and self.entry and self.entry.table:
            return f"TABLE {self.entry.table}"
        if self.outcome == "released":
            if self.wrong_session:
                return "ANOTHER SESSION"
            return ("CONFIRMED" if self.entry and self.entry.confirmed
                    else "NOT CONFIRMED")
        if self.outcome == "waiting":
            return f"WAITING LIST #{self.waiting_position}"
        return "SEE THE OPERATOR"

    @property
    def colour(self) -> str:
        if self.outcome in ("seated", "already"):
            return "green"
        if not self.in_roster:
            return "red" if self.outcome == "refused" else "amber"
        if self.wrong_session:
            return "amber"
        if self.outcome == "released":
            return "amber"
        if self.outcome == "waiting":
            return "blue"
        return "amber"


@dataclass
class FinaliseResult:
    kept: list[Entry] = field(default_factory=list)
    dismissed: list[Entry] = field(default_factory=list)
    moves: list[tuple[str, int, int]] = field(default_factory=list)
    groups: dict[int, list[Entry]] = field(default_factory=dict)
    seed: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def n_kept(self) -> int:
        return len(self.kept)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def hhmm(stamp: str) -> str:
    return stamp[11:16] if len(stamp) >= 16 and stamp[10:11] == "T" else stamp


def blocks_intact(deck: list[int], size: int) -> bool:
    for start in range(0, len(deck), size):
        chunk = deck[start:start + size]
        if set(chunk) != set(range(start + 1, start + len(chunk) + 1)):
            return False
    return True


def build_deck(total: int, order: str, seed: int | None,
               saved: list[int] | None = None, frozen: int = 0,
               block: int = 0) -> list[int]:
    if order != "random":
        return list(range(1, total + 1))
    size = block if block and block > 1 else total
    if (saved and sorted(saved) == list(range(1, total + 1))
            and blocks_intact(saved, size)):
        return list(saved)

    keep = [t for t in (saved or [])[:frozen] if 1 <= t <= total]
    rng = random.Random(seed)
    deck: list[int] = []
    for start in range(1, total + 1, size):
        chunk = list(range(start, min(start + size, total + 1)))
        rng.shuffle(chunk)
        deck.extend(chunk)

    where = {t: i for i, t in enumerate(deck)}
    for i, t in enumerate(keep):
        j = where[t]
        if i == j:
            continue
        other = deck[i]
        deck[i], deck[j] = t, other
        where[t], where[other] = i, j
    return deck


class Allocator:

    def __init__(self, cfg: Config, roster: Roster | None = None, clock=_now,
                 seat_seed: int | None = None, seat_deck: list[int] | None = None,
                 seat_frozen: int = 0):
        self.cfg = cfg
        self.roster = roster or Roster([])
        self.session_number = cfg.session.number
        self.clock = clock

        self.group_size = max(1, cfg.grouping.group_size)
        self.blocked_repeats = {str(v).strip()
                                for v in cfg.waitlist.block_repeat_values}

        self.seat_order = cfg.layout.seat_order
        self.seat_seed = (None if self.seat_order != "random"
                          else (random.randrange(1_000_000) if seat_seed is None
                                else int(seat_seed)))
        tables = build_deck(cfg.layout.total_tables, self.seat_order,
                            self.seat_seed, saved=seat_deck, frozen=seat_frozen,
                            block=self.group_size)
        self._slot_to_table = tables
        self._table_to_slot = {t: i + 1 for i, t in enumerate(tables)}

        self.release_mode = False
        self.room: tuple[int, int] | None = None

        self.entries: dict[str, Entry] = {}
        self.events: list[dict] = []
        self.next_table = 1
        self.freed: list[int] = []
        self._arrivals = 0
        self.finalised: FinaliseResult | None = None

        self.warnings: list[str] = []

    @property
    def session_roster(self) -> SessionRoster:
        return self.roster.for_session(self.session_number)

    def _log(self, action: str, **fields) -> None:
        self.events.append({"at": self.clock(), "action": action, **fields})

    def _in_room(self, slot: int) -> bool:
        if self.room is None:
            return True
        lo, hi = self.room
        return lo <= self._slot_to_table[slot - 1] <= hi

    def _take_table(self) -> int | None:
        freed = sorted(s for s in self.freed if self._in_room(s))
        if freed:
            self.freed.remove(freed[0])
            heapq.heapify(self.freed)
            return self._slot_to_table[freed[0] - 1]
        target = next((s for s in range(self.next_table,
                                        len(self._slot_to_table) + 1)
                       if self._in_room(s)), None)
        if target is None:
            return None
        for s in range(self.next_table, target):
            heapq.heappush(self.freed, s)
        self.next_table = target + 1
        return self._slot_to_table[target - 1]

    def _release_table(self, table: int) -> None:
        slot = self._table_to_slot.get(table)
        if slot is None:
            return
        if slot == self.next_table - 1:
            self.next_table -= 1
        else:
            heapq.heappush(self.freed, slot)

    def _slot_of(self, e: Entry) -> int:
        return self._table_to_slot.get(e.table, e.table)

    def _arrival(self) -> int:
        self._arrivals += 1
        return self._arrivals

    @property
    def seat_deck(self) -> list[int]:
        return list(self._slot_to_table)

    def frozen_prefix(self) -> int:
        held = {e.table for e in self.entries.values() if e.holds_table}
        return max((i for i, t in enumerate(self._slot_to_table, 1) if t in held),
                   default=0)

    def occupied_above(self, total: int) -> list[int]:
        return sorted(e.table for e in self.entries.values()
                      if e.holds_table and e.table > total)

    def dealt_above(self, total: int) -> list[int]:
        return sorted(t for t in self._slot_to_table[:self.frozen_prefix()]
                      if t > total)

    def check_in(self, matric: str, *, method: str = "manual",
                 name: str = "", confidence: float | None = None,
                 force_walk_in: bool = False) -> CheckInResult:
        key = normalise(matric)
        if not key:
            return CheckInResult("refused", reason="empty matric", in_roster=False)

        if key in self.entries:
            e = self.entries[key]
            self._log("rescan", matric=key, status=e.status.value, table=e.table)
            if e.holds_table:
                return CheckInResult("already", e, reason="already checked in")
            if e.status is Status.RELEASED:
                return CheckInResult(
                    "released", e, already_released=True,
                    reason=f"already released at {hhmm(e.arrival_time)} "
                           f"({e.note}) — do not pay again",
                    wrong_session=self.wrong_session(e), in_roster=e.in_roster)
            return CheckInResult("waiting", e, reason="already on the waiting list",
                                 waiting_position=self.waiting_position(e))

        person: Participant | None = self.roster.by_matric(key)
        in_roster = person is not None
        wrong_session = bool(person and person.session != self.session_number)

        if (not in_roster and not self.cfg.waitlist.allow_not_in_roster
                and not force_walk_in):
            self._log("refused", matric=key, why="not in roster")
            return CheckInResult("refused", reason="not in this roster at all",
                                 in_roster=False)

        if person and person.repeat.strip() in self.blocked_repeats:
            self._log("refused", matric=key, why="repeat participant",
                      repeat=person.repeat.strip())
            return CheckInResult(
                "refused", reason=f"repeat participant (Repeat = "
                                  f"{person.repeat.strip()}) — not eligible")

        if wrong_session and not self.cfg.waitlist.allow_wrong_session and not force_walk_in:
            self._log("refused", matric=key, why="wrong session",
                      allocated=person.session)
            return CheckInResult("refused", reason=(
                f"allocated to session {person.session}, not {self.session_number}"
                if person.session is not None
                else "in the Form but no slot allocated"), wrong_session=True)

        entry = Entry(
            matric=key,
            name=person.name if person else name,
            confirmed=bool(person and person.confirmed),
            allocated_session=person.session if person else None,
            arrival_seq=self._arrival(),
            arrival_time=self.clock(),
            method=method,
            in_roster=in_roster,
            confidence=confidence,
        )

        if self.release_mode:
            entry.status = Status.RELEASED
            entry.note = self.registration(entry, wrong_session)
            self.entries[key] = entry
            self._log("released", matric=key, confirmed=entry.confirmed,
                      allocated=entry.allocated_session, method=method)
            return CheckInResult("released", entry, reason=entry.note,
                                 wrong_session=wrong_session, in_roster=in_roster)

        seatable = entry.confirmed and not wrong_session and in_roster
        table = self._take_table() if seatable else None

        if table is not None:
            entry.status = Status.SEATED
            entry.table = table
            self.entries[key] = entry
            self._log("seated", matric=key, table=table, method=method)
            return CheckInResult("seated", entry, reason="confirmed for this session")

        entry.status = Status.WAITING
        if seatable:
            entry.note = "room full"
        elif wrong_session:
            entry.note = (f"allocated to session {entry.allocated_session}"
                          if entry.allocated_session is not None
                          else "in the Form, no slot allocated")
        elif not in_roster:
            entry.note = ("card not in the Form — check the sheet"
                          if method == "ocr" else "not in the Form")
        else:
            entry.note = "not confirmed"
        self.entries[key] = entry
        self._log("waiting", matric=key, why=entry.note, method=method)
        return CheckInResult("waiting", entry, reason=entry.note,
                             wrong_session=wrong_session, in_roster=in_roster,
                             waiting_position=self.waiting_position(entry))

    def undo(self, matric: str) -> CheckInResult:
        key = normalise(matric)
        entry = self.entries.pop(key, None)
        if entry is None:
            return CheckInResult("refused", reason="no such check-in")
        if entry.table is not None:
            self._release_table(entry.table)
        self._log("undo", matric=key, table=entry.table, status=entry.status.value)
        return CheckInResult("refused", entry, reason="undone")

    def registration(self, e: Entry, wrong_session: bool | None = None) -> str:
        if wrong_session is None:
            wrong_session = self.wrong_session(e)
        if not e.in_roster:
            return "not in the Form"
        if wrong_session:
            return (f"allocated to session {e.allocated_session}"
                    if e.allocated_session is not None
                    else "in the Form, no slot allocated")
        return "confirmed" if e.confirmed else "not confirmed"

    def wrong_session(self, e: Entry) -> bool:
        return e.in_roster and e.allocated_session != self.session_number

    def is_walk_in(self, e: Entry) -> bool:
        return not e.in_roster or e.allocated_session != self.session_number

    def queue_rank(self, e: Entry) -> int:
        if self.is_walk_in(e):
            return 2
        return 0 if e.confirmed else 1

    def waiting(self) -> list[Entry]:
        return sorted((e for e in self.entries.values() if e.status == Status.WAITING),
                      key=lambda e: (self.queue_rank(e), e.arrival_seq))

    def waiting_position(self, entry: Entry) -> int | None:
        queue = self.waiting()
        return queue.index(entry) + 1 if entry in queue else None

    def seated(self) -> list[Entry]:
        return sorted((e for e in self.entries.values() if e.holds_table),
                      key=lambda e: e.table)

    def released(self) -> list[Entry]:
        return sorted((e for e in self.entries.values()
                       if e.status is Status.RELEASED),
                      key=lambda e: e.arrival_seq, reverse=True)

    def set_release_mode(self, on: bool) -> bool:
        on = bool(on)
        if on != self.release_mode:
            self.release_mode = on
            self._log("release_mode", on=on)
        return self.release_mode

    def set_room(self, span: tuple[int, int] | None) -> None:
        if span != self.room:
            self.room = span
            self._log("desk_room", span=list(span) if span else None)

    def promote(self, limit: int | None = None,
                walk_ins: bool | None = None) -> list[Entry]:
        promoted: list[Entry] = []
        for e in self.waiting():
            if limit is not None and len(promoted) >= limit:
                break
            if walk_ins is not None and self.is_walk_in(e) != walk_ins:
                continue
            table = self._take_table()
            if table is None:
                break
            e.table = table
            e.status = Status.PROMOTED
            promoted.append(e)
            self._log("promoted", matric=e.matric, table=table)
        return promoted

    def _compact(self) -> list[tuple[str, int, int]]:
        moves: list[tuple[str, int, int]] = []
        while self.freed:
            hole = heapq.heappop(self.freed)
            occupants = sorted(self.seated(), key=self._slot_of)
            if not occupants:
                break
            last = occupants[-1]
            if self._slot_of(last) < hole:
                continue
            frm = last.table
            last.table = self._slot_to_table[hole - 1]
            moves.append((last.matric, frm, last.table))
            self._log("moved", matric=last.matric, frm=frm, to=last.table)
        occupants = sorted(self.seated(), key=self._slot_of)
        self.next_table = (self._slot_of(occupants[-1]) + 1) if occupants else 1
        return moves

    def _assign_groups(self, kept: list[Entry]) -> tuple[dict[int, list[Entry]], int | None]:
        groups: dict[int, list[Entry]] = {}
        seed = None
        if self.group_size <= 1:
            return groups, seed

        if self.cfg.grouping.group_formation == "random_partition":
            seed = random.randrange(1_000_000)
            order = list(kept)
            random.Random(seed).shuffle(order)
        else:
            order = sorted(kept, key=lambda e: e.table)

        for i, e in enumerate(order):
            g = i // self.group_size + 1
            e.group = g
            groups.setdefault(g, []).append(e)
        return groups, seed

    def finalise(self) -> FinaliseResult:
        res = FinaliseResult(warnings=list(self.warnings))
        res.moves = self._compact()

        occupants = sorted(self.seated(), key=self._slot_of)
        n = len(occupants)
        keep_n = n
        if self.cfg.grouping.enforce_complete_groups and self.group_size > 1:
            keep_n = (n // self.group_size) * self.group_size

        res.kept = occupants[:keep_n]
        res.dismissed = occupants[keep_n:]
        for e in res.dismissed:
            e.status = Status.DISMISSED
            e.table = None
            e.group = None
            self._log("dismissed", matric=e.matric, why="incomplete group")

        res.groups, res.seed = self._assign_groups(res.kept)
        for i, e in enumerate(res.kept, 1):
            e.subject_id = f"S{i:02d}"

        if keep_n == 0 and n > 0:
            res.warnings.append(
                f"{n} students seated but group_size is {self.group_size}: "
                f"a whole group could not be formed, so NOBODY can run. "
                f"Lower group_size or promote more people, then finalise again.")
        if res.dismissed:
            res.warnings.append(
                f"{len(res.dismissed)} student(s) dismissed to keep whole groups "
                f"of {self.group_size}: {', '.join(e.matric for e in res.dismissed)}")

        self.next_table = keep_n + 1
        self.freed = []
        self.finalised = res
        self._log("finalised", kept=keep_n, dismissed=len(res.dismissed), seed=res.seed)
        return res

    def expected_not_arrived(self) -> list[Participant]:
        return [p for p in self.session_roster.confirmed if p.matric not in self.entries]

    def projected(self) -> tuple[int, int]:
        n = len(self.seated())
        if self.cfg.grouping.enforce_complete_groups and self.group_size > 1:
            keep = (n // self.group_size) * self.group_size
        else:
            keep = n
        return keep, n - keep

    def _next_physical(self) -> int | None:
        freed = [s for s in self.freed if self._in_room(s)]
        if freed:
            return self._slot_to_table[min(freed) - 1]
        target = next((s for s in range(self.next_table,
                                        len(self._slot_to_table) + 1)
                       if self._in_room(s)), None)
        return None if target is None else self._slot_to_table[target - 1]

    def counters(self) -> dict:
        keep, drop = self.projected()
        return {
            "session": self.session_number,
            "label": self.roster.label_for(self.session_number),
            "group_size": self.group_size,
            "total_tables": self.cfg.layout.total_tables,
            "seated": len(self.seated()),
            "waiting": len(self.waiting()),
            "release_mode": self.release_mode,
            "released": len(self.released()),
            "waiting_walk_ins": sum(1 for e in self.waiting() if self.is_walk_in(e)),
            "walk_ins_seated": sum(1 for e in self.seated() if self.is_walk_in(e)),
            "expected_not_arrived": len(self.expected_not_arrived()),
            "next_table": self._next_physical(),
            "seat_order": self.seat_order,
            "seat_seed": self.seat_seed,
            "projected_seated": keep,
            "projected_groups": keep // self.group_size if self.group_size > 1 else keep,
            "projected_dismissed": drop,
            "finalised": self.finalised is not None,
        }

    def table_map(self) -> list[dict]:
        by_table = {e.table: e for e in self.seated()}
        return [{"table": t,
                 "matric": by_table[t].matric if t in by_table else None,
                 "name": by_table[t].name if t in by_table else None,
                 "group": by_table[t].group if t in by_table else None,
                 "status": by_table[t].status.value if t in by_table else "empty"}
                for t in range(1, self.cfg.layout.total_tables + 1)]

    def check_invariants(self) -> list[str]:
        problems = []
        holders = [e for e in self.entries.values() if e.holds_table]
        tables = [e.table for e in holders]
        if len(tables) != len(set(tables)):
            problems.append(f"duplicate table numbers issued: {sorted(tables)}")
        slots = [self._slot_of(e) for e in holders]
        if set(slots) & set(self.freed):
            problems.append("a seat is both occupied and in the freed pool")
        covered = set(slots) | set(self.freed)
        if covered != set(range(1, self.next_table)):
            problems.append(f"seats {sorted(covered)} do not cover "
                            f"1..{self.next_table - 1} exactly")
        if any(s > len(self._slot_to_table) for s in slots):
            problems.append(f"a seat beyond the {len(self._slot_to_table)}-table "
                            f"room was issued")
        if any(e.table is not None and e.status != Status.SEATED
               and e.status != Status.PROMOTED for e in self.entries.values()):
            problems.append("a student without a seat is holding a table")
        return problems
