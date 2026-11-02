"""Bounded GAS calls. Importing this module never sends a request."""

import json
import os
import time


def wait_for_input(season, expected_id):
    """The legacy GAS converts row 3: wait for IMPORTRANGE before invoking it."""
    from battlelog_safety import OutputNotReady
    from battlelog_storage import google_client
    if expected_id is None:
        raise RuntimeError("Conversion requires an input ID")
    sheet = google_client().open_by_key(os.environ["OUTPUT_SHEET_ID"]).worksheet(f"変換前_{season}")
    for delay in (0, 1, 2, 4, 8, 15, 15):
        if delay:
            time.sleep(delay)
        rows = sheet.get("A2:Z3")
        if len(rows) >= 2 and "ID" in rows[0]:
            column = rows[0].index("ID")
            if len(rows[1]) > column and str(rows[1][column]) == str(expected_id):
                return
    raise OutputNotReady("シート間のデータ反映を待っています。同じ確認画面からもう一度お試しください。")


def run_conversion(season, limited=False):
    import requests
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import Request

    content = os.environ.get("credentials")
    url = os.environ.get("GAS_SCRIPT_URL_limited" if limited else "GAS_SCRIPT_URL")
    if not content or not url:
        raise RuntimeError("GAS credentials or URL is not configured")
    # No shared temporary credential file; never write a private key to /tmp.
    credentials = Credentials.from_service_account_info(
        json.loads(content), scopes=["https://www.googleapis.com/auth/script.external_request"]
    )
    credentials.refresh(lambda *args, **kwargs: Request()(*args, **dict(kwargs, timeout=30)))
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"},
        json={"function": "main", "season": season,
              "parameters": [{"parameter": {"season": season}}]},
        timeout=(10, 90),
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError:
        return  # Legacy GAS can return plain text.
    if isinstance(result, dict) and (result.get("error") or result.get("success") is False):
        raise RuntimeError("GAS returned an error")
