from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.config import load_config, resolve_path
from app.store import Store

TABLES = ("entries", "events", "meta", "sheet_queue")


def _counts(store: Store, session: int | None) -> dict[str, int]:
    out = {}
    for table in TABLES:
        if session is None:
            row = store.db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        else:
            row = store.db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE session=?",
                (session,)).fetchone()
        out[table] = int(row["n"])
    return out


def _pending(store: Store, session: int | None) -> int:
    sql = "SELECT COUNT(*) AS n FROM sheet_queue WHERE sent_at IS NULL"
    if session is None:
        return int(store.db.execute(sql).fetchone()["n"])
    return int(store.db.execute(sql + " AND session=?", (session,)).fetchone()["n"])


def _delete(paths: list[Path]) -> int:
    gone = 0
    for p in paths:
        try:
            p.unlink()
            gone += 1
        except OSError as exc:
            print(f"  could not delete {p}: {exc}")
    return gone


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Delete the participant data this laptop is holding. "
                    "Prints what it would remove and changes nothing unless "
                    "--yes is given. With no target, reports what is on disk.")
    ap.add_argument("--session", type=int, default=None,
                    help="one session: its check-ins and events")
    ap.add_argument("--all", action="store_true",
                    help="every session and the roster cache")
    ap.add_argument("--yes", action="store_true", help="actually delete")
    ap.add_argument("--force", action="store_true",
                    help="delete even with check-ins still queued for the sheet")
    args = ap.parse_args()

    cfg = load_config(os.environ.get("CHECKIN_CONFIG"))
    db_env = os.environ.get("CHECKIN_DB")
    db_path = Path(db_env) if db_env else cfg.db_file
    cache = resolve_path(cfg.roster.cache_path) if cfg.roster.cache_path else None

    store = Store(db_path) if db_path.exists() else None
    session = None if args.all else args.session
    targets = args.all or args.session is not None

    print(f"database   {db_path}")
    if store is None:
        print("           (no database file)")
    else:
        for table, n in _counts(store, None if args.all else args.session).items():
            print(f"           {table:<12} {n}")
        depth = _pending(store, session)
        if depth:
            print(f"           {depth} check-in(s) still queued for the sheet")

    if cache:
        found = cache.exists()
        print(f"roster     {cache}" + ("" if found else " (absent)"))
        if found:
            rows = max(0, len(cache.read_text(encoding="utf-8-sig").splitlines()) - 1)
            print(f"           {rows} row(s) of matric, name and allocation")

    if not targets:
        print("\nNothing selected. Add --session N or --all, then --yes.")
        if store is not None:
            store.close()
        return 0

    print()
    plan: list[str] = []
    if args.all:
        plan.append(f"delete the database file {db_path.name}")
    elif args.session is not None:
        plan.append(f"delete session {args.session} from {db_path.name}")
    if args.all and cache and cache.exists():
        plan.append(f"delete {cache.name}")
    for line in plan:
        print(f"  would {line}")

    if store is not None and (args.all or args.session is not None):
        depth = _pending(store, session)
        if depth and not args.force:
            print(f"\nREFUSED: {depth} check-in(s) have not reached the sheet yet. "
                  f"Send them first (Send now on /admin), or pass --force to "
                  f"delete them anyway.")
            store.close()
            return 1

    if not args.yes:
        print("\nNothing deleted. Repeat the command with --yes.")
        if store is not None:
            store.close()
        return 0

    print()
    if args.session is not None and not args.all and store is not None:
        store.clear_session(args.session)
        print(f"  cleared session {args.session} from the database")
    if store is not None:
        store.close()
    if args.all and db_path.exists():
        _delete([db_path])
        print(f"  deleted {db_path.name}")
    if args.all and cache and cache.exists():
        _delete([cache])
        print(f"  deleted {cache.name}")
    print("\nDone. The Google Sheet is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
