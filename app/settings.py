from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "settings.json"


def settings_path() -> Path:
    return Path(os.environ.get("CHECKIN_SETTINGS") or DEFAULT_PATH)

WRITEBACK_MODES = ("off", "live")

_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_GID = re.compile(r"[#&?]gid=(\d+)")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class SheetLinkError(ValueError):
    pass


def parse_sheet_url(url: str) -> tuple[str, str]:
    text = (url or "").strip()
    if not text:
        raise SheetLinkError("paste the link to the Google Sheet of form responses")

    if "/forms/" in text or "forms.gle" in text:
        raise SheetLinkError(
            "that is the Form itself, which cannot be read as data. Open the "
            "form, go to Responses -> the green Sheets icon ('Link to Sheets'), "
            "then paste the address of the spreadsheet that opens.")

    m = _SHEET_ID.search(text)
    if m:
        gid = _GID.search(text)
        return m.group(1), (gid.group(1) if gid else "")

    if _BARE_ID.match(text):
        return text, ""

    raise SheetLinkError(
        "that does not look like a Google Sheets link. It should look like "
        "https://docs.google.com/spreadsheets/d/<long-id>/edit")


def sheet_edit_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else ""


@dataclass
class Writeback:
    url: str = ""
    secret: str = ""
    mode: str = "off"

    def as_dict(self) -> dict:
        return {"url": self.url, "mode": self.mode, "has_secret": bool(self.secret)}


@dataclass
class Settings:
    sheet_url: str = ""
    sheet_id: str = ""
    gid: str = ""
    rotate_variants: bool = False

    writeback: dict = field(default_factory=dict)

    @property
    def db_name(self) -> str:
        return f"checkin_{self.sheet_id[:12]}.db" if self.sheet_id else "checkin.db"

    def writeback_for(self, sheet_id: str) -> Writeback:
        raw = self.writeback.get(sheet_id or "") or {}
        return Writeback(url=str(raw.get("url") or ""),
                         secret=str(raw.get("secret") or ""),
                         mode=raw.get("mode") if raw.get("mode") in WRITEBACK_MODES else "off")

    def set_writeback_for(self, sheet_id: str, wb: Writeback) -> None:
        self.writeback[sheet_id or ""] = {"url": wb.url, "secret": wb.secret,
                                          "mode": wb.mode}

    def as_dict(self) -> dict:
        d = asdict(self)
        d["db_name"] = self.db_name
        d["connected"] = bool(self.sheet_id)
        d["sheet_link"] = sheet_edit_url(self.sheet_id)
        d["writeback"] = {k: Writeback(**v).as_dict() if isinstance(v, dict) else {}
                          for k, v in self.writeback.items()}
        return d


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else settings_path()
    if not p.exists():
        return Settings()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return Settings()
    known = {f for f in Settings.__dataclass_fields__}
    s = Settings(**{k: v for k, v in raw.items() if k in known and v is not None})
    if not isinstance(s.writeback, dict):
        s.writeback = {}
    return s


def save_settings(s: Settings, path: str | Path | None = None) -> Path:
    p = Path(path) if path else settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    return p
