"""Local operations. Cache refresh requires an explicit --apply flag."""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("cache-check", "cache-refresh", "submissions"))
    parser.add_argument("--season", default=None)
    parser.add_argument("--limited", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "cache-refresh" and not args.apply:
        parser.error("cache-refresh changes local cache files; pass --apply after checking the source Sheets")
    from dotenv import load_dotenv
    if os.environ.get("BATTLELOG_SKIP_DOTENV") != "1":
        load_dotenv(ROOT / ".env")
    from battlelog_safety import data_path, file_lock
    if args.command == "submissions":
        import battlelog_journal as journal
        if journal.enabled():
            values = journal.worksheet().get_all_values()
            if not values or values[0] != journal.HEADERS:
                raise RuntimeError("Submission journal headers do not match")
            print(json.dumps([dict(zip(journal.HEADERS, row)) for row in values[-100:] if row != journal.HEADERS], indent=2))
            return
        path = data_path(".battlelog_runtime/submissions.sqlite3")
        if not path.exists():
            print("No submission journal exists.")
            return
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
            rows = database.execute("SELECT token,kind,season,state,row_id,updated FROM submissions ORDER BY updated DESC LIMIT 100").fetchall()
        print(json.dumps([dict(zip(("token", "kind", "season", "state", "row_id", "updated"), row)) for row in rows], indent=2))
        return
    if args.limited:
        from Blue_Archive_Battlelog_collect.config_limited import CURRENT_SEASON, SEASON_LIST, CACHE_DIR
    else:
        from Blue_Archive_Battlelog_collect_public.config import CURRENT_SEASON, SEASON_LIST, CACHE_DIR
    from battlelog_storage import SheetStore
    store = SheetStore(CACHE_DIR, CURRENT_SEASON, SEASON_LIST,
                       "BATTLELOG_SHEET_ID_limited" if args.limited else "BATTLELOG_SHEET_ID",
                       "OUTPUT_SHEET_ID_limited" if args.limited else "OUTPUT_SHEET_ID")
    season = store.season(args.season)
    if args.command == "cache-refresh":
        with file_lock("sheets"):
            path = store.path(season)
            if path.exists():
                folder = data_path(".battlelog_runtime/cache_snapshots")
                folder.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                shutil.copy2(path, folder / (CACHE_DIR + "-" + season + "-" + stamp + ".json"))
            rows = store.refresh(season)
    else:
        # Inspection must not generate a cache or connect to Google.
        rows = store.load(season)
    ids = Counter(str(row.get("ID")) for row in rows or [] if row.get("ID"))
    print(json.dumps({"season": season, "exists": rows is not None,
                      "rows": len(rows or []), "duplicate_id_count": sum(count > 1 for count in ids.values()),
                      "cache_only_rows": sum(bool(row.get("_cache_only")) for row in rows or [])}, indent=2))


if __name__ == "__main__":
    main()
