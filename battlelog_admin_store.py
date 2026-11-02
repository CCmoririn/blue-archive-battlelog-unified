"""Private, append-only management settings and recoverable master updates."""
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from battlelog_safety import atomic_json, data_path, file_lock, read_json
from battlelog_storage import google_client

TAB = "_battlelog_admin"
HEADERS = ["revision", "updated_utc", "action", "details", "settings"]
JST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)


class Conflict(ValueError):
    pass


def character_hash(item):
    fields = {key: item.get(key) for key in ("kind", "name", "image", "range", "cover")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def season_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"s[1-9][0-9]{0,3}", value):
        raise ValueError("シーズン名は s12 のように入力してください。")
    return value


def initial_settings():
    from Blue_Archive_Battlelog_collect_public.config import CURRENT_SEASON, SEASON_LIST
    return {"current": CURRENT_SEASON, "seasons": copy.deepcopy(SEASON_LIST), "prepared": [], "schedule": None}


def effective_settings(state, now=None):
    current = state["current"]
    seasons = copy.deepcopy(state["seasons"])
    planned = state.get("schedule")
    if planned and (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(planned["at"]):
        current = planned["season"]
        if current not in {s["key"] for s in seasons}:
            seasons.append({"key": current, "label": current})
    return current, seasons


def validate_settings(state):
    if not isinstance(state, dict):
        raise RuntimeError("Management settings are invalid")
    keys = [season_key(s["key"]) for s in state["seasons"]]
    if len(keys) != len(set(keys)) or state["current"] not in keys:
        raise RuntimeError("Management season list is invalid")
    for key in state.get("prepared", []):
        season_key(key)
    planned = state.get("schedule")
    if planned:
        season_key(planned["season"])
        instant = datetime.fromisoformat(planned["at"])
        if instant.tzinfo is None or planned["season"] not in state.get("prepared", []) + keys:
            raise RuntimeError("Management schedule is invalid")
    return state


class AdminStore:
    def __init__(self, client_factory=google_client):
        self.client_factory = client_factory
        self._books = {}
        self._sheets = {}
        self._last = None
        self._at = 0
        self._guard = threading.RLock()

    def book(self, key):
        if not key:
            raise RuntimeError("Management spreadsheet is not configured")
        if key not in self._books:
            self._books[key] = self.client_factory().open_by_key(key)
        return self._books[key]

    def journal(self):
        return self.sheet(os.environ.get("BATTLELOG_ADMIN_SHEET_ID"), TAB)

    def sheet(self, key, title):
        identity = (key, title)
        if identity not in self._sheets:
            self._sheets[identity] = self.book(key).worksheet(title)
        return self._sheets[identity]

    def read(self, force=False):
        with self._guard:
            if not force and self._last and time.monotonic() - self._at < 60:
                return copy.deepcopy(self._last)
            values = self.journal().get_all_values()
            if not values or values[0] != HEADERS or len(values) < 2:
                raise RuntimeError("Management journal has not been initialized")
            rows = [r for r in values[1:] if any(r)]
            last = rows[-1]
            state = validate_settings(json.loads(last[4]))
            result = {"revision": last[0], "state": state,
                      "events": [{"revision": r[0], "at": r[1], "action": r[2],
                                  "details": json.loads(r[3])} for r in rows[-40:]]}
            self._last, self._at = result, time.monotonic()
            atomic_json(data_path(".battlelog_runtime/admin-settings.json"), result)
            return copy.deepcopy(result)

    def public(self):
        try:
            return self.read()["state"]
        except Exception:
            log.exception("Using last known management settings")
            previous = self._last or read_json(data_path(".battlelog_runtime/admin-settings.json"))
            if not previous:
                raise RuntimeError("シーズン設定を取得できませんでした。しばらくしてからお試しください。")
            validate_settings(previous["state"])
            self._last, self._at = previous, time.monotonic() - 50
            return copy.deepcopy(previous["state"])

    def commit(self, expected, action, details, state=None):
        with file_lock("sheets"), self._guard:
            latest = self.read(force=True)
            if latest["revision"] != expected:
                raise Conflict("別の操作で内容が更新されています。画面を読み込み直して確認してください。")
            snapshot = validate_settings(copy.deepcopy(state if state is not None else latest["state"]))
            revision = uuid.uuid4().hex
            self.journal().append_row([revision, datetime.now(timezone.utc).isoformat(), action,
                                       json.dumps(details, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False)],
                                      value_input_option="RAW")
            self._at = 0
            return revision

    def master(self, kind):
        if kind not in ("STRIKER", "SPECIAL"):
            raise ValueError("STRIKERかSPECIALを選択してください。")
        return self.sheet(os.environ.get("CHARDATA_SHEET_ID"), kind)

    def characters(self):
        result = []
        for kind in ("STRIKER", "SPECIAL"):
            rows = self.master(kind).get_all_values()
            if not rows or "キャラ名" not in rows[0] or "アイコン" not in rows[0]:
                raise RuntimeError("Character master headers do not match")
            for number, row in enumerate(rows[1:], 2):
                item = dict(zip(rows[0], row + [""] * len(rows[0])))
                if item.get("キャラ名"):
                    result.append({"kind": kind, "row": number, "name": item["キャラ名"],
                                   "image": item.get("アイコン", ""), "range": item.get("射程", ""),
                                   "cover": str(item.get("遮蔽", "")).upper() == "TRUE"})
        return result

    def save_character(self, expected, fields):
        kind = fields.get("kind")
        sheet = self.master(kind)
        name = (fields.get("name") or "").strip()
        image = (fields.get("image") or "").strip()
        original = (fields.get("original") or "").strip()
        url = urlsplit(image)
        if not name or len(name) > 120 or any(c in name for c in "\r\n\t"):
            raise ValueError("キャラ名を120文字以内で入力してください。")
        if len(image) > 2000 or url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("アイコンには https:// で始まる画像URLを入力してください。")
        if original and original != name:
            raise ValueError("登録済みの名前は戦績との照合に使うため変更できません。")
        if kind == "STRIKER" and str(fields.get("range")) not in ("350", "450", "550", "650", "750", "850"):
            raise ValueError("射程を選択してください。")
        with file_lock("sheets"):
            all_chars = self.characters()
            matches = [c for c in all_chars if c["name"] == name]
            if (not original and matches) or (original and (len(matches) != 1 or matches[0]["kind"] != kind)):
                raise ValueError("同じ名前の登録状況を確認してください。種類は追加後に変更できません。")
            before = matches[0] if original else None
            if before and fields.get("base_hash") != character_hash(before):
                raise Conflict("このキャラの情報が更新されています。画面を読み込み直して確認してください。")
            after = {"name": name, "kind": kind, "image": image,
                     "range": str(fields.get("range", "")) if kind == "STRIKER" else "",
                     "cover": fields.get("cover") == "1" if kind == "STRIKER" else False}
            operation = uuid.uuid4().hex
            revision = self.commit(expected, "character_pending", {"operation": operation, "before": before, "after": after})
            rows = sheet.get_all_values()
            headers = rows[0]
            row_number = before["row"] if before else len(rows) + 1
            row = (rows[row_number - 1] + [""] * len(headers))[:len(headers)] if before else [""] * len(headers)
            updates = {"キャラ名": name, "アイコン": image}
            if kind == "STRIKER":
                updates.update({"射程": after["range"], "遮蔽": "TRUE" if after["cover"] else "FALSE"})
            for key, value in updates.items():
                if key not in headers:
                    raise RuntimeError("Character master column is missing")
                row[headers.index(key)] = value
            if before:
                sheet.update(f"A{row_number}", [row], value_input_option="RAW")
            else:
                sheet.append_row(row, value_input_option="RAW")
            dictionary = self.sheet(os.environ.get("OUTPUT_SHEET_ID"), "辞書")
            if name not in dictionary.col_values(1):
                dictionary.append_row([name], value_input_option="RAW")
            self.commit(revision, "character_saved", {"operation": operation, "before": before, "after": after})
            return after

    def prepare_season(self, expected, key):
        key = season_key(key)
        with file_lock("sheets"):
            latest = self.read(force=True)
            if latest["revision"] != expected:
                raise Conflict("画面を読み込み直してから準備してください。")
            current, seasons = effective_settings(latest["state"])
            if key in {s["key"] for s in seasons}:
                raise ValueError("公開済みのシーズンです。新しいシーズン名を指定してください。")
            revision = self.commit(expected, "season_preparing", {"season": key})
            input_book = self.book(os.environ.get("BATTLELOG_SHEET_ID"))
            output_book = self.book(os.environ.get("OUTPUT_SHEET_ID"))
            from gspread.exceptions import WorksheetNotFound
            for book, prefix in ((input_book, "変換前_"), (output_book, "出力結果_")):
                headers = book.worksheet(prefix + current).get("A1:Z2")
                if len(headers) < 2 or not {"ID", "GOOD", "BAD"}.issubset(headers[1]):
                    raise RuntimeError("Season template headers do not match")
                try:
                    target = book.worksheet(prefix + key)
                    if target.get("A1:Z2") != headers:
                        raise ValueError("同名シートの見出しがひな形と異なります。既存の内容は変更していません。")
                except WorksheetNotFound:
                    target = book.add_worksheet(prefix + key, rows=1000, cols=26)
                    target.update("A1", headers, value_input_option="RAW")
            if os.environ["BATTLELOG_SHEET_ID"] != os.environ["OUTPUT_SHEET_ID"]:
                # GAS consumes row 3 only. Bound the mirror to its 1000-row grid,
                # as in the existing layout, even as the input sheet grows.
                formula = '=IMPORTRANGE("' + os.environ["BATTLELOG_SHEET_ID"] + '","変換前_' + key + '!A1:Z1000")'
                try:
                    mirror = output_book.worksheet("変換前_" + key)
                    if mirror.acell("A1", value_render_option="FORMULA").value != formula:
                        raise ValueError("同名の転送シートがあります。既存の内容は変更していません。")
                except WorksheetNotFound:
                    mirror = output_book.add_worksheet("変換前_" + key, rows=1000, cols=26)
                    mirror.update("A1", [[formula]], value_input_option="USER_ENTERED")
            state = latest["state"]
            if key not in state["prepared"]:
                state["prepared"].append(key)
            self.commit(revision, "season_prepared", {"season": key}, state)
            return self.check_season(key)

    def check_season(self, key):
        key = season_key(key)
        problems = []
        expected = None
        for env, prefix in (("BATTLELOG_SHEET_ID", "変換前_"), ("OUTPUT_SHEET_ID", "出力結果_"),
                            ("OUTPUT_SHEET_ID", "変換前_")):
            try:
                rows = self.sheet(os.environ.get(env), prefix + key).get("A1:Z2")
                if len(rows) < 2 or not {"ID", "GOOD", "BAD"}.issubset(rows[1]):
                    problems.append(prefix + key + "：見出しや転送の反映を確認してください。")
                else:
                    positions = tuple(rows[1].index(name) for name in ("ID", "GOOD", "BAD"))
                    if any(rows[1].count(name) != 1 for name in ("ID", "GOOD", "BAD")) or min(positions) < 18:
                        problems.append(prefix + key + "：ID・評価の列を確認してください。")
                    elif expected is None:
                        expected = positions
                    elif positions != expected:
                        problems.append(prefix + key + "：入力側とID・評価の列位置が異なります。")
            except Exception:
                problems.append(prefix + key + "：シートを読み取れませんでした。")
        try:
            if not self.sheet(os.environ.get("OUTPUT_SHEET_ID"), "辞書").col_values(1):
                problems.append("名前補正の辞書が空です。")
        except Exception:
            problems.append("名前補正の辞書を読み取れませんでした。")
        return problems

    def set_schedule(self, expected, key, local_time, now=None):
        key = season_key(key)
        try:
            at = datetime.fromisoformat(local_time)
            if at.tzinfo is not None:
                raise ValueError()
            at = at.replace(tzinfo=JST)
        except (ValueError, TypeError):
            raise ValueError("切り替え日時を日本時間で入力してください。")
        now = now or datetime.now(timezone.utc)
        if at <= now:
            raise ValueError("現在より後の日時を指定してください。")
        with file_lock("sheets"):
            latest = self.read(force=True)
            state = latest["state"]
            current, seasons = effective_settings(state, now)
            if key == current or key not in state["prepared"]:
                raise ValueError("準備済みの次シーズンを選択してください。")
            problems = self.check_season(key)
            if problems:
                raise ValueError("予約できません。" + " ".join(problems))
            state.update(current=current, seasons=seasons, schedule={"season": key, "at": at.astimezone(timezone.utc).isoformat()})
            return self.commit(expected, "schedule_saved", {"season": key, "at": at.isoformat()}, state)

    def cancel_schedule(self, expected):
        with file_lock("sheets"):
            latest = self.read(force=True)
            state = latest["state"]
            if not state.get("schedule"):
                raise ValueError("切り替え予約はありません。")
            if datetime.now(timezone.utc) >= datetime.fromisoformat(state["schedule"]["at"]):
                raise ValueError("この予約は適用済みです。現行シーズンの変更を使ってください。")
            state["schedule"] = None
            return self.commit(expected, "schedule_canceled", {}, state)

    def set_current(self, expected, key):
        key = season_key(key)
        with file_lock("sheets"):
            latest = self.read(force=True)
            state = latest["state"]
            _, seasons = effective_settings(state)
            if key not in {s["key"] for s in seasons}:
                raise ValueError("公開済みのシーズンを選択してください。")
            if self.check_season(key):
                raise ValueError("対象シーズンのシート構成を確認してください。")
            before, _ = effective_settings(state)
            state.update(current=key, seasons=seasons, schedule=None)
            return self.commit(expected, "current_changed", {"before": before, "after": key}, state)


store = AdminStore()


def public_settings():
    """Pin one settings snapshot per request; evaluate the reservation on every request."""
    if not os.environ.get("BATTLELOG_ADMIN_SHEET_ID"):
        from Blue_Archive_Battlelog_collect_public.config import CURRENT_SEASON, SEASON_LIST
        return CURRENT_SEASON, SEASON_LIST
    from flask import g, has_request_context
    if has_request_context() and hasattr(g, "battlelog_public_settings"):
        return g.battlelog_public_settings
    try:
        result = effective_settings(store.public())
    except Exception as error:
        from werkzeug.exceptions import ServiceUnavailable
        raise ServiceUnavailable() from error
    if has_request_context():
        g.battlelog_public_settings = result
    return result


def current_season():
    return public_settings()[0]


def season_list():
    return public_settings()[1]
