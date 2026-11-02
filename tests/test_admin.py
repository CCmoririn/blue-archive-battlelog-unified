"""Offline management regressions: authentication, reservations and recoverable writes."""
import copy
import io
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


class Sheet:
    def __init__(self, title, values=None, books=None):
        self.title, self.values, self.books = title, copy.deepcopy(values or []), books

    def get_all_values(self):
        return copy.deepcopy(self.values)

    def append_row(self, row, **kwargs):
        self.values.append(list(row))

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
        return self.sheets[title]

    def add_worksheet(self, title, **kwargs):
        if title in self.sheets:
            raise AssertionError("Duplicate sheet creation")
        self.sheets[title] = Sheet(title, books=self.books)
        return self.sheets[title]


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
        self.assertEqual(row, ["ユウカ", "750", "FALSE", "爆発", "https://example.com/new.png"])
        event = self.store.read(force=True)["events"][-1]
        self.assertEqual(event["details"]["before"], before)
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
        fields = dict(kind="SPECIAL", name="新キャラ", image="https://example.com/new.png")
        self.store.save_character(self.revision(), fields)
        self.assertIn("新キャラ", self.books["output"].worksheet("辞書").col_values(1))
        self.assertEqual(self.store.characters()[-1]["name"], "新キャラ")
        with self.assertRaises(Conflict):
            self.store.save_character("initial", dict(kind="SPECIAL", name="別キャラ", image="https://example.com/a.png"))
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
        with patch.object(self.books["master"].worksheet("SPECIAL"), "append_row", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                self.store.save_character(self.revision(), dict(kind="SPECIAL", name="新キャラ", image="https://example.com/x.png"))
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
        for path in ("/admin/", "/admin/characters", "/admin/characters?kind=STRIKER&name=ユウカ", "/admin/seasons", "/admin/history", "/admin/status"):
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


if __name__ == "__main__":
    unittest.main()
