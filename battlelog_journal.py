"""Optional Sheets journal for hosts whose local filesystem is ephemeral.

Only tokens, hashes and processing states are stored, never image/OCR payloads.
The caller's sheets lock serializes access within the single service instance.
"""

import hashlib
import os
import time
from functools import lru_cache

from battlelog_storage import google_client

TITLE = "_battlelog_submissions"
HEADERS = ["token_hash", "kind", "season", "state", "payload_hash", "row_id", "updated"]


def enabled():
    return bool(os.environ.get("BATTLELOG_JOURNAL_SHEET_ID"))


def worksheet():
    return _worksheet(os.environ["BATTLELOG_JOURNAL_SHEET_ID"])


@lru_cache(maxsize=4)
def _worksheet(identifier):
    # Provisioning is explicit: a missing tab must not silently create a new one.
    return google_client().open_by_key(identifier).worksheet(TITLE)


def locate(sheet, token):
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    values = sheet.get_all_values()
    if not values or values[0] != HEADERS:
        raise RuntimeError("Submission journal headers do not match")
    matches = [(index, row) for index, row in enumerate(values[1:], 2) if row and row[0] == token_hash]
    if len(matches) > 1:
        raise RuntimeError("Duplicate submission journal token")
    if not matches:
        return None, None
    index, row = matches[0]
    record = dict(zip(HEADERS, row + [""] * (len(HEADERS) - len(row))))
    # Re-read on every submit; reuse only within its locked state transitions.
    sheet._battlelog_record = (token_hash, index, record)
    return index, record.copy()


def create(token, kind, season):
    sheet = worksheet()
    _, record = locate(sheet, token)
    if record:
        raise RuntimeError("Submission token already exists")
    sheet.append_row([hashlib.sha256(token.encode("ascii")).hexdigest(), kind, season,
                      "prepared", "", "", str(time.time())], value_input_option="RAW")


def read(token):
    _, record = locate(worksheet(), token)
    return record


def update(token, **changes):
    sheet = worksheet()
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    cached = getattr(sheet, "_battlelog_record", None)
    if isinstance(cached, tuple) and cached[0] == token_hash:
        _, index, record = cached
        record = record.copy()
    else:
        index, record = locate(sheet, token)
    if not record:
        raise RuntimeError("Submission journal record is missing")
    record.update(changes, updated=str(time.time()))
    sheet.update(f"A{index}:G{index}", [[str(record[key] or "") for key in HEADERS]], value_input_option="RAW")
    sheet._battlelog_record = (token_hash, index, record)
