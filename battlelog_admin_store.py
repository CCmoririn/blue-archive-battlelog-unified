"""Private, append-only management settings and recoverable master updates."""
import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from battlelog_safety import atomic_json, data_path, file_lock, read_json
from battlelog_storage import google_client

TAB = "_battlelog_admin"
HEADERS = ["revision", "updated_utc", "action", "details", "settings"]
JST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)


from battlelog_characters import CharacterManagement, Conflict, character_hash
from battlelog_notices import NoticeManagement


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
    notice = state.get("season_notice")
    if notice is not None:
        if not isinstance(notice, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(notice.get("id", ""))):
            raise RuntimeError("Season announcement is invalid")
        season_key(notice["season"])
        if datetime.fromisoformat(notice["at"]).tzinfo is None or type(notice.get("published")) is not bool:
            raise RuntimeError("Season announcement is invalid")
        if "row" in notice and (type(notice["row"]) is not int or notice["row"] < 2 or
                                (not notice["published"] and notice.get("entry", {}).get("id") != notice["id"])):
            raise RuntimeError("Season announcement row is invalid")
    return state


class AdminStore(CharacterManagement, NoticeManagement):
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
            state = self.read()["state"]
        except Exception:
            log.exception("Using last known management settings")
            previous = self._last or read_json(data_path(".battlelog_runtime/admin-settings.json"))
            if not previous:
                raise RuntimeError("シーズン設定を取得できませんでした。しばらくしてからお試しください。")
            validate_settings(previous["state"])
            self._last, self._at = previous, time.monotonic() - 50
            state = copy.deepcopy(previous["state"])
        if ((state.get("season_notice") and not state["season_notice"].get("published")) or
                (state.get("season_notice") is None and state.get("schedule"))):
            try:
                self.publish_due_season_notice()
            except Exception:
                log.exception("Season announcement awaits retry")
        return state

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
            self._at = float("-inf")
            return revision

    def master(self, kind):
        if kind not in ("STRIKER", "SPECIAL"):
            raise ValueError("STRIKERかSPECIALを選択してください。")
        return self.sheet(os.environ.get("CHARDATA_SHEET_ID"), kind)

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
            if latest["revision"] != expected:
                raise Conflict("画面を読み込み直してから予約してください。")
            self.publish_due_season_notice(now=now)
            latest = self.read(force=True)
            expected = latest["revision"]
            state = latest["state"]
            current, seasons = effective_settings(state, now)
            if key == current or key not in state["prepared"]:
                raise ValueError("準備済みの次シーズンを選択してください。")
            problems = self.check_season(key)
            if problems:
                raise ValueError("予約できません。" + " ".join(problems))
            state.update(current=current, seasons=seasons, schedule={"season": key, "at": at.astimezone(timezone.utc).isoformat()})
            state["season_notice"] = {"id": uuid.uuid4().hex, "season": key,
                                      "at": at.astimezone(timezone.utc).isoformat(), "published": False}
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
            state["season_notice"] = None
            return self.commit(expected, "schedule_canceled", {}, state)

    def set_current(self, expected, key):
        key = season_key(key)
        with file_lock("sheets"):
            if self.read(force=True)["revision"] != expected:
                raise Conflict("画面を読み込み直してから変更してください。")
            self.publish_due_season_notice()
            latest = self.read(force=True)
            expected = latest["revision"]
            state = latest["state"]
            _, seasons = effective_settings(state)
            if key not in {s["key"] for s in seasons}:
                raise ValueError("公開済みのシーズンを選択してください。")
            if self.check_season(key):
                raise ValueError("対象シーズンのシート構成を確認してください。")
            before, _ = effective_settings(state)
            state.update(current=key, seasons=seasons, schedule=None)
            state["season_notice"] = ({"id": uuid.uuid4().hex, "season": key,
                                       "at": datetime.now(timezone.utc).isoformat(), "published": False}
                                      if key != before else None)
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
