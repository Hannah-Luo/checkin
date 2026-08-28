from __future__ import annotations

import copy
import heapq
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from app.allocation import (QUEUE_TIERS, Allocator, CheckInResult, Entry,
                            FinaliseResult, Status)
from app.config import Config, load_config
from app.matcher import best_match, match, normalise, roster_warnings
from app.roster import Roster, load_roster, mask_name
from app.settings import (WRITEBACK_MODES, SheetLinkError, Writeback,
                          load_settings, parse_sheet_url, save_settings,
                          sheet_edit_url)
from app.sheets import SheetWriter
from app.store import Store

GLOBAL = 0
SCAN_COOLDOWN_S = 8.0


def _masked(d: dict) -> dict:
    return {**d, "name": mask_name(d["name"])} if d.get("name") else d


def _name_key(s: str) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


class CheckinService:
    def __init__(self, config_path: str | Path | None = None,
                 db_path: str | Path | None = None, autoload: bool = True):
        self.cfg: Config = load_config(config_path or os.environ.get("CHECKIN_CONFIG"))
        self.store = Store(db_path or self.cfg.db_file)
        self._yaml_sheet_id = self.cfg.roster.sheet_id
        _wb = self.cfg.sheet_writeback
        self._yaml_writeback = Writeback(
            url=_wb.webapp_url, secret=_wb.shared_secret,
            mode="live" if _wb.enabled and not _wb.dry_run else "off")
        self._apply_saved_sheet()
        self._apply_saved_writeback()
        self.roster: Roster = load_roster(self.cfg.roster) if autoload else Roster([])

        active = self.store.get_meta(GLOBAL, "active_session")
        if active is not None:
            self.cfg.session.number = int(active)
        self._apply_saved_overrides()
        self._persist_overrides()

        self.alloc = self._new_allocator()
        self._persist_seating()
        self._flushed = 0
        self.last_display: dict = {"display": "", "colour": "idle", "seq": 0, "at": ""}

        self.sheets = SheetWriter(self.store, self.cfg.sheet_writeback)

        self._pipeline = None
        self._vision_lock = threading.Lock()
        self._last_scanned: str | None = None
        self._last_scan_at = 0.0
        self._names_cache: list[str] = []
        self._names_src: object = None

        self._roster_stop = threading.Event()
        self._roster_thread: threading.Thread | None = None
        self._roster_interval = float(self.cfg.roster.refresh_seconds)
        self._roster_status: dict = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "source": self.roster.source, "rows": len(self.roster.participants),
            "live": not any(i.code in ("using_cache", "sheet_unreachable", "no_roster")
                            for i in self.roster.issues),
            "error": "",
        }

        self._restore()

    @staticmethod
    def _overrides_onto(cfg: Config, saved: dict) -> None:
        if "room_tables" in saved:
            cfg.layout.total_tables = sum(int(t) for t in saved["room_tables"])
        elif "total_tables" in saved:
            cfg.layout.total_tables = int(saved["total_tables"])
        if "group_size" in saved:
            cfg.grouping.group_size = int(saved["group_size"])

    def _apply_saved_overrides(self) -> None:
        saved = self.store.get_meta(self.cfg.session.number, "overrides") or {}
        self._overrides_onto(self.cfg, saved)

    def _split_for(self, number: int) -> list[int]:
        saved = self.store.get_meta(number, "overrides") or {}
        if "room_tables" in saved:
            return [int(t) for t in saved["room_tables"]]
        if "total_tables" in saved:
            return [int(saved["total_tables"])]
        return [self.cfg.layout.total_tables]

    def _persist_overrides(self, split: list[int] | None = None) -> None:
        if split is None:
            split = self._split_for(self.session)
            if sum(split) != self.cfg.layout.total_tables:
                split = [self.cfg.layout.total_tables]
        self.store.set_meta(self.session, "overrides", {
            "room_tables": split,
            "group_size": self.cfg.grouping.group_size})

    def _seat_seed(self) -> int | None:
        v = self.store.get_meta(self.cfg.session.number, "seat_seed")
        return int(v) if v is not None else None

    def _seat_deck(self) -> tuple[list[int] | None, int]:
        deck = self.store.get_meta(self.cfg.session.number, "seat_deck")
        if not deck:
            return None, 0
        held = {e.table for e in self.store.load_entries(self.cfg.session.number)
                if e.holds_table}
        frozen = max((i for i, t in enumerate(deck, 1) if t in held), default=0)
        return [int(t) for t in deck], frozen

    def _new_allocator(self) -> Allocator:
        deck, frozen = self._seat_deck()
        return Allocator(self.cfg, self.roster, seat_seed=self._seat_seed(),
                         seat_deck=deck, seat_frozen=frozen)

    def _persist_seating(self) -> None:
        if self.alloc.seat_seed is not None:
            self.store.set_meta(self.session, "seat_seed", self.alloc.seat_seed)
        self.store.set_meta(self.session, "seat_deck", self.alloc.seat_deck)

    def _restore(self) -> None:
        self.alloc.release_mode = bool(
            self.store.get_meta(self.session, "release_mode", False))
        self.alloc.room = self._room_span(
            self.store.get_meta(self.session, "desk_room"))

        entries = self.store.load_entries(self.session)
        self._flushed = len(self.alloc.events)
        if not entries:
            return
        self.alloc.entries = {e.matric: e for e in entries}
        slots = sorted(self.alloc._slot_of(e) for e in entries if e.holds_table)
        self.alloc.next_table = (slots[-1] + 1) if slots else 1
        held = set(slots)
        self.alloc.freed = [s for s in range(1, self.alloc.next_table) if s not in held]
        heapq.heapify(self.alloc.freed)
        self.alloc._arrivals = max((e.arrival_seq for e in entries), default=0)

        if self.store.get_meta(self.session, "finalised_at"):
            kept = self.alloc.seated()
            groups: dict[int, list[Entry]] = {}
            for e in kept:
                if e.group:
                    groups.setdefault(e.group, []).append(e)
            self.alloc.finalised = FinaliseResult(
                kept=kept,
                dismissed=[e for e in entries if e.status == Status.DISMISSED],
                groups=groups,
                seed=self.store.get_meta(self.session, "group_seed"),
                warnings=["restored from a previous run of this session"])

    def _flush_events(self) -> None:
        new = self.alloc.events[self._flushed:]
        if new:
            self.store.log_events(self.session, new)
            self._flushed = len(self.alloc.events)

    def _persist(self, *entries: Entry) -> None:
        for e in entries:
            self.store.save_entry(self.session, e)
        self._flush_events()

    def _queue_showup(self, matric: str, show_up: int, walk_in="",
                      slot="", table="") -> None:
        self.store.enqueue_sheet(self.session, matric, {
            "matric": matric, "show_up": show_up, "walk_in": walk_in,
            "slot": slot, "table": table,
        }, datetime.now().isoformat(timespec="seconds"))

    def _queue_writeback(self, entry: Entry) -> None:
        if entry.status is Status.RELEASED:
            return
        walk_in = (self.session if entry.holds_table and self.alloc.is_walk_in(entry)
                   else "")
        table = entry.table if entry.holds_table else ""
        self._queue_showup(entry.matric, 1, walk_in, slot=walk_in, table=table)

    @property
    def session(self) -> int:
        return self.cfg.session.number

    def display_payload(self) -> dict:
        d = dict(self.last_display)
        age = None
        if d.get("at"):
            try:
                age = (datetime.now() - datetime.fromisoformat(d["at"])).total_seconds()
            except ValueError:
                age = None
        d["age"] = age
        return d

    def _show(self, result: CheckInResult) -> None:
        self.last_display = {
            "display": result.display,
            "colour": result.colour,
            "outcome": result.outcome,
            "seq": self.last_display["seq"] + 1,
            "at": datetime.now().isoformat(timespec="seconds"),
        }

    def check_in(self, query: str, *, method: str = "manual",
                 name: str = "", confidence: float | None = None,
                 force_walk_in: bool = False) -> CheckInResult:
        result = self.alloc.check_in(query, method=method, name=name,
                                     confidence=confidence,
                                     force_walk_in=force_walk_in)
        if result.entry is not None and result.outcome != "refused":
            self._persist(result.entry)
            if result.outcome != "already":
                self._queue_writeback(result.entry)
        else:
            self._flush_events()
        self._show(result)
        return result

    def scan_pool(self) -> list[str]:
        return (list(self.alloc.session_roster.matrics)
                + [p.matric for p in self.roster.participants
                   if p.session is None])

    def wide_match(self, token: str):
        return match(token, [p.matric for p in self.roster.participants])

    def walk_in_from_scan(self, token: str, wide=None) -> CheckInResult:
        wide = self.wide_match(token) if wide is None else wide
        if wide.accepted:
            return self.check_in(wide.best, method="ocr",
                                 confidence=1.0 - wide.best_distance,
                                 force_walk_in=True)
        return self.check_in(token, method="ocr", force_walk_in=True)

    def check_in_scan(self, ocr_lines: list[str], method: str = "ocr") -> dict:
        m = best_match(ocr_lines, self.scan_pool())
        if m is None:
            return {"identified": False, "reason": "no ID-shaped text in frame"}
        if not m.accepted:
            return {"identified": False, "reason": m.reason,
                    "candidates": [{"matric": r, "distance": d} for r, d in m.ranked]}
        result = self.check_in(m.best, method=method, confidence=1.0 - m.best_distance)
        return {"identified": True, "matric": m.best, **self.result_dict(result)}

    @property
    def pipeline(self):
        if self._pipeline is None:
            from app.vision import Pipeline
            self._pipeline = Pipeline()
        return self._pipeline

    def process_frame(self, jpeg: bytes) -> dict:
        import cv2
        import numpy as np

        with self._vision_lock:
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return {"error": "could not decode the frame"}

            r = self.pipeline.process(frame, self.scan_pool())
            out = r.as_dict()
            out["texts"] = self.redact_texts(out["texts"])

            wide = None
            if r.match and not r.match.accepted:
                wide = self.wide_match(r.match.token)
                elsewhere = (self.roster.by_matric(wide.best) if wide.accepted
                             else None)
                if elsewhere and elsewhere.session != self.session:
                    out["elsewhere"] = {"matric": elsewhere.matric,
                                        "name": mask_name(elsewhere.name),
                                        "session": elsewhere.session}

            if r.stable:
                now = time.monotonic()
                repeat = (r.stable == self._last_scanned
                          and now - self._last_scan_at < SCAN_COOLDOWN_S)
                if not repeat:
                    conf = max(0.0, 1.0 - (r.match.best_distance if r.match else 1.0))
                    result = self.check_in(r.stable, method="ocr", confidence=conf)
                    out["checkin"] = self.result_dict(result)
                    self._last_scanned, self._last_scan_at = r.stable, now
                    self.pipeline.reset()
                else:
                    out["cooldown"] = round(SCAN_COOLDOWN_S - (now - self._last_scan_at), 1)
            elif r.unmatched:
                now = time.monotonic()
                repeat = (r.unmatched == self._last_scanned
                          and now - self._last_scan_at < SCAN_COOLDOWN_S)
                if not repeat:
                    result = self.walk_in_from_scan(r.unmatched, wide)
                    out["checkin"] = self.result_dict(result)
                    self._last_scanned, self._last_scan_at = r.unmatched, now
                    self.pipeline.reset()
                else:
                    out["cooldown"] = round(SCAN_COOLDOWN_S - (now - self._last_scan_at), 1)
            return out

    def reset_scanner(self) -> None:
        if self._pipeline is not None:
            self._pipeline.reset()
        self._last_scanned, self._last_scan_at = None, 0.0

    def warm_up(self) -> float:
        from tools.make_test_card import render_card

        t = time.perf_counter()
        self.pipeline.engine(render_card("U2520575H", "WARM UP"))
        return time.perf_counter() - t

    def self_test(self, frames: int = 5) -> dict:
        import statistics

        import cv2

        from app.vision import ENGINE_SETTINGS, Pipeline
        from tools.make_test_card import render_card

        frame = render_card("U2520575H", "SELF TEST")
        pipe = Pipeline()
        pipe.engine(frame)
        times = []
        for _ in range(frames):
            r = pipe.process(frame, ["U2520575H"])
            times.append(r.ocr_ms)
        median = statistics.median(times)
        return {
            "median_ms": round(median),
            "fps": round(1000 / median, 1) if median else None,
            "samples": [round(t) for t in times],
            "cv2_threads": cv2.getNumThreads(),
            "engine": ENGINE_SETTINGS,
            "healthy": median < 800,
            "advice": ("ok" if median < 800 else
                       "OCR is far slower than the measured 250 ms. Check that "
                       "nothing heavy is running, that the laptop is on mains "
                       "power, and that cv2.setNumThreads(1) is still in "
                       "app\\vision.py. Manual entry is unaffected."),
        }

    def undo(self, matric: str) -> CheckInResult:
        result = self.alloc.undo(matric)
        if result.entry is not None:
            key = normalise(matric)
            if result.entry.sheet_show_up:
                self._queue_showup(key, 0)
            self.store.delete_entry(self.session, key)
        self._flush_events()
        self.last_display = {"display": "", "colour": "idle",
                             "seq": self.last_display["seq"] + 1, "at": ""}
        return result

    def set_release_mode(self, on: bool) -> bool:
        on = self.alloc.set_release_mode(on)
        self.store.set_meta(self.session, "release_mode", on)
        self._flush_events()
        return on

    def _room_spans(self, number: int | None = None) -> list[tuple[int, int]]:
        spans, start = [], 1
        for size in self._split_for(self.session if number is None else number):
            spans.append((start, start + size - 1))
            start += size
        return spans

    def _room_span(self, room: int | None) -> tuple[int, int] | None:
        spans = self._room_spans()
        if room is None or len(spans) < 2 or not 1 <= int(room) <= len(spans):
            return None
        return spans[int(room) - 1]

    def desk_room(self) -> int | None:
        stored = self.store.get_meta(self.session, "desk_room")
        return None if self._room_span(stored) is None else int(stored)

    def set_desk_room(self, room: int | None) -> int | None:
        span = self._room_span(room)
        if room is not None and span is None:
            raise ValueError(f"session {self.session} has no room {room}")
        self.alloc.set_room(span)
        self.store.set_meta(self.session, "desk_room",
                            None if span is None else int(room))
        self._flush_events()
        return None if span is None else int(room)

    def make_room_for_next(self, walk_ins: bool | None = None) -> dict | None:
        pool = [e for e in self.alloc.waiting()
                if walk_ins is None or self.alloc.is_walk_in(e) == walk_ins]
        if not pool or self.alloc._next_physical() is not None:
            return None
        g = max(1, self.cfg.grouping.group_size)
        split = self._split_for(self.session)
        if sum(split) != self.cfg.layout.total_tables:
            split = [self.cfg.layout.total_tables]
        room = self.desk_room()
        if room is not None and room != len(split):
            return None
        before = sum(split)
        split = split[:-1] + [split[-1] + g]
        self._commit_layout(self.session, split,
                            self.cfg.grouping.group_size, self.alloc)
        return {"added": g, "first": before + 1, "last": before + g,
                "room": len(split)}

    def promote(self, limit: int | None = None,
                walk_ins: bool | None = None) -> list[Entry]:
        promoted = self.alloc.promote(limit, walk_ins=walk_ins)
        self._persist(*promoted)
        for e in promoted:
            self._queue_writeback(e)
        return promoted

    def resend_all(self) -> dict:
        if not self.cfg.sheet_writeback.enabled:
            return {"queued": 0,
                    "error": "write-back is off in config\\session.yaml"}
        entries = sorted(self.alloc.entries.values(), key=lambda e: e.arrival_seq)
        for e in entries:
            self._queue_writeback(e)
        return {"queued": len(entries)}

    def finalise(self) -> dict:
        res = self.alloc.finalise()
        self._persist(*self.alloc.entries.values())
        self.store.set_meta(self.session, "finalised_at",
                            datetime.now().isoformat(timespec="seconds"))
        self.store.set_meta(self.session, "group_seed", res.seed)
        moved = {m for m, _, _ in res.moves}
        for e in res.kept:
            if e.matric in moved:
                self._queue_writeback(e)
        for e in res.dismissed:
            self._queue_writeback(e)
        return {
            "kept": [_masked(e.as_dict()) for e in res.kept],
            "dismissed": [_masked(e.as_dict()) for e in res.dismissed],
            "moves": [{"matric": m, "from": a, "to": b} for m, a, b in res.moves],
            "groups": {str(g): [e.matric for e in members]
                       for g, members in res.groups.items()},
            "seed": res.seed,
            "warnings": res.warnings,
        }

    def set_session(self, number: int) -> None:
        number = int(number)
        rooms = self.rooms_list()
        if number not in rooms:
            rooms[rooms.index(self.session)] = number
            self._save_rooms(rooms)
        self.cfg.session.number = number
        self.store.set_meta(GLOBAL, "active_session", number)
        self._apply_saved_overrides()
        self._persist_overrides()
        self.alloc = self._new_allocator()
        self._persist_seating()
        self._flushed = 0
        self.reset_scanner()
        self._restore()

    def _commit_layout(self, number: int, split: list[int], group_size: int,
                       prior: Allocator) -> None:
        total = sum(split)
        if number == self.session:
            prior = self.alloc
            self.cfg.layout.total_tables = total
            self.cfg.grouping.group_size = group_size
            self._persist_overrides(split)
            self.alloc = Allocator(self.cfg, self.roster,
                                   seat_seed=prior.seat_seed,
                                   seat_deck=prior.seat_deck,
                                   seat_frozen=prior.frozen_prefix())
            self.alloc.entries = prior.entries
            self.alloc.events = prior.events
            self.alloc.next_table = prior.next_table
            self.alloc.freed = prior.freed
            self.alloc._arrivals = prior._arrivals
            self.alloc.finalised = prior.finalised
            self.alloc.release_mode = prior.release_mode
            self.alloc.room = self._room_span(
                self.store.get_meta(self.session, "desk_room"))
            self._persist_seating()
            return
        cfg = prior.cfg
        cfg.layout.total_tables = total
        cfg.grouping.group_size = group_size
        rebuilt = Allocator(cfg, self.roster, seat_seed=prior.seat_seed,
                            seat_deck=prior.seat_deck,
                            seat_frozen=prior.frozen_prefix())
        self.store.set_meta(number, "overrides", {
            "room_tables": split, "group_size": group_size})
        if rebuilt.seat_seed is not None:
            self.store.set_meta(number, "seat_seed", rebuilt.seat_seed)
        self.store.set_meta(number, "seat_deck", rebuilt.seat_deck)

    @staticmethod
    def _room_problems(prior: Allocator, want_total: int) -> list[str]:
        lost = prior.dealt_above(want_total)
        if not lost:
            return []
        occupied = prior.occupied_above(want_total)
        return [
            f"cannot drop to {want_total} tables: "
            + (f"table(s) {', '.join(str(t) for t in occupied)} are occupied"
               if occupied else
               f"table(s) {', '.join(str(t) for t in lost)} have already "
               f"been dealt from this session's seat order")]

    def _split_problems(self, prior: Allocator, old_split: list[int],
                        new_split: list[int]) -> list[str]:
        total = sum(new_split)
        problems = self._room_problems(prior, total)

        def room_of(split: list[int]) -> dict[int, int]:
            out, start = {}, 1
            for i, size in enumerate(split):
                for t in range(start, start + size):
                    out[t] = i
                start += size
            return out

        old_rooms, new_rooms = room_of(old_split), room_of(new_split)
        dealt = prior.seat_deck[:prior.frozen_prefix()]
        moved = sorted(t for t in dealt
                       if t <= total and old_rooms.get(t) != new_rooms.get(t))
        if moved:
            problems.append(
                f"cannot re-divide the rooms: table(s) "
                f"{', '.join(str(t) for t in moved)} are already dealt and "
                f"would end up in a different room")
        return problems

    def rooms_list(self) -> list[int]:
        rooms = [int(r) for r in (self.store.get_meta(GLOBAL, "rooms") or [])]
        if self.session not in rooms:
            rooms.insert(0, self.session)
        return rooms

    def _save_rooms(self, rooms: list[int]) -> None:
        self.store.set_meta(GLOBAL, "rooms", rooms)

    def add_room(self) -> dict:
        split = self._split_for(self.session)
        if sum(split) != self.cfg.layout.total_tables:
            split = [self.cfg.layout.total_tables]
        split = split + [split[-1]]
        self._commit_layout(self.session, split,
                            self.cfg.grouping.group_size, self.alloc)
        return {"added": self.session, "room": len(split),
                "rooms": self.rooms_list()}

    def _pin_layout(self, number: int) -> None:
        if self.store.get_meta(number, "overrides") is None:
            self.store.set_meta(number, "overrides", {
                "room_tables": [self.cfg.layout.total_tables],
                "group_size": self.cfg.grouping.group_size})

    def remove_room(self, number: int, room: int = 1) -> None:
        number, room = int(number), int(room)
        split = self._split_for(number)
        if len(split) > 1:
            if not 1 <= room <= len(split):
                raise ValueError(f"session {number} has no room {room}")
            new_split = split[:room - 1] + split[room:]
            prior = self._allocator_for(number)
            problems = self._split_problems(prior, split, new_split)
            if problems:
                raise ValueError(" ".join(problems))
            self._commit_layout(number, new_split,
                                prior.cfg.grouping.group_size, prior)
            return
        if number == self.session:
            raise ValueError("the desk is in this room -- move it to another "
                             "room first")
        self._save_rooms([r for r in self.rooms_list() if r != number])

    def rekey_room(self, number: int, room: int, to: int,
                   tables: int | None = None,
                   group_size: int | None = None) -> None:
        number, room, to = int(number), int(room), int(to)
        if to == number:
            return
        rooms = self.rooms_list()
        if number not in rooms:
            raise ValueError(f"no block is showing session {number}")
        split = self._split_for(number)
        if not 1 <= room <= len(split):
            raise ValueError(f"session {number} has no room {room}")
        detach = len(split) > 1
        if not detach and number == self.session:
            raise ValueError("this is the desk's only room; its session box "
                             "moves the desk instead")
        size = max(1, int(tables)) if tables is not None else split[room - 1]

        if detach:
            new_split = split[:room - 1] + split[room:]
            prior = self._allocator_for(number)
            problems = self._split_problems(prior, split, new_split)
            if problems:
                raise ValueError(" ".join(problems))
            self._commit_layout(number, new_split,
                                prior.cfg.grouping.group_size, prior)
        else:
            self._save_rooms([r for r in rooms if r != number])

        rooms = self.rooms_list()
        if to in rooms:
            to_split = self._split_for(to) + [size]
            prior_to = self._allocator_for(to)
            group = (prior_to.cfg.grouping.group_size if group_size is None
                     else max(1, int(group_size)))
            self._commit_layout(to, to_split, group, prior_to)
        else:
            self._save_rooms(rooms + [to])
            self._pin_layout(to)

    def _allocator_for(self, number: int) -> Allocator:
        if number == self.session:
            return self.alloc
        cfg = copy.deepcopy(self.cfg)
        cfg.session.number = number
        self._overrides_onto(cfg, self.store.get_meta(number, "overrides") or {})
        seed = self.store.get_meta(number, "seat_seed")
        deck = self.store.get_meta(number, "seat_deck")
        entries = self.store.load_entries(number)
        frozen = 0
        if deck:
            deck = [int(t) for t in deck]
            held = {e.table for e in entries if e.holds_table}
            frozen = max((i for i, t in enumerate(deck, 1) if t in held),
                         default=0)
        alloc = Allocator(cfg, self.roster,
                          seat_seed=int(seed) if seed is not None else None,
                          seat_deck=deck or None, seat_frozen=frozen)
        alloc.entries = {e.matric: e for e in entries}
        return alloc

    def update_room_settings(self, number: int, room: int = 1,
                             group_size: int | None = None,
                             total_tables: int | None = None) -> list[str]:
        number, room = int(number), int(room)
        split = self._split_for(number)
        if not 1 <= room <= len(split):
            return [f"session {number} has no room {room}"]
        new_split = list(split)
        if total_tables is not None:
            new_split[room - 1] = max(1, int(total_tables))
        prior = self._allocator_for(number)
        want_group = (prior.cfg.grouping.group_size if group_size is None
                      else max(1, int(group_size)))
        problems = self._split_problems(prior, split, new_split)
        if problems:
            return problems
        self._commit_layout(number, new_split, want_group, prior)
        return []

    def rooms_state(self) -> list[dict]:
        out = []
        for n in self.rooms_list():
            a = self._allocator_for(n)
            split = self._split_for(n)
            if sum(split) != a.cfg.layout.total_tables:
                split = [a.cfg.layout.total_tables]
            full = a.table_map()
            group = a.cfg.grouping.group_size
            start = 1
            for idx, size in enumerate(split):
                cells = full[start - 1:start - 1 + size]
                out.append({
                    "session": n,
                    "room": idx + 1,
                    "rooms_in_session": len(split),
                    "start": start,
                    "end": start + size - 1,
                    "starts_mid_group": bool(idx and group > 1
                                             and (start - 1) % group),
                    "label": self.roster.label_for(n),
                    "active": n == self.session,
                    "settings": {"total_tables": size, "group_size": group},
                    "seated": sum(1 for c in cells if c["matric"]),
                    "table_map": [_masked(c) for c in cells],
                })
                start += size
        return out

    def _adopt_roster(self, fresh: Roster, live: bool) -> None:
        self.roster = fresh
        self.alloc.roster = fresh
        self._roster_status = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "source": fresh.source, "rows": len(fresh.participants),
            "live": live, "error": "" if live else self._roster_status.get("error", ""),
        }

    def refresh_roster(self, force: bool = False) -> dict:
        fresh = load_roster(self.cfg.roster)
        stale = {i.code for i in fresh.issues} & {"using_cache", "sheet_unreachable",
                                                 "no_roster"}
        live = not stale
        if not force and self.roster.participants and (stale or not fresh.participants):
            why = (f"{'; '.join(sorted(stale))}" if stale else "the sheet came back empty")
            self._roster_status["error"] = f"kept the roster already loaded ({why})"
            self._roster_status["checked_at"] = datetime.now().isoformat(timespec="seconds")
            return {**self._roster_status, "adopted": False}
        self._adopt_roster(fresh, live)
        self._roster_status["checked_at"] = self._roster_status["at"]
        return {**self._roster_status, "adopted": True,
                "issues": [str(i) for i in fresh.issues]}

    def reload_roster(self) -> Roster:
        self.refresh_roster(force=True)
        return self.roster

    def _apply_saved_sheet(self) -> None:
        if self.cfg.roster.csv_path:
            return
        saved = load_settings()
        if saved.sheet_id:
            self.cfg.roster.sheet_id = saved.sheet_id
            self.cfg.roster.gid = saved.gid
            self.cfg.roster.csv_path = ""

    def sheet_source(self) -> dict:
        saved = load_settings()
        return {"sheet_id": self.cfg.roster.sheet_id,
                "gid": self.cfg.roster.gid,
                "link": sheet_edit_url(self.cfg.roster.sheet_id),
                "from_admin": bool(saved.sheet_id),
                "pasted": saved.sheet_url}

    def _apply_saved_writeback(self) -> None:
        saved = load_settings()
        yaml_id = self._yaml_sheet_id
        if yaml_id not in saved.writeback and self._yaml_writeback.url:
            saved.set_writeback_for(yaml_id, self._yaml_writeback)
            save_settings(saved)
        self._use_writeback(saved.writeback_for(self.cfg.roster.sheet_id))

    def _use_writeback(self, wb: Writeback) -> None:
        cfg = self.cfg.sheet_writeback
        cfg.webapp_url = wb.url
        cfg.shared_secret = wb.secret
        cfg.enabled = wb.mode == "live" and bool(wb.url)
        cfg.dry_run = False

    def set_writeback(self, url: str = "", secret: str | None = None,
                      mode: str = "off") -> dict:
        mode = mode if mode in WRITEBACK_MODES else "off"
        url = (url or "").strip()
        if url and not url.startswith("https://"):
            raise SheetLinkError(
                "that is not an Apps Script Web App address. Deploy the script "
                "as a Web App and paste the URL ending in /exec.")
        if mode != "off" and not url:
            raise SheetLinkError("paste the /exec URL before switching the "
                                 "write-back on.")

        saved = load_settings()
        sheet_id = self.cfg.roster.sheet_id
        current = saved.writeback_for(sheet_id)
        wb = Writeback(url=url,
                       secret=current.secret if secret is None else secret.strip(),
                       mode=mode)
        if wb.mode != "off" and not wb.secret:
            raise SheetLinkError("the script's SHARED_SECRET is not set here yet "
                                 "-- type it in before switching the write-back on.")
        saved.set_writeback_for(sheet_id, wb)
        save_settings(saved)
        self._use_writeback(wb)
        return self.writeback_state()

    def writeback_state(self) -> dict:
        saved = load_settings()
        sheet_id = self.cfg.roster.sheet_id
        wb = saved.writeback_for(sheet_id)
        return {**wb.as_dict(),
                "sheet_id": sheet_id,
                "sheet_link": sheet_edit_url(sheet_id),
                "known_sheets": len([k for k in saved.writeback if k])}

    def connect_sheet(self, url: str) -> dict:
        sheet_id, gid = parse_sheet_url(url)
        before = (self.cfg.roster.sheet_id, self.cfg.roster.gid,
                  self.cfg.roster.csv_path)
        self.cfg.roster.sheet_id, self.cfg.roster.gid = sheet_id, gid
        self.cfg.roster.csv_path = ""
        fresh = load_roster(self.cfg.roster)
        if not fresh.participants:
            (self.cfg.roster.sheet_id, self.cfg.roster.gid,
             self.cfg.roster.csv_path) = before
            reason = "; ".join(str(i) for i in fresh.issues if i.level != "info")
            raise SheetLinkError(
                f"that sheet gave no usable rows, so nothing was changed. {reason}")

        saved = load_settings()
        saved.sheet_url, saved.sheet_id, saved.gid = url.strip(), sheet_id, gid
        save_settings(saved)
        self._adopt_roster(fresh, live=True)
        self._apply_saved_writeback()
        return {**self.sheet_source(), "rows": len(fresh.participants),
                "sessions": self.roster.sessions(),
                "writeback": self.writeback_state()}

    def _refresh_loop(self, interval: float) -> None:
        while not self._roster_stop.wait(interval):
            try:
                self.refresh_roster()
            except Exception as exc:
                self._roster_status["error"] = f"refresh crashed: {exc}"

    def start_roster_refresh(self, interval: float | None = None) -> bool:
        every = self.cfg.roster.refresh_seconds if interval is None else interval
        if every <= 0 or (self._roster_thread and self._roster_thread.is_alive()):
            return False
        self._roster_interval = float(every)
        self._roster_stop.clear()
        self._roster_thread = threading.Thread(
            target=self._refresh_loop, args=(float(every),), daemon=True,
            name="roster-refresh")
        self._roster_thread.start()
        return True

    def stop_roster_refresh(self, timeout: float = 3.0) -> None:
        self._roster_stop.set()
        if self._roster_thread:
            self._roster_thread.join(timeout)

    def roster_state(self) -> dict:
        return {**self._roster_status,
                "auto": bool(self._roster_thread and self._roster_thread.is_alive()),
                "every": self._roster_interval}

    def reset_session(self) -> None:
        split = self._split_for(self.session)
        self.store.clear_session(self.session)
        self._persist_overrides(split)
        self.alloc = self._new_allocator()
        self._persist_seating()
        self._flushed = 0
        self.reset_scanner()
        self.last_display = {"display": "", "colour": "idle", "seq": 0, "at": ""}

    def public_entry(self, e: Entry) -> dict:
        return _masked(e.as_dict())

    def _roster_names(self) -> list[str]:
        if self._names_src is not self.roster:
            self._names_cache = [k for k in (_name_key(p.name)
                                             for p in self.roster.participants) if k]
            self._names_src = self.roster
        return self._names_cache

    def redact_texts(self, texts: list[str]) -> list[str]:
        names = self._roster_names()
        out = []
        for t in texts:
            u = _name_key(t)
            hit = bool(u) and any(len(n) >= 6 and (n in u or (len(u) >= 6 and u in n))
                                  for n in names)
            out.append("(name hidden)" if hit else t)
        return out

    def result_dict(self, r: CheckInResult) -> dict:
        return {
            "outcome": r.outcome, "reason": r.reason, "colour": r.colour,
            "display": r.display, "wrong_session": r.wrong_session,
            "in_roster": r.in_roster, "waiting_position": r.waiting_position,
            "already_released": r.already_released,
            "entry": _masked(r.entry.as_dict()) if r.entry else None,
            "masked": (r.entry.matric[:4] + "•" * max(0, len(r.entry.matric) - 5)
                       + r.entry.matric[-1]) if r.entry else "",
        }

    def search(self, q: str, limit: int = 8) -> list[dict]:
        hits = self.roster.find(q, session=self.session, limit=limit)
        out = []
        for p in hits:
            e = self.alloc.entries.get(p.matric)
            d = p.as_dict()
            d["checked_in"] = e is not None
            d["table"] = e.table if e else None
            d["status"] = e.status.value if e else None
            out.append(_masked(d))
        return out

    def state(self) -> dict:
        sr = self.alloc.session_roster
        return {
            "counters": self.alloc.counters(),
            "session": self.session,
            "operator": self.cfg.session.operator,
            "sessions_available": self.roster.sessions(),
            "roster": {
                **self.roster_state(),
                "sheet": self.sheet_source(),
                "source": self.roster.source,
                "total": len(sr.participants),
                "confirmed": len(sr.confirmed),
                "unconfirmed": len(sr.unconfirmed),
                "errors": [str(i) for i in self.roster.issues if i.level == "error"],
                "warnings": [str(i) for i in self.roster.issues if i.level == "warn"],
                "confusable": [{"a": a, "b": b, "distance": d}
                               for a, b, d in roster_warnings(self.scan_pool())],
            },
            "seated": [{**_masked(e.as_dict()),
                        "walk_in": self.alloc.is_walk_in(e)}
                       for e in self.alloc.seated()],
            "waiting": [{**_masked(e.as_dict()), "position": i,
                         "walk_in": self.alloc.is_walk_in(e),
                         "tier": QUEUE_TIERS[self.alloc.queue_rank(e)]}
                        for i, e in enumerate(self.alloc.waiting(), 1)],
            "released": [{**_masked(e.as_dict()), "show_up": e.sheet_show_up,
                          "registration": self.alloc.registration(e)}
                         for e in self.alloc.released()],
            "not_arrived": [_masked(p.as_dict()) for p in self.alloc.expected_not_arrived()],
            "table_map": [_masked(c) for c in self.alloc.table_map()],
            "rooms": self.rooms_state(),
            "desk_rooms": [{"room": i + 1, "start": lo, "end": hi}
                           for i, (lo, hi) in enumerate(self._room_spans())],
            "desk_room": self.desk_room(),
            "settings": {
                "group_size": self.cfg.grouping.group_size,
                "total_tables": self.cfg.layout.total_tables,
                "seat_order": self.cfg.layout.seat_order,
                "seat_seed": self.alloc.seat_seed,
                "group_formation": self.cfg.grouping.group_formation,
                "enforce_complete_groups": self.cfg.grouping.enforce_complete_groups,
                "allow_wrong_session": self.cfg.waitlist.allow_wrong_session,
                "allow_not_in_roster": self.cfg.waitlist.allow_not_in_roster,
                "start_time": self.cfg.session.start_time,
            },
            "sheet": self.sheets.state(),
            "writeback": self.writeback_state(),
            "display": self.display_payload(),
            "invariants": self.alloc.check_invariants(),
        }


if __name__ == "__main__":
    svc = CheckinService()
    print(json.dumps(svc.state()["counters"], indent=2))
