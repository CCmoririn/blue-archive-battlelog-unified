"""Bounded GAS calls. Importing this module never sends a request."""

import json
import os


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
        json={"function": "main", "parameters": [{"parameter": {"season": season}}]},
        timeout=(10, 90),
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError:
        return  # Legacy GAS can return plain text.
    if isinstance(result, dict) and (result.get("error") or result.get("success") is False):
        raise RuntimeError("GAS returned an error")
