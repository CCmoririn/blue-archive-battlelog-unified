"""Explicitly initialize the management tab; never change battlelog tabs."""
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
        parser.error("Initialization writes the dedicated admin tab. Pass --apply.")
    from dotenv import load_dotenv
    if os.environ.get("BATTLELOG_SKIP_DOTENV") != "1":
        load_dotenv(ROOT / ".env")
    from battlelog_admin_store import HEADERS, TAB, initial_settings
    from battlelog_storage import google_client
    from gspread.exceptions import WorksheetNotFound
    identifier = os.environ.get("BATTLELOG_ADMIN_SHEET_ID")
    if not identifier:
        raise RuntimeError("BATTLELOG_ADMIN_SHEET_ID is required")
    book = google_client().open_by_key(identifier)
    try:
        sheet = book.worksheet(TAB)
        values = sheet.get_all_values()
        if values and (values[0] != HEADERS or len(values) != 1):
            if values[0] == HEADERS and len(values) >= 2:
                print("Management tab already initialized; no changes.")
                return
            raise RuntimeError("Existing tab does not match; nothing was overwritten")
    except WorksheetNotFound:
        sheet = book.add_worksheet(TAB, rows=1000, cols=5)
        values = []
    if not values:
        sheet.append_row(HEADERS, value_input_option="RAW")
    sheet.append_row([uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(), "initialized", "{}",
                      json.dumps(initial_settings(), ensure_ascii=False)], value_input_option="RAW")
    print("Management tab initialized. Battlelog data and current season unchanged.")


if __name__ == "__main__":
    main()
