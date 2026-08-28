from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.config import SheetWritebackCfg
from app.store import Store

BATCH = 20
MAX_ATTEMPTS = 8
POLL_SECONDS = 2.0
MAX_BACKOFF = 60.0


@dataclass
class WriterStatus:
    running: bool = False
    sent: int = 0
    failed: int = 0
    parked: int = 0
    last_error: str = ""
    last_sent_at: str = ""
    backoff_until: float = 0.0
    history: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.history.append(f"{stamp}  {msg}")
        del self.history[:-20]


class SheetWriter:

    def __init__(self, store: Store, cfg: SheetWritebackCfg, post=None):
        self.store = store
        self.cfg = cfg
        self.status = WriterStatus()
        self._post = post or self._http_post
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _http_post(self, url: str, payload: dict, timeout: float = 10.0) -> dict:
        import requests

        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"non-JSON reply: {resp.text[:200]}") from None

    def drain_once(self) -> dict:
        if not self.cfg.enabled:
            return {"skipped": "write-back disabled"}
        if time.monotonic() < self.status.backoff_until:
            return {"skipped": "backing off",
                    "seconds_left": round(self.status.backoff_until - time.monotonic(), 1)}

        jobs = [j for j in self.store.pending_sheet_jobs(BATCH)
                if j["attempts"] < MAX_ATTEMPTS]
        if not jobs:
            parked = [j for j in self.store.pending_sheet_jobs(BATCH)
                      if j["attempts"] >= MAX_ATTEMPTS]
            self.status.parked = len(parked)
            return {"sent": 0, "pending": 0, "parked": len(parked)}

        rows = [{"id": j["id"], **json.loads(j["payload"])} for j in jobs]

        if not self.cfg.webapp_url:
            self.status.last_error = "sheet_writeback.enabled is true but webapp_url is empty"
            self.status.backoff_until = time.monotonic() + MAX_BACKOFF
            return {"error": self.status.last_error}

        payload = {"secret": self.cfg.shared_secret, "rows": rows}
        try:
            reply = self._post(self.cfg.webapp_url, payload)
        except Exception as exc:
            for j in jobs:
                self.store.mark_sheet_failed(j["id"], f"{exc.__class__.__name__}: {exc}")
            self.status.failed += len(jobs)
            self.status.last_error = f"{exc.__class__.__name__}: {exc}"
            attempts = max(j["attempts"] for j in jobs) + 1
            wait = min(MAX_BACKOFF, 2.0 ** attempts)
            self.status.backoff_until = time.monotonic() + wait
            self.status.note(f"{len(jobs)} row(s) failed ({exc.__class__.__name__}), "
                             f"retrying in {wait:.0f}s")
            return {"error": self.status.last_error, "retry_in": wait}

        if not reply.get("ok"):
            raw = str(reply)[:200]
            self.status.last_error = raw
            if "bad secret" in str(reply.get("error", "")).lower():
                self.status.last_error = (
                    "bad secret: the DEPLOYED Apps Script's SHARED_SECRET is not "
                    "sheet_writeback.shared_secret from config\\session.yaml. "
                    "Editing the script is not enough -- redeploy it: Deploy > "
                    "Manage deployments > pencil > Version: New version > Deploy. "
                    "Nothing is lost meanwhile; press Send now afterwards.")
            self.status.backoff_until = time.monotonic() + 10
            self.status.note(f"the script refused the batch: {raw}")
            for j in jobs:
                self.store.mark_sheet_failed(j["id"], self.status.last_error)
            return {"error": self.status.last_error}

        now = datetime.now().isoformat(timespec="seconds")
        for j in jobs:
            self.store.mark_sheet_sent(j["id"], now)
        self.status.sent += len(jobs)
        self.status.last_sent_at = now
        self.status.backoff_until = 0.0

        missing = reply.get("missing") or []
        if missing:
            self.status.note(f"NOT FOUND in the sheet: {', '.join(missing[:6])}")
        else:
            self.status.note(f"{len(jobs)} row(s) written")
        if reply.get("note"):
            self.status.note(str(reply["note"])[:200])
        return {"sent": len(jobs), "missing": missing,
                "walk_ins": reply.get("walk_ins", 0),
                "tables": reply.get("tables", 0), "note": reply.get("note", ""),
                "updated": reply.get("updated", len(jobs))}

    def retry_parked(self) -> int:
        n = self.store.reset_attempts()
        self.status.parked = 0
        self.status.backoff_until = 0.0
        if n:
            self.status.note(f"retrying {n} row(s) that had been parked")
        return n

    def _run(self) -> None:
        self.status.running = True
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.drain_once()
            except Exception as exc:
                self.status.last_error = f"drain crashed: {exc}"
                self.status.note(self.status.last_error)
        self.status.running = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sheet-writer")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def state(self) -> dict:
        left = max(0.0, self.status.backoff_until - time.monotonic())
        return {
            "enabled": self.cfg.enabled,
            "dry_run": self.cfg.dry_run,
            "configured": bool(self.cfg.webapp_url),
            "running": self.status.running,
            "queue_depth": self.store.queue_depth(),
            "sent": self.status.sent,
            "failed": self.status.failed,
            "parked": self.status.parked,
            "last_error": self.status.last_error,
            "last_sent_at": self.status.last_sent_at,
            "retry_in": round(left, 1) if left else 0,
            "history": self.status.history[-8:],
        }
