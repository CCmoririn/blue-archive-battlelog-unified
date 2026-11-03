"""Plain-text notices with drafts and recoverable edits in the private Sheet."""
import copy
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from battlelog_characters import Conflict, fullwidth_parentheses
from battlelog_safety import atomic_json, data_path, file_lock, read_json

NOTICES_TAB = "_battlelog_notices"
NOTICE_HEADERS = ["id", "date", "text", "published", "order", "revision", "updated"]


def legacy_notices():
    class Paragraphs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active, self.parts, self.rows = False, [], []
        def handle_starttag(self, tag, attrs):
            if tag == "p":
                self.active, self.parts = True, []
            elif tag == "br" and self.active:
                self.parts.append("\n")
        def handle_data(self, value):
            if self.active:
                self.parts.append(value)
        def handle_endtag(self, tag):
            if tag == "p" and self.active:
                match = re.fullmatch(r"(\d{4}/\d{2}/\d{2})[\s　]+(.+)", "".join(self.parts).strip(), re.S)
                if not match:
                    raise RuntimeError("Existing notice could not be imported")
                self.rows.append({"date": match[1].replace("/", "-"), "text": match[2], "published": True})
                self.active = False
    parser = Paragraphs()
    source = Path(__file__).parent / "Blue_Archive_Battlelog_collect_public/templates/notices.html"
    html = source.read_text(encoding="utf-8")
    if "<!-- legacy-notices-start -->" in html:
        html = html.split("<!-- legacy-notices-start -->", 1)[1].split("<!-- legacy-notices-end -->", 1)[0]
    parser.feed(html)
    return parser.rows


class NoticeManagement:
    def notices_sheet(self):
        return self.sheet(os.environ.get("BATTLELOG_ADMIN_SHEET_ID"), NOTICES_TAB)

    def notices(self, force=False):
        self.publish_due_season_notice()
        if not force and getattr(self, "_notice_cache", None) is not None and time.monotonic() - getattr(self, "_notice_at", 0) < 60:
            return copy.deepcopy(self._notice_cache)
        values = self.notices_sheet().get_all_values()
        if not values or values[0] != NOTICE_HEADERS:
            raise RuntimeError("Notice management has not been initialized")
        rows = [dict(zip(NOTICE_HEADERS, row + [""] * len(NOTICE_HEADERS))) for row in values[1:] if any(row)]
        for row in rows:
            row["published"] = str(row["published"]).upper() in ("TRUE", "1")
            row["order"] = int(row["order"])
        rows.sort(key=lambda row: (row["date"], row["order"]), reverse=True)
        self._notice_cache, self._notice_at = rows, time.monotonic()
        atomic_json(data_path(".battlelog_runtime/notices.json"), rows)
        return copy.deepcopy(rows)

    def public_notices(self):
        if not os.environ.get("BATTLELOG_ADMIN_SHEET_ID"):
            return None
        try:
            rows = self.notices()
        except Exception:
            rows = getattr(self, "_notice_cache", None)
            if rows is None:
                rows = read_json(data_path(".battlelog_runtime/notices.json"))
            if rows is None:
                return None  # Existing HTML remains a fallback during setup/outages.
        return [row for row in rows if row["published"]]

    def publish_due_season_notice(self, now=None):
        """Reserve a durable row before writing so a retry cannot append twice."""
        if not os.environ.get("BATTLELOG_ADMIN_SHEET_ID"):
            return False
        now = now or datetime.now(timezone.utc)
        state = self.read()["state"]
        pending = state.get("season_notice")
        # Adopt a pre-existing future reservation, never an already-applied one.
        planned = state.get("schedule")
        if pending is None and planned and now < datetime.fromisoformat(planned["at"]):
            with file_lock("sheets"):
                latest = self.read(force=True)
                state = latest["state"]
                planned = state.get("schedule")
                if state.get("season_notice") is None and planned and now < datetime.fromisoformat(planned["at"]):
                    state["season_notice"] = {"id": uuid.uuid4().hex, "season": planned["season"],
                                              "at": planned["at"], "published": False}
                    self.commit(latest["revision"], "season_notice_armed", {"season": planned["season"]}, state)
            return False
        if not pending or pending.get("published") or ("row" not in pending and now < datetime.fromisoformat(pending["at"])):
            return False
        with file_lock("sheets"):
            latest = self.read(force=True)
            state = latest["state"]
            pending = state.get("season_notice")
            if not pending or pending.get("published") or ("row" not in pending and now < datetime.fromisoformat(pending["at"])):
                return False
            sheet = self.notices_sheet()
            values = sheet.get_all_values()
            if not values or values[0] != NOTICE_HEADERS:
                raise RuntimeError("Notice management has not been initialized")
            if "row" not in pending:
                at = datetime.fromisoformat(pending["at"])
                pending["row"] = len(values) + 1
                pending["entry"] = {
                    "id": pending["id"], "date": at.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"),
                    "text": f"シーズン{int(pending['season'][1:])}に更新しました。", "published": True,
                    "order": max([int(row[4]) for row in values[1:] if len(row) > 4 and row[4] != ""] or [0]) + 1,
                    "revision": uuid.uuid4().hex, "updated": now.isoformat(),
                }
                latest["revision"] = self.commit(latest["revision"], "season_notice_pending",
                                                  {"season": pending["season"], "notice_id": pending["id"]}, state)
            position = pending["row"]
            existing = values[position - 1] if position <= len(values) else []
            if any(existing) and existing[0] != pending["id"]:
                raise Conflict("自動投稿先のお知らせが変更されています。管理者にお問い合わせください。")
            if not any(existing):
                sheet.update(f"A{position}", [[pending["entry"][key] for key in NOTICE_HEADERS]], value_input_option="RAW")
            self._notice_at = float("-inf")
            pending["published"] = True
            pending.pop("entry", None)
            self.commit(latest["revision"], "season_notice_published",
                        {"season": pending["season"], "notice_id": pending["id"]}, state)
            return True

    def save_notice(self, expected, fields):
        try:
            date = datetime.strptime(fields.get("date", ""), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError("お知らせの日付を入力してください。")
        text = fullwidth_parentheses((fields.get("text") or "").strip())
        if not text or len(text) > 6000:
            raise ValueError("本文を6000文字以内で入力してください。")
        with file_lock("sheets"):
            notices = self.notices(force=True)
            identifier = fields.get("id", "")
            before = next((n for n in notices if n["id"] == identifier), None)
            if identifier and (not before or before["revision"] != fields.get("base_revision")):
                raise Conflict("このお知らせが更新されています。画面を読み込み直してください。")
            after = {"id": identifier or uuid.uuid4().hex, "date": date, "text": text, "published": fields.get("published") == "1",
                     "order": before["order"] if before else max([n["order"] for n in notices] or [0]) + 1,
                     "revision": uuid.uuid4().hex, "updated": datetime.now(timezone.utc).isoformat()}
            revision = self.commit(expected, "notice_pending", {"before": before, "after": after})
            sheet = self.notices_sheet()
            values = sheet.get_all_values()
            if before:
                positions = [index for index, row in enumerate(values[1:], 2) if row and row[0] == identifier]
                if len(positions) != 1 or values[positions[0] - 1][5] != before["revision"]:
                    raise Conflict("お知らせが更新されています。内容を確認してください。")
                sheet.update(f"A{positions[0]}", [[after[k] for k in NOTICE_HEADERS]], value_input_option="RAW")
            else:
                sheet.append_row([after[k] for k in NOTICE_HEADERS], value_input_option="RAW")
            self._notice_at = float("-inf")
            self.commit(revision, "notice_saved", {"before": before, "after": after})
            return after

    def restore_notice(self, expected, event_id):
        with file_lock("sheets"):
            latest = self.read(force=True)
            event = next((e for e in latest["events"] if e["revision"] == event_id and e["action"] == "notice_saved"), None)
            if not event or not event["details"].get("before"):
                raise ValueError("編集前のお知らせが見つかりません。")
            before, after = event["details"]["before"], event["details"]["after"]
            return self.save_notice(expected, {**before, "base_revision": after["revision"], "published": "1" if before["published"] else "0"})
