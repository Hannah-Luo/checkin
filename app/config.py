from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "session.yaml"


def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


@dataclass
class SessionCfg:
    number: int = 1
    start_time: str = "10:00"
    operator: str = "operator"


@dataclass
class LayoutCfg:
    total_tables: int = 29
    seat_order: str = "sequential"


@dataclass
class GroupingCfg:
    group_size: int = 1
    enforce_complete_groups: bool = True
    group_formation: str = "consecutive"


@dataclass
class WaitlistCfg:
    allow_wrong_session: bool = True
    allow_not_in_roster: bool = False
    block_repeat_values: list[str] = field(default_factory=lambda: ["2"])


@dataclass
class RosterCfg:
    sheet_id: str = ""
    gid: str = ""
    csv_path: str = ""
    cache_path: str = "data/roster_cache.csv"
    timeout_seconds: float = 10.0
    refresh_seconds: float = 60.0
    confirmed_true_values: list[str] = field(
        default_factory=lambda: ["1", "yes", "y", "true", "confirmed"])

    @property
    def csv_url(self) -> str:
        if not self.sheet_id:
            return ""
        url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/gviz/tq?tqx=out:csv"
        if self.gid:
            url += f"&gid={self.gid}"
        return url


@dataclass
class SheetWritebackCfg:
    enabled: bool = False
    dry_run: bool = True
    webapp_url: str = ""
    shared_secret: str = ""


@dataclass
class StorageCfg:
    db_path: str = "data/checkin.db"


@dataclass
class Config:
    session: SessionCfg = field(default_factory=SessionCfg)
    layout: LayoutCfg = field(default_factory=LayoutCfg)
    grouping: GroupingCfg = field(default_factory=GroupingCfg)
    waitlist: WaitlistCfg = field(default_factory=WaitlistCfg)
    roster: RosterCfg = field(default_factory=RosterCfg)
    sheet_writeback: SheetWritebackCfg = field(default_factory=SheetWritebackCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    path: Path | None = None

    @property
    def db_file(self) -> Path:
        return _resolve(self.storage.db_path)


def _fill(cls, raw):
    if not isinstance(raw, dict):
        return cls()
    known = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in raw.items() if k in known and v is not None})


_ENV_OVERRIDES = (
    ("CHECKIN_SHEET_ID", "roster", "sheet_id"),
    ("CHECKIN_SHEET_GID", "roster", "gid"),
    ("CHECKIN_WRITEBACK_URL", "sheet_writeback", "webapp_url"),
    ("CHECKIN_WRITEBACK_SECRET", "sheet_writeback", "shared_secret"),
)


def _apply_env(cfg: Config) -> Config:
    for name, block, key in _ENV_OVERRIDES:
        value = (os.environ.get(name) or "").strip()
        if value:
            setattr(getattr(cfg, block), key, value)
    return cfg


def _apply_saved_sheet(cfg: Config) -> Config:
    if cfg.roster.sheet_id or cfg.roster.csv_path:
        return cfg
    from app.settings import load_settings

    saved = load_settings()
    if saved.sheet_id:
        cfg.roster.sheet_id = saved.sheet_id
        cfg.roster.gid = saved.gid
    return cfg


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _apply_saved_sheet(_apply_env(Config(
        session=_fill(SessionCfg, raw.get("session")),
        layout=_fill(LayoutCfg, raw.get("layout")),
        grouping=_fill(GroupingCfg, raw.get("grouping")),
        waitlist=_fill(WaitlistCfg, raw.get("waitlist")),
        roster=_fill(RosterCfg, raw.get("roster")),
        sheet_writeback=_fill(SheetWritebackCfg, raw.get("sheet_writeback")),
        storage=_fill(StorageCfg, raw.get("storage")),
        path=path if path.exists() else None,
    )))


def resolve_path(p: str | Path) -> Path:
    return _resolve(p)
