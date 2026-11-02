"""Google Sheets and cache operations shared by the public and limited apps."""

import json
import logging
import os
import unicodedata
from datetime import datetime

from battlelog_safety import OutputNotReady, atomic_json, data_path, file_lock, nonnegative_int, read_json

log = logging.getLogger(__name__)


def google_credentials(scopes):
    from google.oauth2.service_account import Credentials
    content = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if content:
        return Credentials.from_service_account_info(json.loads(content), scopes=scopes)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        raise RuntimeError("Google認証情報が設定されていません。")
    return Credentials.from_service_account_file(path, scopes=scopes)


def google_client():
    import gspread
    client = gspread.authorize(google_credentials(["https://www.googleapis.com/auth/spreadsheets"]))
    # gspread 5.x exposes Client.set_timeout; keep network waits bounded.
    client.set_timeout((10, 60))
    return client


def records_from_values(rows, head_row=2):
    if len(rows) < head_row:
        raise RuntimeError("シートの見出し行が見つかりません。")
    seen = {}
    headers = []
    for value in rows[head_row - 1]:
        base = str(value).strip() or "空欄"
        seen[base] = seen.get(base, 0) + 1
        headers.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return [dict(zip(headers, list(row) + [""] * max(0, len(headers) - len(row))))
            for row in rows[head_row:] if any(row)]


def record_signature(row):
    # Ignore counters and local metadata; absent and trailing blank cells agree.
    return tuple(sorted((str(key), str(value)) for key, value in row.items()
                        if key not in ("GOOD", "BAD", "source") and not str(key).startswith("_")
                        and value is not None and value != ""))


def row_key(row):
    return (str(row.get("source", "")), record_signature(row))


def canonical_rows(rows):
    """Collapse only identical ID/content pairs, retaining the first row's votes."""
    result, seen = [], set()
    for row in rows:
        key = row_key(row)
        # Missing IDs are not proof of a duplicate match.
        if not row.get("ID") or key not in seen:
            result.append(row)
            seen.add(key)
    return result


def parse_date(row):
    try:
        return datetime.strptime(str(row.get("日付", "")), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.min


class SheetStore:
    def __init__(self, cache_dir, current, seasons, input_env, output_env, client_factory=google_client, settings_provider=None):
        self.cache_dir = cache_dir
        self.current = current
        self.seasons = seasons
        self.input_env = input_env
        self.output_env = output_env
        self.client_factory = client_factory
        self.settings_provider = settings_provider

    def season(self, value=None):
        from battlelog_safety import validate_season
        current, seasons = self.settings_provider() if self.settings_provider else (self.current, self.seasons)
        return validate_season(value, seasons, current)

    def path(self, season):
        return data_path(self.cache_dir) / (self.season(season) + ".json")

    def sheet(self, env, title):
        key = os.environ.get(env)
        if not key:
            raise RuntimeError(f"{env} is not configured")
        return self.client_factory().open_by_key(key).worksheet(title)

    def save(self, season, rows):
        atomic_json(self.path(season), rows)

    def load(self, season):
        return read_json(self.path(season))

    def get(self, season=None):
        season = self.season(season)
        rows = self.load(season)
        if rows is not None:
            return canonical_rows(rows)
        with file_lock("sheets"):
            rows = self.load(season)
            return self.refresh(season) if rows is None else canonical_rows(rows)

    def refresh(self, season=None):
        season = self.season(season)
        with file_lock("sheets"):
            client = self.client_factory()
            output_key = os.environ.get(self.output_env)
            if not output_key:
                raise RuntimeError(f"{self.output_env} is not configured")
            sheet = client.open_by_key(output_key).worksheet(f"出力結果_{season}")
            rows = records_from_values(sheet.get_all_values())
            # Preserve existing label semantics until the live GAS topology is verified.
            for row in rows:
                row["source"] = "限定"
            transfer_key = os.environ.get("GENERAL_TRANSFER_SHEET_ID") or output_key
            transfer_name = os.environ.get("GENERAL_TRANSFER_SHEET_NAME", "一般版から転送").format(season=season)
            try:
                transfer = client.open_by_key(transfer_key).worksheet(transfer_name)
                transferred = records_from_values(transfer.get_all_values())
            except Exception as error:
                # Only a genuinely absent optional sheet is safe to ignore.
                from gspread.exceptions import WorksheetNotFound
                if not isinstance(error, WorksheetNotFound):
                    raise
                transferred = []
            for row in transferred:
                row_season = row.get("season") or row.get("シーズン")
                if row_season and str(row_season).strip() != season:
                    continue
                row["source"] = "一般"
                rows.append(row)
            previous = self.load(season) or []
            known = {row_key(row) for row in rows}
            for row in previous:
                if row.get("_cache_only") and row_key(row) not in known:
                    rows.append(row)
            rows = canonical_rows(rows)
            rows.sort(key=parse_date, reverse=True)
            self.save(season, rows)
            return rows

    def insert(self, data, season=None):
        season = self.season(season)
        if len(data) != 18:
            raise ValueError("登録内容の項目数が正しくありません。")
        with file_lock("sheets"):
            worksheet = self.sheet(self.input_env, f"変換前_{season}")
            headers = worksheet.row_values(2)
            new_row = list(data)
            new_id = None
            if "ID" in headers:
                id_col = headers.index("ID")
                ids = worksheet.col_values(id_col + 1)[2:]
                new_id = str(max([nonnegative_int(unicodedata.normalize("NFKC", str(v)).strip()) for v in ids] or [0]) + 1)
                new_row += [""] * max(0, len(headers) - len(new_row))
                if id_col < 18:
                    raise RuntimeError("ID列の位置を確認してください。")
                new_row[id_col] = new_id
                for name in ("GOOD", "BAD"):
                    if name in headers:
                        position = headers.index(name)
                        if position < 18:
                            raise RuntimeError("評価列の位置を確認してください。")
                        new_row[position] = "0"
            elif self.input_env == "BATTLELOG_SHEET_ID":
                raise RuntimeError("ID列が見つかりません。")
            worksheet.insert_row(new_row, 3, value_input_option="RAW")
            return new_id

    def fetch(self, season=None, expected_id=None, expected_row=None):
        season = self.season(season)
        worksheet = self.sheet(self.output_env, f"出力結果_{season}")
        rows = canonical_rows(records_from_values(worksheet.get_all_values()))
        if expected_id is not None:
            matches = [row for row in rows if str(row.get("ID", "")) == str(expected_id)]
            if not matches:
                raise OutputNotReady("登録したデータの反映を待っています。")
            if len(matches) != 1:
                raise RuntimeError("登録したデータを一意に確認できませんでした。")
            return matches[0]
        if not rows:
            raise OutputNotReady("変換後のデータが見つかりません。")
        if expected_row:
            expected = parse_date({"日付": expected_row[0]})
            if expected == datetime.min or parse_date(rows[0]) != expected:
                raise OutputNotReady("登録したデータの反映を待っています。")
        return rows[0]

    def append(self, row, season=None, source="限定", cache_only=False):
        season = self.season(season)
        if not isinstance(row, dict) or not row.get("日付"):
            raise ValueError("対戦データの形式が正しくありません。")
        with file_lock("sheets"):
            rows = self.get(season)
            item = dict(row, source=source)
            if cache_only:
                item["_cache_only"] = True
            identity = row_key(item)
            rows = [old for old in rows if row_key(old) != identity]
            rows.insert(0, item)
            self.save(season, rows)

    def vote(self, row_id, vote_type, delta, season=None):
        season = self.season(season)
        if vote_type not in ("GOOD", "BAD") or delta not in (-1, 1):
            raise ValueError("評価の指定が正しくありません。")
        with file_lock("sheets"):
            cached = self.get(season)
            matches = [row for row in cached if str(row.get("ID", "")) == str(row_id)]
            if len(matches) != 1:
                return False
            worksheet = self.sheet(self.output_env, f"出力結果_{season}")
            values = worksheet.get_all_values()
            headers = values[1]
            if "ID" not in headers or vote_type not in headers:
                raise RuntimeError("評価用の列が見つかりません。")
            id_col, vote_col = headers.index("ID"), headers.index(vote_type)
            positions = [(index, row) for index, row in enumerate(values[2:], 3)
                         if len(row) > id_col and str(row[id_col]) == str(row_id)]
            if not positions:
                return False
            if len(positions) > 1:
                signatures = {record_signature(records_from_values([[""], headers, row])[0])
                              for _, row in positions}
                if len(signatures) != 1:
                    return False
            position, row = positions[0]
            if record_signature(records_from_values([[""], headers, row])[0]) != record_signature(matches[0]):
                return False
            count = max(0, nonnegative_int(row[vote_col] if len(row) > vote_col else 0) + delta)
            # Sheets is authoritative. A failed request must not alter local counts.
            worksheet.update_cell(position, vote_col + 1, str(count))
            matches[0][vote_type] = str(count)
            self.save(season, cached)
            return True
