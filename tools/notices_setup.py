"""Import existing public notice paragraphs exactly once into the private Sheet."""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("Import writes the dedicated notices tab. Pass --apply.")
    from dotenv import load_dotenv
    if os.environ.get("BATTLELOG_SKIP_DOTENV") != "1":
        load_dotenv(ROOT / ".env")
    from battlelog_admin_store import store
    from battlelog_notices import NOTICES_TAB, NOTICE_HEADERS, legacy_notices
    from gspread.exceptions import WorksheetNotFound
    book = store.book(os.environ.get("BATTLELOG_ADMIN_SHEET_ID"))
    try:
        sheet = book.worksheet(NOTICES_TAB)
        values = sheet.get_all_values()
        if values and values[0] != NOTICE_HEADERS:
            raise RuntimeError("Existing notice tab differs; nothing was overwritten")
        if len(values) > 1:
            print("Notices already initialized; no changes.")
            return
    except WorksheetNotFound:
        sheet = book.add_worksheet(NOTICES_TAB, rows=1000, cols=len(NOTICE_HEADERS))
        values = []
    notices = legacy_notices()
    if not notices:
        raise RuntimeError("No legacy notices found; stopping")
    now = datetime.now(timezone.utc).isoformat()
    rows = [[uuid.uuid4().hex, n["date"], n["text"], True, len(notices) - index, uuid.uuid4().hex, now] for index, n in enumerate(notices)]
    if not values:
        rows.insert(0, NOTICE_HEADERS)
    # One values update avoids a partially imported batch of announcements.
    sheet.update("A1" if not values else "A2", rows, value_input_option="RAW")
    snapshot = store.read(force=True)
    store.commit(snapshot["revision"], "notices_imported", {"count": len(notices)})
    print(f"Imported {len(notices)} existing notices. No new announcement published.")


if __name__ == "__main__":
    main()
