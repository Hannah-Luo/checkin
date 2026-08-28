from __future__ import annotations

import argparse
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.matcher import normalise
from app.service import CheckinService
from app.settings import SheetLinkError

STATIC = Path(__file__).resolve().parent / "static"

svc = CheckinService(config_path=os.environ.get("CHECKIN_CONFIG"),
                     db_path=os.environ.get("CHECKIN_DB"))


def _warm_engine() -> None:
    try:
        print(f"  OCR engine warm in {svc.warm_up():.1f}s", flush=True)
    except Exception as exc:
        print(f"  OCR warm-up failed ({exc.__class__.__name__}: {exc}). "
              f"The camera may be slow on its first frame; manual entry is "
              f"unaffected.", flush=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=_warm_engine, daemon=True).start()
    svc.sheets.start()
    if svc.start_roster_refresh():
        print(f"  roster re-read every {svc.cfg.roster.refresh_seconds:.0f}s "
              f"- correct a row in the sheet and the door picks it up", flush=True)
    yield
    svc.stop_roster_refresh()
    svc.sheets.stop()


app = FastAPI(title="NTU Lab Check-In", docs_url="/api/docs", lifespan=lifespan)


class CheckInBody(BaseModel):
    query: str
    method: str = "manual"
    name: str = ""


class ScanBody(BaseModel):
    lines: list[str]


class MatricBody(BaseModel):
    matric: str


class PromoteBody(BaseModel):
    limit: int | None = 1
    pool: str | None = None
    grow: bool = False


PROMOTE_POOLS = {"": None, "allocated": False, "walk_in": True}


class SessionBody(BaseModel):
    number: int


class ReleaseModeBody(BaseModel):
    on: bool


class SettingsBody(BaseModel):
    session: int | None = None
    room: int = 1
    group_size: int | None = None
    total_tables: int | None = None


class RoomBody(BaseModel):
    session: int
    room: int = 1


class DeskRoomBody(BaseModel):
    room: int | None = None


class RekeyBody(BaseModel):
    session: int
    room: int = 1
    to: int
    total_tables: int | None = None
    group_size: int | None = None


class SheetLinkBody(BaseModel):
    url: str


class WritebackBody(BaseModel):
    url: str = ""
    mode: str = "off"
    secret: str | None = None


@app.middleware("http")
async def always_revalidate(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in ("/", "/operator", "/admin"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/operator")


@app.get("/operator", include_in_schema=False)
def operator_page():
    return FileResponse(STATIC / "operator.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/api/state")
def api_state():
    return svc.state()


@app.get("/api/display")
def api_display():
    return svc.display_payload()


@app.get("/api/search")
def api_search(q: str, limit: int = 8):
    return {"query": q, "hits": svc.search(q, limit)}


@app.post("/api/checkin")
def api_checkin(body: CheckInBody):
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "empty query")

    key = normalise(query)
    known = svc.roster.by_matric(key) is not None or key in svc.alloc.entries
    if not known:
        hits = svc.search(query, limit=5)
        if len(hits) == 1:
            key = hits[0]["matric"]
        elif len(hits) > 1:
            return {"outcome": "ambiguous", "colour": "amber",
                    "reason": f"{len(hits)} students match {query!r}",
                    "candidates": hits}

    result = svc.check_in(key, method=body.method, name=body.name)
    return svc.result_dict(result)


@app.post("/api/scan")
def api_scan(body: ScanBody):
    return svc.check_in_scan(body.lines)


@app.post("/api/frame")
async def api_frame(request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty frame")
    return await run_in_threadpool(svc.process_frame, data)


@app.post("/api/scanner/reset")
def api_scanner_reset():
    svc.reset_scanner()
    return {"reset": True}


@app.get("/api/diagnostics")
async def api_diagnostics(frames: int = 5):
    return await run_in_threadpool(svc.self_test, frames)


def _flush_now() -> dict:
    retried = svc.sheets.retry_parked()
    result = svc.sheets.drain_once()
    return {**result, "retried": retried} if retried else result


@app.post("/api/sheet/flush")
async def api_sheet_flush():
    result = await run_in_threadpool(_flush_now)
    return {"result": result, "sheet": svc.sheets.state()}


@app.post("/api/sheet/resend")
async def api_sheet_resend():
    result = await run_in_threadpool(svc.resend_all)
    return {"result": result, "sheet": svc.sheets.state()}


@app.post("/api/undo")
def api_undo(body: MatricBody):
    result = svc.undo(body.matric)
    if result.entry is None:
        raise HTTPException(404, "no such check-in")
    return {"undone": svc.public_entry(result.entry)}


@app.post("/api/promote")
def api_promote(body: PromoteBody):
    if (body.pool or "") not in PROMOTE_POOLS:
        raise HTTPException(400, f"unknown pool {body.pool!r}")
    walk_ins = PROMOTE_POOLS[body.pool or ""]
    grown = svc.make_room_for_next(walk_ins) if body.grow else None
    promoted = svc.promote(body.limit, walk_ins=walk_ins)
    queue = [e for e in svc.alloc.waiting()
             if walk_ins is None or svc.alloc.is_walk_in(e) == walk_ins]
    return {"promoted": [svc.public_entry(e) for e in promoted],
            "grown": grown,
            "next_in_queue": svc.public_entry(queue[0]) if queue else None,
            "still_waiting": len(queue),
            "counters": svc.alloc.counters()}


@app.post("/api/release-mode")
def api_release_mode(body: ReleaseModeBody):
    return {"release_mode": svc.set_release_mode(body.on),
            "counters": svc.alloc.counters()}


@app.post("/api/finalise")
def api_finalise():
    return svc.finalise()


@app.post("/api/session")
def api_session(body: SessionBody):
    svc.set_session(body.number)
    return svc.state()


@app.post("/api/settings")
def api_settings(body: SettingsBody):
    number = svc.session if body.session is None else body.session
    problems = svc.update_room_settings(number, body.room, body.group_size,
                                        body.total_tables)
    return {"problems": problems, "state": svc.state()}


@app.post("/api/rooms/add")
def api_rooms_add():
    added = svc.add_room()
    return {**added, "state": svc.state()}


@app.post("/api/rooms/remove")
def api_rooms_remove(body: RoomBody):
    try:
        svc.remove_room(body.session, body.room)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"state": svc.state()}


@app.post("/api/rooms/desk")
def api_rooms_desk(body: DeskRoomBody):
    try:
        room = svc.set_desk_room(body.room)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"desk_room": room, "counters": svc.alloc.counters()}


@app.post("/api/rooms/rekey")
def api_rooms_rekey(body: RekeyBody):
    try:
        svc.rekey_room(body.session, body.room, body.to,
                       body.total_tables, body.group_size)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"state": svc.state()}


@app.post("/api/roster/reload")
async def api_roster_reload():
    r = await run_in_threadpool(svc.reload_roster)
    return {"source": r.source, "rows": len(r.participants),
            "roster": svc.roster_state(),
            "issues": [str(i) for i in r.issues]}


@app.post("/api/sheet/writeback")
def api_sheet_writeback(body: WritebackBody):
    try:
        wb = svc.set_writeback(body.url, body.secret, body.mode)
    except SheetLinkError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"writeback": wb, "sheet": svc.sheets.state()}


@app.post("/api/sheet/connect")
async def api_sheet_connect(body: SheetLinkBody):
    try:
        result = await run_in_threadpool(svc.connect_sheet, body.url)
    except SheetLinkError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"connected": result, "state": svc.state()}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="NTU lab check-in server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    print(f"  operator : http://{args.host}:{args.port}/operator", flush=True)
    print(f"  admin    : http://{args.host}:{args.port}/admin", flush=True)
    uvicorn.run("app.server:app" if args.reload else app,
                host=args.host, port=args.port, reload=args.reload,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
