"""Offline management regressions: authentication, reservations and recoverable writes."""
import copy
import io
import itertools
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "Blue_Archive_Battlelog_collect_public")]
os.environ["BATTLELOG_SKIP_DOTENV"] = "1"

from werkzeug.security import generate_password_hash
from gspread.exceptions import WorksheetNotFound

from battlelog_admin_store import AdminStore, Conflict, HEADERS, TAB, character_hash, effective_settings, initial_settings
from battlelog_notices import NOTICES_TAB, NOTICE_HEADERS

_SHEET_IDS = itertools.count(1)


class Sheet:
    def __init__(self, title, values=None, books=None):
        self.title, self.values, self.books = title, copy.deepcopy(values or []), books
        self.id, self.col_count = next(_SHEET_IDS), 26
        self.formats = [[{} for value in row] for row in self.values]

    def get_all_values(self, **kwargs):
        rows = []
        for raw in self.values:
            row = [("TRUE" if v else "FALSE") if isinstance(v, bool) else str(v) if isinstance(v, (int, float)) else v for v in raw]
            while row and row[-1] == "":
                row.pop()
            rows.append(row)
        while rows and not rows[-1]:
            rows.pop()
        width = max(map(len, rows), default=0)
        return [row + [""] * (width - len(row)) for row in rows]

    def append_row(self, row, **kwargs):
        self.values.append(list(row))
        self.formats.append([{} for value in row])

    def get(self, cell_range):
        if self.values and self.values[0] and str(self.values[0][0]).startswith("=IMPORTRANGE"):
            title = re.search(r'変換前_s\d+', self.values[0][0])[0]
            return self.books["input"].worksheet(title).get(cell_range)
        return copy.deepcopy(self.values[:2])

    def update(self, cell_range, rows, **kwargs):
        first = int(re.search(r"\d+", cell_range)[0]) - 1
        while len(self.values) < first + len(rows):
            self.values.append([])
        self.values[first:first + len(rows)] = copy.deepcopy(rows)

    def col_values(self, column):
        return [r[column - 1] if len(r) >= column else "" for r in self.values]

    def acell(self, cell, **kwargs):
        return SimpleNamespace(value=self.values[0][0] if self.values else "")


class Book:
    def __init__(self, books):
        self.sheets, self.books = {}, books

    def worksheet(self, title):
        if title not in self.sheets:
            raise WorksheetNotFound(title)
        self.sheets[title].spreadsheet = self
        return self.sheets[title]

    def add_worksheet(self, title, **kwargs):
        if title in self.sheets:
            raise AssertionError("Duplicate sheet creation")
        self.sheets[title] = Sheet(title, books=self.books)
        return self.worksheet(title)

    def fetch_sheet_metadata(self, params):
        title, cell_range = params["ranges"].split("!")
        sheet = self.worksheet(title.strip("'"))
        row = int(re.search(r"\d+", cell_range)[0]) - 1
        from battlelog_characters import entered
        cells = []
        for col, value in enumerate(sheet.values[row]):
            extra = sheet.formats[row][col] if row < len(sheet.formats) and col < len(sheet.formats[row]) else {}
            cells.append({**copy.deepcopy(extra), "userEnteredValue": {"formulaValue": value} if isinstance(value, str) and value.startswith("=") else entered(value)})
        return {"sheets": [{"data": [{"rowData": [{"values": cells}]}]}]}

    def batch_update(self, body):
        def target(sid):
            return next(sheet for sheet in self.sheets.values() if sheet.id == sid)
        def ensure(sheet, row, col):
            while len(sheet.values) <= row:
                sheet.values.append([])
            while len(sheet.formats) <= row:
                sheet.formats.append([])
            while len(sheet.values[row]) <= col:
                sheet.values[row].append("")
            while len(sheet.formats[row]) <= col:
                sheet.formats[row].append({})
        for request in body["requests"]:
            kind, data = next(iter(request.items()))
            if kind == "updateCells":
                start = data["start"]
                sheet = target(start["sheetId"])
                for i, row in enumerate(data["rows"]):
                    for j, cell in enumerate(row["values"]):
                        r, c = start["rowIndex"] + i, start["columnIndex"] + j
                        ensure(sheet, r, c)
                        value = cell.get("userEnteredValue", {})
                        sheet.values[r][c] = next(iter(value.values()), "")
                        if data["fields"] != "userEnteredValue":
                            sheet.formats[r][c] = {k:v for k,v in cell.items() if k != "userEnteredValue"}
            elif kind == "sortRange":
                area = data["range"]
                sheet = target(area["sheetId"])
                column = data["sortSpecs"][0]["dimensionIndex"]
                begin, end = area["startRowIndex"], area["endRowIndex"]
                rows = sorted(zip(sheet.values[begin:end], sheet.formats[begin:end]), key=lambda pair: pair[0][column])
                sheet.values[begin:end] = [row[0] for row in rows]
                sheet.formats[begin:end] = [row[1] for row in rows]
            elif kind == "repeatCell":
                area = data["range"]
                sheet = target(area["sheetId"])
                for row in range(area["startRowIndex"], area["endRowIndex"]):
                    for col in range(area["startColumnIndex"], area["endColumnIndex"]):
                        sheet.values[row][col] = ""
            elif kind in ("insertDimension", "deleteDimension"):
                area = data["range"]
                sheet = target(area["sheetId"])
                start = area["startIndex"]
                if kind == "insertDimension":
                    sheet.values.insert(start, [])
                    sheet.formats.insert(start, [])
                else:
                    del sheet.values[start:area["endIndex"]]
                    del sheet.formats[start:area["endIndex"]]
            elif kind == "copyPaste":
                source, dest = data["source"], data["destination"]
                sheet = target(dest["sheetId"])
                key = "userEnteredFormat" if data["pasteType"] == "PASTE_FORMAT" else "dataValidation"
                for row in range(dest["startRowIndex"], dest["endRowIndex"]):
                    for col in range(dest["startColumnIndex"], dest["endColumnIndex"]):
                        ensure(sheet, row, col)
                        src = source["startRowIndex"]
                        extra = sheet.formats[src][col] if col < len(sheet.formats[src]) else {}
                        if key in extra:
                            sheet.formats[row][col][key] = copy.deepcopy(extra[key])
            elif kind == "setDataValidation":
                area = data["range"]
                sheet = target(area["sheetId"])
                for row in range(area["startRowIndex"], area["endRowIndex"]):
                    for col in range(area["startColumnIndex"], area["endColumnIndex"]):
                        ensure(sheet, row, col)
                        sheet.formats[row][col]["dataValidation"] = copy.deepcopy(data["rule"])
            elif kind == "deleteRange":
                area = data["range"]
                sheet = target(area["sheetId"])
                column = area["startColumnIndex"]
                for row in range(area["startRowIndex"], len(sheet.values) - 1):
                    sheet.values[row][column] = sheet.values[row + 1][column]
                sheet.values[-1][column] = ""
            elif kind == "appendDimension":
                target(data["sheetId"]).col_count += data["length"]
            else:
                raise AssertionError("Unexpected native Sheets operation: " + kind)


def fixture_store():
    books = {}
    books.update({key: Book(books) for key in ("input", "output", "master")})
    state = initial_settings()
    books["input"].sheets[TAB] = Sheet(TAB, [HEADERS, ["initial", "2026-11-02T00:00:00+00:00", "initialized", "{}", json.dumps(state)]])
    headers = ["日付"] + [f"field{i}" for i in range(1, 19)] + ["ID", "GOOD", "BAD"]
    for book, title in (("input", "変換前_s11"), ("output", "出力結果_s11"), ("output", "変換前_s11")):
        books[book].sheets[title] = Sheet(title, [["戦績"], headers], books)
    books["master"].sheets["STRIKER"] = Sheet("STRIKER", [["キャラ名", "射程", "遮蔽", "攻撃属性", "アイコン"],
                                                                 ["ユウカ", "650", "TRUE", "爆発", "https://example.com/a.png"]])
    books["master"].sheets["SPECIAL"] = Sheet("SPECIAL", [["キャラ名", "アイコン"], ["ヒビキ", "https://example.com/b.png"]])
    books["output"].sheets["辞書"] = Sheet("辞書", [["ユウカ"], ["ヒビキ"]])
    books["input"].sheets[NOTICES_TAB] = Sheet(NOTICES_TAB, [NOTICE_HEADERS, ["legacy", "2026-10-07", "以前のお知らせ", "TRUE", 1, "legacy-revision", "2026-10-07T00:00:00+00:00"]])
    client = Mock()
    client.open_by_key.side_effect = lambda key: books[key]
    return AdminStore(lambda: client), books


class AdminFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("BATTLELOG_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"BATTLELOG_DATA_DIR": self.temp.name, "BATTLELOG_SKIP_DOTENV": "1",
                                          "BATTLELOG_ADMIN_SHEET_ID": "input", "BATTLELOG_SHEET_ID": "input",
                                          "OUTPUT_SHEET_ID": "output", "CHARDATA_SHEET_ID": "master",
                                          "BATTLELOG_JOURNAL_SHEET_ID": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Network disabled"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.store, self.books = fixture_store()

    def revision(self):
        return self.store.read(force=True)["revision"]


class AdminTests(AdminFixture):
    def test_character_edit_preserves_other_columns_and_is_recoverable(self):
        before = self.store.characters()[0]
        fields = dict(before, original=before["name"], image="https://example.com/new.png", range="750", cover="0", base_hash=character_hash(before))
        self.store.save_character(self.revision(), fields)
        row = self.books["master"].worksheet("STRIKER").values[1]
        self.assertEqual(row[:5], ["ユウカ", "750", False, "爆発", "https://example.com/new.png"])
        event = self.store.read(force=True)["events"][-1]
        self.assertTrue(all(event["details"]["before"][key] == value for key,value in before.items()))
        fields = dict(before, original=before["name"], cover="1", base_hash=character_hash(self.store.characters()[0]))
        self.store.save_character(self.revision(), fields)
        self.assertEqual(self.store.characters()[0], before)

    def test_duplicate_and_rename_are_rejected(self):
        fields = dict(kind="SPECIAL", name="ユウカ", image="https://example.com/a.png")
        with self.assertRaises(ValueError):
            self.store.save_character(self.revision(), fields)
        fields.update(original="別名", name="新しい名前")
        with self.assertRaises(ValueError):
            self.store.save_character(self.revision(), fields)

    def test_new_character_is_added_to_master_and_dictionary(self):
        fields = dict(kind="SPECIAL", name="新キャラ", reading="しんきゃら", image="https://example.com/new.png")
        self.store.save_character(self.revision(), fields)
        self.assertIn("新キャラ", self.books["output"].worksheet("辞書").col_values(1))
        self.assertEqual([c["name"] for c in self.store.characters()], ["ユウカ", "新キャラ", "ヒビキ"])
        with self.assertRaises(Conflict):
            self.store.save_character("initial", dict(kind="SPECIAL", name="別キャラ", reading="べつきゃら", image="https://example.com/a.png"))
        self.assertNotIn("別キャラ", self.books["master"].worksheet("SPECIAL").col_values(1))

    def test_invalid_image_url_never_writes_master_or_history(self):
        with self.assertRaises(ValueError):
            self.store.save_character(self.revision(), dict(kind="SPECIAL", name="新キャラ", image="javascript:alert(1)"))
        self.assertEqual(self.revision(), "initial")
        self.assertEqual(len(self.store.characters()), 2)

    def test_stale_character_form_cannot_overwrite_a_manual_sheet_edit(self):
        before = self.store.characters()[0]
        self.books["master"].worksheet("STRIKER").values[1][1] = "750"
        with self.assertRaises(Conflict):
            self.store.save_character(self.revision(), dict(before, original=before["name"], base_hash=character_hash(before)))

    def test_master_failure_leaves_a_pending_recovery_record(self):
        with patch.object(self.books["master"], "batch_update", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                self.store.save_character(self.revision(), dict(kind="SPECIAL", name="新キャラ", reading="しんきゃら", image="https://example.com/x.png"))
        self.assertEqual(self.store.read(force=True)["events"][-1]["action"], "character_pending")

    def test_prepare_is_idempotent_and_does_not_copy_battlelog_rows(self):
        self.books["input"].worksheet("変換前_s11").values.append(["existing battlelog"])
        self.assertEqual(self.store.prepare_season(self.revision(), "s12"), [])
        self.assertEqual(len(self.books["input"].worksheet("変換前_s12").values), 2)
        self.assertEqual(self.store.prepare_season(self.revision(), "s12"), [])
        self.assertEqual(self.books["input"].worksheet("変換前_s11").values[-1], ["existing battlelog"])
        formula = self.books["output"].worksheet("変換前_s12").values[0][0]
        self.assertEqual(formula, '=IMPORTRANGE("input","変換前_s12!A1:Z1000")')

    def test_existing_wrong_sheet_is_not_overwritten(self):
        self.books["input"].sheets["変換前_s12"] = Sheet("変換前_s12", [["do not change"]])
        with self.assertRaises(ValueError):
            self.store.prepare_season(self.revision(), "s12")
        self.assertEqual(self.books["input"].worksheet("変換前_s12").values, [["do not change"]])

    def test_distinct_input_and_output_labels_keep_the_same_id_positions(self):
        self.books["output"].worksheet("出力結果_s11").values[1][1] = "出力用の別の見出し"
        self.assertEqual(self.store.prepare_season(self.revision(), "s12"), [])
        headers = self.books["output"].worksheet("出力結果_s12").values[1]
        headers[19], headers[20] = headers[20], headers[19]
        self.assertTrue(self.store.check_season("s12"))

    def test_reservation_switches_exactly_at_japan_time_without_a_background_job(self):
        self.store.prepare_season(self.revision(), "s12")
        self.store.set_schedule(self.revision(), "s12", "2030-01-01T15:00", now=datetime(2029, 1, 1, tzinfo=timezone.utc))
        state = self.store.read(force=True)["state"]
        cutoff = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(effective_settings(state, cutoff - timedelta(microseconds=1))[0], "s11")
        self.assertEqual(effective_settings(state, cutoff)[0], "s12")
        self.assertNotIn("s12", [s["key"] for s in effective_settings(state, cutoff - timedelta(seconds=1))[1]])
        self.assertIn("s11", [s["key"] for s in effective_settings(state, cutoff)[1]])
        restarted, _ = fixture_store()
        restarted._books["input"] = self.books["input"]
        self.assertEqual(effective_settings(restarted.read(force=True)["state"], cutoff)[0], "s12")

    def test_invalid_unready_or_stale_reservations_do_not_save(self):
        for key in ("../s12", "s0", "s12345", "S12"):
            with self.assertRaises(ValueError):
                self.store.prepare_season(self.revision(), key)
        self.store.prepare_season(self.revision(), "s12")
        with self.assertRaises(ValueError):
            self.store.set_schedule(self.revision(), "s12", "2000-01-01T00:00")
        self.books["output"].worksheet("出力結果_s12").values = [["bad headers"]]
        with self.assertRaises(ValueError):
            self.store.set_schedule(self.revision(), "s12", "2030-01-01T15:00")
        self.assertIsNone(self.store.read(force=True)["state"]["schedule"])

    def test_cancel_reservation_preserves_current_season(self):
        self.store.prepare_season(self.revision(), "s12")
        self.store.set_schedule(self.revision(), "s12", "2030-01-01T15:00")
        self.store.cancel_schedule(self.revision())
        state = self.store.read(force=True)["state"]
        self.assertIsNone(state["schedule"])
        self.assertEqual(state["current"], "s11")

    def test_google_outage_uses_known_schedule_and_a_fresh_host_fails_closed(self):
        self.store.read(force=True)
        with patch.object(self.store, "read", side_effect=ConnectionError()):
            self.assertEqual(self.store.public()["current"], "s11")
        Path(self.temp.name, ".battlelog_runtime/admin-settings.json").unlink()
        Path(self.temp.name, ".battlelog_runtime/admin-settings.json.bak").unlink(missing_ok=True)
        restarted, _ = fixture_store()
        with patch.object(restarted, "read", side_effect=ConnectionError()):
            with self.assertRaises(RuntimeError):
                restarted.public()

    def test_kana_order_includes_voicing_and_kanji_readings(self):
        from battlelog_characters import gojuon_key
        names = ["ミカ", "御坂美琴", "カスミ", "カズサ", "シロコ", "食蜂操祈", "初音ミク", "佐天涙子"]
        self.assertEqual(sorted(names, key=gojuon_key), ["カズサ", "カスミ", "佐天涙子", "食蜂操祈", "シロコ", "初音ミク", "ミカ", "御坂美琴"])

    def test_new_character_is_sorted_with_boolean_checkbox_and_fullwidth_parentheses(self):
        fields = dict(kind="STRIKER", name="アコ(レーシング)(通常)", image="https://example.com/image(x).png", range="650", cover="0")
        self.store.save_character(self.revision(), fields)
        sheet = self.books["master"].worksheet("STRIKER")
        self.assertEqual(sheet.values[1][0], "アコ（レーシング）（通常）")
        self.assertIs(sheet.values[1][2], False)
        self.assertEqual(sheet.values[1][4], "https://example.com/image(x).png")
        self.assertEqual(sheet.formats[1][2]["dataValidation"]["condition"]["type"], "BOOLEAN")
        self.assertEqual([c["name"] for c in self.store.characters()], ["アコ（レーシング）（通常）", "ユウカ", "ヒビキ"])
        self.assertEqual(self.books["output"].worksheet("辞書").col_values(1)[:3], ["アコ（レーシング）（通常）", "ユウカ", "ヒビキ"])

    def test_delete_and_restore_preserve_attributes_formulas_notes_and_formats(self):
        sheet = self.books["master"].worksheet("STRIKER")
        sheet.values[1][3] = "=1+1"
        sheet.formats[1][3] = {"note":"keep this note", "userEnteredFormat":{"backgroundColor":{"red":1}}}
        before = self.store.characters()[0]
        battlelogs = copy.deepcopy(self.books["input"].worksheet("変換前_s11").values)
        self.store.delete_character(self.revision(), "STRIKER", "ユウカ", character_hash(before))
        self.assertNotIn("ユウカ", [c["name"] for c in self.store.characters()])
        self.assertNotIn("ユウカ", self.books["output"].worksheet("辞書").col_values(1))
        event = self.store.read(force=True)["events"][-1]
        self.store.restore_deleted_character(self.revision(), event["revision"])
        restored = next(c for c in self.store.characters() if c["name"] == "ユウカ")
        self.assertEqual(sheet.values[restored["row"]-1][3], "=1+1")
        self.assertEqual(sheet.formats[restored["row"]-1][3]["note"], "keep this note")
        self.assertEqual(sheet.formats[restored["row"]-1][3]["userEnteredFormat"], {"backgroundColor":{"red":1}})
        self.assertEqual(battlelogs, self.books["input"].worksheet("変換前_s11").values)
        with self.assertRaises(Conflict):
            self.store.restore_deleted_character(self.revision(), event["revision"])

    def test_partial_delete_can_be_restored_from_its_start_record(self):
        before = self.store.characters()[0]
        with patch.object(self.books["output"], "batch_update", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                self.store.delete_character(self.revision(), "STRIKER", "ユウカ", character_hash(before))
        event = self.store.read(force=True)["events"][-1]
        self.assertEqual(event["action"], "character_delete_pending")
        self.store.restore_deleted_character(self.revision(), event["revision"])
        self.assertIn("ユウカ", [c["name"] for c in self.store.characters()])

    def test_stale_deletion_does_not_change_rows(self):
        with self.assertRaises(Conflict):
            self.store.delete_character(self.revision(), "STRIKER", "ユウカ", "old-hash")
        self.assertEqual(len(self.store.characters()), 2)

    def test_dictionary_keeps_extra_legacy_candidates_and_other_columns(self):
        dictionary = self.books["output"].worksheet("辞書")
        dictionary.values = [["ヒビキ", "keep"], ["旧表記"], ["ユウカ"]]
        self.store.sync_dictionary()
        self.assertEqual(dictionary.col_values(1), ["ユウカ", "ヒビキ", "旧表記"])
        self.assertEqual(dictionary.values[0][1], "keep")

    def test_notice_drafts_publish_edit_restore_and_fullwidth_parentheses(self):
        fields = {"date":"2026-11-03", "text":"新しい(お知らせ)(確認)", "published":"0"}
        added = self.store.save_notice(self.revision(), fields)
        self.assertEqual(added["text"], "新しい（お知らせ）（確認）")
        self.assertNotIn(added["id"], [n["id"] for n in self.store.public_notices()])
        fields.update(id=added["id"], base_revision=added["revision"], published="1")
        published = self.store.save_notice(self.revision(), fields)
        self.assertEqual(self.store.public_notices()[0]["id"], published["id"])
        event = self.store.read(force=True)["events"][-1]
        self.store.restore_notice(self.revision(), event["revision"])
        self.assertNotIn(added["id"], [n["id"] for n in self.store.public_notices()])

    def test_hidden_notices_do_not_fall_back_to_old_published_html(self):
        with patch("battlelog_notices.time.monotonic", return_value=10):
            item = self.store.notices(force=True)[0]
            self.store.save_notice(self.revision(), {**item, "base_revision":item["revision"], "published":"0"})
            self.assertEqual(self.store.public_notices(), [])

    def test_legacy_notice_import_retains_existing_paragraphs(self):
        from battlelog_notices import legacy_notices
        items = legacy_notices()
        self.assertEqual(len(items), 44)
        self.assertEqual(items[0]["date"], "2026-10-07")
        self.assertIn("ツムギ", items[0]["text"])
        self.assertTrue(all("<" not in item["text"] for item in items))


class AdminWebTests(AdminFixture):
    def setUp(self):
        super().setUp()
        from Blue_Archive_Battlelog_collect_public.app import app
        self.app = app
        self.config = patch.dict(app.config, SECRET_KEY="offline-test-key", TESTING=True)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.login_env = patch.dict(os.environ, BATTLELOG_ADMIN_PASSWORD_HASH=generate_password_hash("offline-test-password", method="pbkdf2:sha256:1000"))
        self.login_env.start()
        self.addCleanup(self.login_env.stop)
        for module in ("battlelog_admin", "battlelog_admin_store"):
            patched = patch(module + ".store", self.store)
            patched.start()
            self.addCleanup(patched.stop)
        patched = patch("Blue_Archive_Battlelog_collect_public.app.management_store", self.store)
        patched.start()
        self.addCleanup(patched.stop)
        self.client = app.test_client()

    def get(self, path):
        return self.client.get(path, base_url="https://localhost")

    def post(self, path, data):
        return self.client.post(path, data=data, base_url="https://localhost")

    def login(self):
        response = self.get("/admin/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', response.get_data(as_text=True))[1]
        self.assertEqual(self.post("/admin/login", {"csrf": csrf, "password": "offline-test-password"}).status_code, 302)
        home = self.get("/admin/")
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', home.get_data(as_text=True))[1]

    def test_unauthenticated_access_csrf_and_password_rotation(self):
        self.assertEqual(self.get("/admin/characters").status_code, 302)
        self.assertEqual(self.post("/admin/seasons/prepare", {"season": "s12"}).status_code, 400)
        self.login()
        self.assertEqual(self.get("/admin/characters").status_code, 200)
        with patch.dict(os.environ, BATTLELOG_ADMIN_PASSWORD_HASH="rotated"):
            self.assertEqual(self.get("/admin/characters").status_code, 302)

    def test_submission_timestamp_strings_are_displayed_in_japan_time(self):
        from battlelog_admin import admin_time
        self.assertEqual(admin_time("0"), "1970/01/01 09:00")
        self.assertEqual(admin_time("2030-01-01T06:00:00+00:00"), "2030/01/01 15:00")

    def test_admin_pages_render_and_are_not_cached_or_framed(self):
        self.login()
        for path in ("/admin/", "/admin/characters", "/admin/characters?kind=STRIKER&name=ユウカ", "/admin/seasons", "/admin/notices", "/admin/history", "/admin/status"):
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_authenticated_forms_prepare_schedule_cancel_and_public_upload(self):
        self.login()
        form = {"csrf": self.csrf, "revision": self.revision(), "season": "s12"}
        self.assertEqual(self.post("/admin/seasons/prepare", form).status_code, 302)
        form.update(revision=self.revision(), at="2030-01-01T15:00")
        self.assertEqual(self.post("/admin/seasons/schedule", form).status_code, 302)
        response = self.get("/upload")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('value="s12"', response.get_data(as_text=True))
        form.update(revision=self.revision())
        self.assertEqual(self.post("/admin/seasons/cancel", form).status_code, 302)

    def test_public_upload_uses_effective_reserved_season(self):
        self.store.prepare_season(self.revision(), "s12")
        state = self.store.read(force=True)["state"]
        state["schedule"] = {"season": "s12", "at": "2000-01-01T00:00:00+00:00"}
        with patch.object(self.store, "public", return_value=state):
            response = self.get("/upload")
        self.assertIn('value="s12"', response.get_data(as_text=True))

    def test_old_confirmation_keeps_its_season_after_a_reservation_has_applied(self):
        from battlelog_submission import issue_token
        from Blue_Archive_Battlelog_collect_public import app as module
        token = issue_token("general", "s11")
        state = self.store.read(force=True)["state"]
        state["prepared"] = ["s12"]
        state["schedule"] = {"season": "s12", "at": "2000-01-01T00:00:00+00:00"}
        data = {"season":"s11", "confirmation_token":token, **{f"field{i}":"" for i in range(18)}}
        with patch.object(self.store, "public", return_value=state), patch.object(module, "update_spreadsheet", return_value="12") as insert, patch.object(module, "fetch_latest_output_row_as_dict", return_value={"ID":"12"}), patch.object(module, "append_battlelog_row_from_api"), patch("battlelog_web.wait_for_input"), patch("battlelog_web.run_conversion"):
            response = self.post("/upload/confirm", data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(insert.call_args.kwargs["season"], "s11")

    def test_missing_settings_on_a_new_host_does_not_fall_back_to_the_wrong_season(self):
        with patch.object(self.store, "public", side_effect=ConnectionError()):
            self.assertEqual(self.get("/upload").status_code, 503)

    def test_delete_form_and_history_restore_are_authenticated(self):
        self.login()
        before = self.store.characters()[0]
        form = {"csrf":self.csrf,"revision":self.revision(),"kind":"STRIKER","name":"ユウカ","base_hash":character_hash(before)}
        with patch("battlelog_admin.refresh_characters"):
            self.assertEqual(self.post("/admin/characters/delete",form).status_code,302)
        event = self.store.read(force=True)["events"][-1]
        self.assertIn("削除前のキャラを復元",self.get("/admin/history").get_data(as_text=True))
        form.update(revision=self.revision(),event=event["revision"])
        self.assertEqual(self.post("/admin/history/restore-deleted",form).status_code,302)

    def test_notice_form_updates_public_fragment_without_executing_html(self):
        self.login()
        form = {"csrf":self.csrf,"revision":self.revision(),"date":"2026-11-03","text":"告知(test)<script>alert(1)</script>","published":"1"}
        self.assertEqual(self.post("/admin/notices/save",form).status_code,302)
        body = self.get("/notices").get_data(as_text=True)
        self.assertIn("告知（test）",body)
        self.assertIn("&lt;script&gt;",body)
        self.assertNotIn("<script>",body)


if __name__ == "__main__":
    unittest.main()
