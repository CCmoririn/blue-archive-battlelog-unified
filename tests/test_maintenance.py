"""Offline regressions. No real credentials, network calls, OCR or Sheets writes."""

import base64
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Blue_Archive_Battlelog_collect_public"))
os.environ["BATTLELOG_SKIP_DOTENV"] = "1"

from battlelog_safety import OutputNotReady, atomic_json, file_lock, read_json, validate_search, validate_season
from battlelog_storage import SheetStore, records_from_values
from battlelog_submission import SubmissionPending, issue_token, submit

SEASONS = [{"key": "s9"}, {"key": "s10"}, {"key": "s11"}]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGJ8AAAAASUVORK5CYII=")


class FakeSheet:
    def __init__(self, headers, rows=()):
        self.values = [["title"], list(headers)] + [list(row) for row in rows]
        self.insertions = []
        self.updates = []
        self.fail_update = False

    def get_all_values(self):
        return [row[:] for row in self.values]

    def row_values(self, row):
        return self.values[row - 1][:]

    def col_values(self, col):
        return [row[col - 1] if len(row) >= col else "" for row in self.values]

    def insert_row(self, values, index, **kwargs):
        self.insertions.append((values[:], index, kwargs))
        self.values.insert(index - 1, values[:])

    def update_cell(self, row, col, value):
        if self.fail_update:
            raise ConnectionError("simulated remote failure")
        self.updates.append((row, col, value))
        while len(self.values[row - 1]) < col:
            self.values[row - 1].append("")
        self.values[row - 1][col - 1] = value


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="battlelog-tests-", dir=os.environ.get("BATTLELOG_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"BATTLELOG_DATA_DIR": self.temp.name,
                                          "BATTLELOG_SKIP_DOTENV": "1",
                                          "BATTLELOG_SHEET_ID": "input-test",
                                          "OUTPUT_SHEET_ID": "output-test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Tests fail if any unexpected external request is attempted.
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Network is disabled in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)


class CacheTests(IsolatedTest):
    def store(self, sheet=None):
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = sheet
        return SheetStore("cache_general", "s11", SEASONS, "BATTLELOG_SHEET_ID", "OUTPUT_SHEET_ID", lambda: client)

    def test_season_cannot_escape_directory(self):
        for season in ("../private", "s12", [], 11, False):
            with self.assertRaises(ValueError):
                self.store().path(season)

    def test_empty_cache_does_not_fetch_sheets_repeatedly(self):
        store = self.store()
        store.save("s11", [])
        with patch.object(store, "refresh", side_effect=AssertionError("unwanted refresh")):
            self.assertEqual(store.get("s11"), [])

    def test_corrupt_cache_recovers_last_valid_snapshot(self):
        path = Path(self.temp.name) / "cache.json"
        atomic_json(path, [{"ID": "1"}])
        atomic_json(path, [{"ID": "2"}])
        path.write_text("broken", encoding="utf-8")
        self.assertEqual(read_json(path), [{"ID": "1"}])
        atomic_json(path, [{"ID": "3"}])
        self.assertEqual(json.loads(Path(str(path) + ".bak").read_text()), [{"ID": "1"}])

    def test_corrupt_cache_without_backup_is_not_silently_regenerated(self):
        store = self.store()
        store.path("s11").parent.mkdir(parents=True)
        store.path("s11").write_text("broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            store.get("s11")
        self.assertEqual(store.path("s11").read_text(), "broken")

    def test_missing_primary_can_use_backup(self):
        path = Path(self.temp.name) / "cache.json"
        Path(str(path) + ".bak").write_text('[{"ID":"1"}]')
        self.assertEqual(read_json(path), [{"ID": "1"}])

    def test_backup_failure_must_not_replace_current_cache(self):
        path = Path(self.temp.name) / "cache.json"
        atomic_json(path, [{"ID": "1"}])
        with patch("battlelog_safety.shutil.copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_json(path, [{"ID": "2"}])
        self.assertEqual(read_json(path), [{"ID": "1"}])

    def test_append_is_idempotent_and_does_not_mutate_callers(self):
        store = self.store()
        store.save("s11", [])
        row = {"ID": "1", "日付": "2026-11-02 10:00:00"}
        store.append(row, "s11")
        store.append(row, "s11")
        self.assertEqual(len(store.get("s11")), 1)
        self.assertNotIn("source", row)

    def test_concurrent_cache_appends_are_not_lost(self):
        store = self.store()
        store.save("s11", [])
        def append(index):
            store.append({"ID": str(index), "日付": "2026-11-02 10:00:00"}, "s11")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(1, 25)))
        self.assertEqual(len(store.get("s11")), 24)

    def test_cross_process_lock_protects_read_modify_write(self):
        path = Path(self.temp.name) / "counter.json"
        atomic_json(path, [{"count": 0}])
        code = """import sys
from pathlib import Path
from battlelog_safety import file_lock, read_json, atomic_json
path=Path(sys.argv[1])
for _ in range(12):
    with file_lock('counter'):
        rows=read_json(path)
        rows[0]['count'] += 1
        atomic_json(path,rows)
"""
        children = [subprocess.Popen([sys.executable, "-B", "-c", code, str(path)], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for child in children:
            output, error = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, error.decode(errors="replace"))
        self.assertEqual(read_json(path)[0]["count"], 36)

    def test_fetch_finds_submitted_id_instead_of_latest_row(self):
        sheet = FakeSheet(["日付", "ID"], [["2026-11-02 10:00:01", "22"], ["2026-11-02 10:00:00", "21"]])
        self.assertEqual(self.store(sheet).fetch("s11", expected_id="21")["ID"], "21")

    def test_fetch_rejects_ambiguous_or_missing_id(self):
        sheet = FakeSheet(["ID", "A1"], [["21", "ユウカ"], ["21", "シュン"]])
        for identifier in ("21", "22"):
            with self.assertRaises(RuntimeError):
                self.store(sheet).fetch("s11", expected_id=identifier)

    def test_identical_existing_duplicates_are_collapsed_without_changing_sheets(self):
        sheet = FakeSheet(["ID", "A1", "GOOD", "BAD"], [["21", "ユウカ", "5", "0"], ["21", "ユウカ", "0", "0"]])
        store = self.store(sheet)
        store.save("s11", [{"ID": "21", "A1": "ユウカ", "GOOD": "5", "BAD": "0"},
                            {"ID": "21", "A1": "ユウカ", "GOOD": "0", "BAD": "0"}])
        self.assertEqual(len(store.get("s11")), 1)
        self.assertEqual(store.fetch("s11", expected_id="21")["GOOD"], "5")
        self.assertTrue(store.vote("21", "GOOD", 1))
        self.assertEqual(sheet.updates, [(3, 3, "6")])
        self.assertEqual(len(sheet.values), 4, "no remote duplicate rows are deleted")

    def test_insert_allocates_id_and_preserves_raw_cell_values(self):
        headers = [f"field{i}" for i in range(18)] + ["", "ID", "GOOD", "BAD"]
        sheet = FakeSheet(headers, [[""] * 19 + ["７", "1", "2"]])
        fields = ["=not_a_formula"] + [""] * 17
        self.assertEqual(self.store(sheet).insert(fields, "s11"), "8")
        row, index, options = sheet.insertions[0]
        self.assertEqual((row[0], row[19:22], index, options["value_input_option"]),
                         ("=not_a_formula", ["8", "0", "0"], 3, "RAW"))

    def test_vote_uses_authoritative_sheet_count(self):
        sheet = FakeSheet(["ID", "GOOD", "BAD"], [["1", "8", "2"]])
        store = self.store(sheet)
        store.save("s9", [{"ID": "1", "GOOD": "1", "BAD": "2"}])
        self.assertTrue(store.vote("1", "GOOD", 1, "s9"))
        self.assertEqual(store.get("s9")[0]["GOOD"], "9")
        self.assertEqual(sheet.updates, [(3, 2, "9")])

    def test_failed_vote_does_not_alter_cache(self):
        sheet = FakeSheet(["ID", "GOOD", "BAD"], [["1", "8", "2"]])
        sheet.fail_update = True
        store = self.store(sheet)
        store.save("s11", [{"ID": "1", "GOOD": "1"}])
        with self.assertRaises(ConnectionError):
            store.vote("1", "GOOD", 1)
        self.assertEqual(store.get()[0]["GOOD"], "1")

    def test_vote_rejects_duplicate_ids_without_writing(self):
        sheet = FakeSheet(["ID", "GOOD"], [["1", "8"]])
        store = self.store(sheet)
        store.save("s11", [{"ID": "1", "A1": "ユウカ"}, {"ID": "1", "A1": "シュン"}])
        self.assertFalse(store.vote("1", "GOOD", 1))
        self.assertFalse(sheet.updates)

    def test_vote_rejects_a_record_changed_since_the_cached_search(self):
        sheet = FakeSheet(["ID", "A1", "GOOD"], [["1", "シュン", "8"]])
        store = self.store(sheet)
        store.save("s11", [{"ID": "1", "A1": "ユウカ", "GOOD": "1"}])
        self.assertFalse(store.vote("1", "GOOD", 1))
        self.assertFalse(sheet.updates)

    def test_duplicate_headers_and_short_rows_are_safe(self):
        self.assertEqual(records_from_values([["title"], ["勝敗", "", "勝敗"], ["Win"]]),
                         [{"勝敗": "Win", "空欄": "", "勝敗_2": ""}])

    def test_refresh_preserves_explicit_cache_only_rows(self):
        from gspread.exceptions import WorksheetNotFound
        sheet = FakeSheet(["日付", "ID"], [["2026-11-02 10:00:00", "1"]])
        store = self.store(sheet)
        client = Mock()
        client.open_by_key.return_value.worksheet.side_effect = [sheet, WorksheetNotFound()]
        store.client_factory = lambda: client
        store.save("s11", [{"日付": "2026-11-02 9:00:00", "ID": "2", "source": "一般", "_cache_only": True}])
        self.assertEqual(len(store.refresh("s11")), 2)

    def test_failed_refresh_leaves_existing_cache_untouched(self):
        store = self.store()
        store.save("s11", [{"ID": "1"}])
        store.client_factory = Mock(side_effect=ConnectionError("offline"))
        with self.assertRaises(ConnectionError):
            store.refresh()
        self.assertEqual(store.get(), [{"ID": "1"}])


class SubmissionTests(IsolatedTest):
    fields = ["2026-11-02 10:00:00"] + [""] * 17

    def run_submit(self, token, **overrides):
        callbacks = dict(insert=Mock(return_value="12"), convert=Mock(),
                         fetch=Mock(return_value={"ID": "12"}), append=Mock())
        callbacks.update(overrides)
        submit(token, "general", "s11", self.fields, **callbacks)
        return callbacks

    def test_repeated_confirmation_does_not_insert_twice(self):
        token = issue_token("general", "s11")
        self.run_submit(token)
        callbacks = self.run_submit(token)
        for callback in callbacks.values():
            callback.assert_not_called()

    def test_insert_timeout_is_not_retried_blindly(self):
        token = issue_token("general", "s11")
        insert = Mock(side_effect=ConnectionError("unknown outcome"))
        with self.assertRaises(SubmissionPending):
            self.run_submit(token, insert=insert)
        with self.assertRaises(SubmissionPending):
            self.run_submit(token, insert=insert)
        self.assertEqual(insert.call_count, 1)

    def test_conversion_failure_does_not_repeat_remote_insert(self):
        token = issue_token("general", "s11")
        insert = Mock(return_value="12")
        with self.assertRaises(SubmissionPending):
            self.run_submit(token, insert=insert, convert=Mock(side_effect=TimeoutError()))
        with self.assertRaises(SubmissionPending):
            self.run_submit(token, insert=insert)
        self.assertEqual(insert.call_count, 1)

    def test_cache_failure_can_resume_without_inserting_or_converting(self):
        token = issue_token("general", "s11")
        with self.assertRaises(OSError):
            self.run_submit(token, append=Mock(side_effect=OSError("disk full")))
        callbacks = self.run_submit(token)
        callbacks["insert"].assert_not_called()
        callbacks["convert"].assert_not_called()
        callbacks["fetch"].assert_called_once_with(season="s11", expected_id="12", expected_row=self.fields)

    def test_token_is_bound_to_season_and_app(self):
        token = issue_token("limited", "s9")
        with self.assertRaises(ValueError):
            self.run_submit(token)

    def test_delayed_output_retries_reads_without_repeating_writes(self):
        token = issue_token("general", "s11")
        fetch = Mock(side_effect=[OutputNotReady(), {"ID": "12"}])
        with patch("battlelog_submission.time.sleep") as sleep:
            callbacks = self.run_submit(token, fetch=fetch)
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(1)
        callbacks["insert"].assert_called_once()
        callbacks["convert"].assert_called_once()
        callbacks["append"].assert_called_once()

    def test_output_retry_limit_keeps_the_resumable_state(self):
        token = issue_token("general", "s11")
        fetch = Mock(side_effect=OutputNotReady())
        with patch("battlelog_submission.time.sleep"):
            with self.assertRaises(OutputNotReady):
                self.run_submit(token, fetch=fetch)
        self.assertEqual(fetch.call_count, 4)
        callbacks = self.run_submit(token)
        callbacks["insert"].assert_not_called()
        callbacks["convert"].assert_not_called()

    def test_input_mirror_delay_can_resume_without_another_insert(self):
        token = issue_token("general", "s11")
        ready = Mock(side_effect=OutputNotReady())
        with self.assertRaises(OutputNotReady):
            self.run_submit(token, ready=ready)
        callbacks = self.run_submit(token)
        callbacks["insert"].assert_not_called()
        callbacks["convert"].assert_called_once()


class RemoteJournalTests(SubmissionTests):
    def setUp(self):
        super().setUp()
        import battlelog_journal as journal
        self.env_remote = patch.dict(os.environ, {"BATTLELOG_JOURNAL_SHEET_ID": "journal-test"})
        self.env_remote.start()
        self.addCleanup(self.env_remote.stop)
        self.rows = [journal.HEADERS[:]]
        sheet = Mock()
        sheet.get_all_values.side_effect = lambda: [row[:] for row in self.rows]
        sheet.append_row.side_effect = lambda values, **kwargs: self.rows.append(values[:])

        def update(cell_range, values, **kwargs):
            index = int(cell_range.split(":")[0][1:]) - 1
            self.rows[index] = values[0][:]
        sheet.update.side_effect = update
        self.journal_patch = patch.object(journal, "worksheet", return_value=sheet)
        self.journal_patch.start()
        self.addCleanup(self.journal_patch.stop)

    def test_restart_without_local_files_does_not_repeat_a_completed_write(self):
        token = issue_token("general", "s11")
        self.run_submit(token)
        self.assertFalse((Path(self.temp.name) / ".battlelog_runtime/submissions.sqlite3").exists())
        callbacks = self.run_submit(token)
        for callback in callbacks.values():
            callback.assert_not_called()
        self.assertNotIn(token, json.dumps(self.rows))

    def test_unavailable_journal_prevents_any_remote_insert(self):
        import battlelog_journal as journal
        token = issue_token("general", "s11")
        insert = Mock()
        with patch.object(journal, "update", side_effect=ConnectionError()):
            with self.assertRaises(ConnectionError):
                self.run_submit(token, insert=insert)
        insert.assert_not_called()


class OcrOutputTests(IsolatedTest):
    def test_player_names_are_not_returned_or_logged_for_either_attack_side(self):
        for folder, filename in (("Blue_Archive_Battlelog_collect_public", "main.py"),
                                 ("Blue_Archive_Battlelog_collect", "main_limited.py")):
            for left_sword in (True, False):
                with self.subTest(processor=filename, left_sword=left_sword):
                    cv2 = types.ModuleType("cv2")
                    cv2.imwrite = Mock()
                    cv2.rectangle = Mock()
                    ocr = types.ModuleType("ocr_processing")
                    header = "Lv.90 private-left\nWin\nLv.90 private-right\nLose\nVS"
                    characters = [f"character-{i}" for i in range(12)]
                    ocr.perform_google_vision_ocr = Mock(side_effect=[header] + characters)
                    stubs = {"cv2": cv2, "numpy": types.ModuleType("numpy"), "ocr_processing": ocr}
                    spec = importlib.util.spec_from_file_location("ocr_output_under_test", ROOT / folder / filename)
                    processor = importlib.util.module_from_spec(spec)
                    with patch.dict(sys.modules, stubs), \
                         patch.object(sys, "path", sys.path + [str(ROOT / "Blue_Archive_Battlelog_collect")]):
                        spec.loader.exec_module(processor)
                        with patch.object(processor, "preprocess_image", return_value=MagicMock()), \
                             patch.object(processor, "load_template"), \
                             patch.object(processor, "match_icon", return_value=left_sword), \
                             patch.object(processor.os, "remove"), patch("builtins.print") as output:
                            row = processor.process_image("unused.png", season="s11")
                    expected_results = ("Win", "Lose") if left_sword else ("Lose", "Win")
                    self.assertEqual(len(row), 18)
                    self.assertEqual((row[1], row[10]), ("", ""))
                    self.assertEqual((row[2], row[11]), expected_results)
                    self.assertEqual(row[3:9] + row[12:18], characters)
                    self.assertEqual(row[9], "")
                    self.assertEqual(ocr.perform_google_vision_ocr.call_count, 13)
                    self.assertNotIn("private-left", str(output.call_args_list))
                    self.assertNotIn("private-right", str(output.call_args_list))


class WebTests(IsolatedTest):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("Blue_Archive_Battlelog_collect_public.app")
        cls.module.app.config["TESTING"] = True
        cls.manager = importlib.import_module("spreadsheet_manager")

    def setUp(self):
        super().setUp()
        self.client = self.module.app.test_client()

    def test_malformed_search_is_400_not_500(self):
        cases = [[], {}, {"side": "attack", "characters": ["x"], "ranges": []},
                 {"side": "defense", "characters": [""] * 6, "ranges": [1] * 4},
                 {"side": "attack", "characters": ["x"] + [""] * 5, "season": "../s11"}]
        for data in cases:
            response = self.client.post("/api/search", json=data)
            self.assertEqual(response.status_code, 400, data)

    def test_search_returns_opposing_winner_and_handles_empty_votes(self):
        rows = [{"勝敗": "Win", "勝敗_2": "Lose", "日付": "2026-11-02 10:00:00", "GOOD": "", "BAD": "x", "ID": "1"}]
        with patch.object(self.module, "search_battlelog_output_sheet", return_value=rows), patch.object(self.module, "get_other_icon", return_value=""):
            response = self.client.post("/api/search", json={"side": "defense", "characters": ["ユウカ"] + [""] * 5, "season": "s9"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["results"][0]["winner_type"], "attack")
        self.assertEqual(response.json["results"][0]["good_count"], 0)

    def test_past_season_vote_is_forwarded_and_missing_season_is_rejected(self):
        vote = importlib.import_module("vote_api")
        payload = {"row_id": "12", "vote_type": "good", "current_status": False, "season": "s9"}
        with patch.object(vote, "update_vote_count", return_value=True) as update, patch.object(vote, "get_output_sheet_cache", return_value=[{"ID": "12", "GOOD": "1"}]):
            self.assertEqual(self.client.post("/api/vote", json=payload).status_code, 200)
            update.assert_called_once_with("12", "GOOD", 1, "s9")
            payload.pop("season")
            self.assertEqual(self.client.post("/api/vote", json=payload).status_code, 400)
            self.assertEqual(update.call_count, 1)

    def test_non_image_rejected_before_ocr(self):
        response = self.client.post("/upload", data={"image_file": (io.BytesIO(b"not an image"), "../main.py")})
        self.assertEqual(response.status_code, 400)
        self.assertFalse((Path(self.temp.name) / "uploads").exists())

    def test_safe_unique_upload_keeps_exact_image_bytes_and_confirmation_fields(self):
        fake = types.ModuleType("main")
        row = ["2026-11-02 10:00:00", "private-attacker", "Win"]
        row += [f"attack-{i}" for i in range(6)] + ["", "private-defender", "Lose"]
        row += [f"defense-{i}" for i in range(6)]
        fake.process_image = Mock(return_value=row)
        with patch.dict(sys.modules, {"main": fake}):
            responses = [self.client.post("/upload", data={"image_file": (io.BytesIO(PNG), "../../main.py")}) for _ in range(2)]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        files = list((Path(self.temp.name) / "uploads").iterdir())
        self.assertEqual(len(files), 2)
        self.assertTrue(all(file.suffix == ".png" and file.read_bytes() == PNG for file in files))
        self.assertIn(b'name="confirmation_token"', responses[0].data)
        self.assertIn(b'name="season" value="s11"', responses[0].data)
        for i, value in enumerate(row):
            if i in (1, 10):
                self.assertNotIn(f'name="field{i}"'.encode(), responses[0].data)
                self.assertNotIn(value.encode(), responses[0].data)
            else:
                self.assertIn(f'name="field{i}" value="{value}"'.encode(), responses[0].data)
        self.assertEqual(fake.process_image.call_count, 2)

    def test_confirmation_discards_player_names_without_shifting_other_fields(self):
        for include_names in (False, True):
            with self.subTest(old_form=include_names):
                fields = [f"value-{i}" for i in range(18)]
                fields[0], fields[2], fields[11] = "2026-11-03 10:00:00", "Win", "Lose"
                token = issue_token("general", "s11")
                form = {f"field{i}": value for i, value in enumerate(fields)
                        if include_names or i not in (1, 10)}
                form.update(season="s11", confirmation_token=token)
                fields[1] = fields[10] = ""
                with patch.object(self.module, "update_spreadsheet", return_value="12") as insert, \
                     patch.object(self.module, "fetch_latest_output_row_as_dict", return_value={}), \
                     patch.object(self.module, "append_battlelog_row_from_api"), \
                     patch("battlelog_web.wait_for_input"), patch("battlelog_web.run_conversion"):
                    response = self.client.post("/upload/confirm", data=form)
                self.assertEqual(response.status_code, 302)
                insert.assert_called_once_with(fields, season="s11")

    def test_size_limit_prevents_ocr(self):
        with patch.dict(self.module.app.config, {"MAX_CONTENT_LENGTH": 128}):
            response = self.client.post("/upload", data={"image_file": (io.BytesIO(PNG * 10), "x.png")})
        self.assertEqual(response.status_code, 413)

    def test_confirmation_without_valid_token_never_writes_sheets(self):
        with patch.object(self.module, "update_spreadsheet") as insert:
            response = self.client.post("/upload/confirm", data={"season": "s11"})
        self.assertEqual(response.status_code, 400)
        insert.assert_not_called()

    def test_upload_preview_is_nosniff_and_not_cached(self):
        folder = Path(self.temp.name) / "uploads"
        folder.mkdir()
        (folder / "x.png").write_bytes(PNG)
        response = self.client.get("/uploads/x.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()

    def test_template_detail_rejects_invalid_shapes(self):
        response = self.client.post("/api/template_detail", json={"template_tags": [[650, "false"]] * 4, "attacks": [[""] * 6]})
        self.assertEqual(response.status_code, 400)

    def test_ads_and_health_do_not_require_google(self):
        self.assertEqual(self.client.get("/api/vote/health").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        response = self.client.get("/ads.txt")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_character_and_sp_search_semantics(self):
        rows = [dict(A1="シロコ＊テラー", A2="ユウカ", A3="シュン", A4="エイミ", ASP1="アコ", ASP2="イロハ")]
        master = [{"name": "シロコ＊テラー", "射程": 650, "遮蔽": True}]
        with patch.object(self.manager, "get_output_sheet_cache", return_value=rows), patch.object(self.manager, "get_striker_list_from_sheet", return_value=master) as load:
            self.assertEqual(self.manager.search_battlelog_output_sheet(["シロコ*テラー", "", "", "", "イロハ", "アコ"], "attack"), rows)
            self.assertEqual(self.manager.search_battlelog_output_sheet(["", "シロコ*テラー", "", "", "", ""], "attack"), [])
            self.assertEqual(self.manager.search_battlelog_output_sheet([""] * 6, "attack", ranges=["650", "", "", ""], covers=["true", "", "", ""]), rows)
            self.assertEqual(load.call_count, 1)

    def test_search_page_renders_season_scoped_votes_and_escaped_dates(self):
        with patch.object(self.module, "get_striker_list_from_sheet", return_value=[]), patch.object(self.module, "get_special_list_from_sheet", return_value=[]), patch.object(self.module, "get_latest_loser_teams", return_value=[]):
            response = self.client.get("/search?side=defense&season=s9&c1=x")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-season="${escapeHtml(season)}"', response.data)
        self.assertIn(b'${escapeHtml(res.date)}', response.data)


class StartupTests(IsolatedTest):
    def run_startup(self, limited=False, token=None):
        code = '''import os
from unittest.mock import patch
with patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')):
    import manage
    from werkzeug.test import Client
    from werkzeug.wrappers import Response
    client=Client(manage.application,Response)
    assert client.get('/healthz').status_code == 200
    if os.environ.get('ENABLE_LIMITED_APP') == '1':
        path=manage.LIMITED_PREFIX + '/upload'
        assert client.get(path).status_code == 401
        assert client.get(path,headers={'Authorization':'Bearer test-only-token'}).status_code == 200
    else:
        assert 'Blue_Archive_Battlelog_collect.app_limited' not in __import__('sys').modules
    print('startup OK')
'''
        env = dict(os.environ, ENABLE_LIMITED_APP="1" if limited else "0")
        if token:
            env["LIMITED_ACCESS_TOKEN"] = token
        else:
            env.pop("LIMITED_ACCESS_TOKEN", None)
        return subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_public_startup_has_no_google_reads_or_limited_import(self):
        result = self.run_startup()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_limited_requires_authentication_when_explicitly_enabled(self):
        result = self.run_startup(limited=True, token="test-only-token")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_limited_cannot_be_enabled_without_a_token(self):
        result = self.run_startup(limited=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires LIMITED_ACCESS_TOKEN", result.stderr)

    def test_gas_cli_imports_have_no_network_side_effects(self):
        import runpy
        for relative in ("call_gas.py", "call_gas_limited.py", "Blue_Archive_Battlelog_collect_public/call_gas.py", "Blue_Archive_Battlelog_collect/call_gas_limited.py"):
            runpy.run_path(str(ROOT / relative), run_name="import_test")


class GasTests(IsolatedTest):
    def test_conversion_waits_for_the_exact_input_id(self):
        from battlelog_gas import wait_for_input
        sheet = Mock()
        sheet.get.side_effect = [[["日付", "ID"], ["old", "11"]], [["日付", "ID"], ["new", "12"]]]
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = sheet
        with patch("battlelog_storage.google_client", return_value=client), patch("battlelog_gas.time.sleep") as sleep:
            wait_for_input("s9", "12")
        client.open_by_key.return_value.worksheet.assert_called_once_with("変換前_s9")
        self.assertEqual(sheet.get.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_stale_input_mirror_never_proceeds_to_conversion(self):
        from battlelog_gas import wait_for_input
        sheet = Mock()
        sheet.get.return_value = [["ID"], ["11"]]
        client = Mock()
        client.open_by_key.return_value.worksheet.return_value = sheet
        with patch("battlelog_storage.google_client", return_value=client), patch("battlelog_gas.time.sleep"):
            with self.assertRaises(OutputNotReady):
                wait_for_input("s11", "12")
        self.assertEqual(sheet.get.call_count, 7)

    def test_conversion_payload_and_timeouts(self):
        from battlelog_gas import run_conversion
        credential = Mock(token="fake-token")
        response = Mock()
        response.json.return_value = {"success": True}
        with patch.dict(os.environ, {"credentials": "{}", "GAS_SCRIPT_URL": "https://example.invalid/gas"}), patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=credential), patch("requests.post", return_value=response) as post:
            run_conversion("s9")
        self.assertEqual(post.call_args.kwargs["json"], {"function": "main", "season": "s9", "parameters": [{"parameter": {"season": "s9"}}]})
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 90))
        response.raise_for_status.assert_called_once()

    def test_gas_error_body_is_not_treated_as_success(self):
        from battlelog_gas import run_conversion
        response = Mock()
        response.json.return_value = {"error": "failed"}
        with patch.dict(os.environ, {"credentials": "{}", "GAS_SCRIPT_URL": "https://example.invalid/gas"}), patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=Mock()), patch("requests.post", return_value=response):
            with self.assertRaises(RuntimeError):
                run_conversion("s11")


if __name__ == "__main__":
    unittest.main()
