"""Kana ordering and reversible master-row operations; no OCR algorithm changes."""
import copy
import hashlib
import json
import os
import re
import unicodedata
import uuid
from urllib.parse import urlsplit

from battlelog_safety import file_lock

READING_COLUMN = "よみがな"
NAME_READINGS = {"御坂美琴": "みさかみこと", "食蜂操祈": "しょくほうみさき",
                 "佐天涙子": "さてんるいこ", "初音ミク": "はつねみく"}
SUFFIX_READINGS = {"水着": "みずぎ", "正月": "しょうがつ", "制服": "せいふく", "臨戦": "りんせん",
                   "温泉": "おんせん", "体操服": "たいそうふく", "応援団": "おうえんだん", "私服": "しふく", "通常": "つうじょう", "幼女": "ようじょ"}
SMALL_KANA = str.maketrans("ぁぃぅぇぉっゃゅょゎゕゖ", "あいうえおつやゆよわかけ")


class Conflict(ValueError):
    pass


def fullwidth_parentheses(value):
    return value.replace("(", "（").replace(")", "）")


def reading_for(name, supplied=""):
    value = unicodedata.normalize("NFKC", supplied.strip() if supplied else name)
    if not supplied:
        for source, reading in NAME_READINGS.items():
            value = value.replace(source, reading)
        for source, reading in SUFFIX_READINGS.items():
            value = value.replace(source, reading)
    value = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in value)
    return re.sub(r"[\s()*＊・･]", "", value)


def gojuon_key(name, reading=""):
    kana = reading_for(name, reading)
    primary = "".join(c for c in unicodedata.normalize("NFD", kana) if not unicodedata.combining(c)).translate(SMALL_KANA)
    # Long vowels compare by their vowel, rather than after every kana.
    vowels = {c: v for chars, v in (("あかさたなはまやらわがざだばぱぁ", "あ"), ("いきしちにひみりぎじぢびぴぃ", "い"),
                                   ("うくすつぬふむゆるぐずづぶぷぅ", "う"), ("えけせてねへめれげぜでべぺぇ", "え"),
                                   ("おこそとのほもよろをごぞどぼぽぉ", "お")) for c in chars}
    folded = ""
    for c in primary:
        folded += vowels.get(folded[-1], "ー") if c == "ー" and folded else c
    return folded, kana, unicodedata.normalize("NFKC", name)


def character_hash(item):
    fields = {key: item.get(key) for key in ("kind", "name", "image", "range", "cover")}
    fields["reading"] = reading_for(item.get("name", ""), item.get("reading", ""))
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def entered(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)):
        return {"numberValue": value}
    return {"stringValue": str(value or "")}


def row_object(kind, number, headers, row):
    data = dict(zip(headers, row + [""] * len(headers)))
    return {"kind": kind, "row": number, "name": data.get("キャラ名", ""), "image": data.get("アイコン", ""),
            "range": str(data.get("射程", "")) if kind == "STRIKER" else "",
            "cover": str(data.get("遮蔽", "")).upper() == "TRUE" if kind == "STRIKER" else False,
            "reading": reading_for(data.get("キャラ名", ""), data.get(READING_COLUMN, ""))}


class CharacterManagement:
    def characters(self):
        result = []
        for kind in ("STRIKER", "SPECIAL"):
            rows = self.master(kind).get_all_values()
            if not rows or not {"キャラ名", "アイコン"}.issubset(rows[0]):
                raise RuntimeError("Character master headers do not match")
            chars = [row_object(kind, index, rows[0], row) for index, row in enumerate(rows[1:], 2)]
            result.extend(sorted((c for c in chars if c["name"]), key=lambda c: gojuon_key(c["name"], c["reading"])))
        return result

    def sort_master(self, kind):
        sheet = self.master(kind)
        rows = sheet.get_all_values(value_render_option="FORMULA")
        headers = rows[0]
        read_col = headers.index(READING_COLUMN) if READING_COLUMN in headers else max(map(len, rows))
        helper = max(max(map(len, rows)), read_col + 1)
        requests = []
        if sheet.col_count <= helper:
            requests.append({"appendDimension": {"sheetId": sheet.id, "dimension": "COLUMNS", "length": helper + 1 - sheet.col_count}})
        requests.append({"updateCells": {"start": {"sheetId": sheet.id, "rowIndex": 0, "columnIndex": read_col},
                                          "rows": [{"values": [{"userEnteredValue": entered(READING_COLUMN)}]}], "fields": "userEnteredValue"}})
        name_col = headers.index("キャラ名")
        readings = [reading_for(row[name_col], row[read_col] if len(row) > read_col else "") if len(row) > name_col and row[name_col] else "" for row in rows[1:]]
        order = sorted(range(len(readings)), key=lambda i: (not bool(readings[i]), gojuon_key(rows[i + 1][name_col] if len(rows[i + 1]) > name_col else "", readings[i])))
        ranks = {index: rank for rank, index in enumerate(order)}
        for column, values in ((read_col, readings), (helper, [ranks[i] for i in range(len(readings))])):
            if values:
                requests.append({"updateCells": {"start": {"sheetId": sheet.id, "rowIndex": 1, "columnIndex": column},
                                                  "rows": [{"values": [{"userEnteredValue": entered(v)}]} for v in values], "fields": "userEnteredValue"}})
        if len(rows) > 1:
            requests.append({"sortRange": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": len(rows),
                                                       "startColumnIndex": 0, "endColumnIndex": helper + 1},
                                            "sortSpecs": [{"dimensionIndex": helper, "sortOrder": "ASCENDING"}]}})
            requests.append({"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": len(rows),
                                                         "startColumnIndex": helper, "endColumnIndex": helper + 1},
                                             "cell": {}, "fields": "userEnteredValue"}})
            if kind == "STRIKER" and "遮蔽" in headers:
                cover = headers.index("遮蔽")
                # A checkbox needs a boolean value and checkbox validation.
                for index, source in enumerate(order, 1):
                    row = rows[source + 1]
                    if len(row) > cover and str(row[cover]).upper() in ("TRUE", "FALSE"):
                        requests.append({"updateCells": {"start": {"sheetId": sheet.id, "rowIndex": index, "columnIndex": cover},
                                                          "rows": [{"values": [{"userEnteredValue": entered(str(row[cover]).upper() == "TRUE")}]}], "fields": "userEnteredValue"}})
                requests.append({"setDataValidation": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": len(rows),
                                                                    "startColumnIndex": cover, "endColumnIndex": cover + 1},
                                                         "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}}})
        sheet.spreadsheet.batch_update({"requests": requests})

    def sync_dictionary(self):
        chars = self.characters()
        ordered = list(dict.fromkeys(c["name"] for c in chars))
        sheet = self.sheet(os.environ.get("OUTPUT_SHEET_ID"), "辞書")
        old = sheet.col_values(1)
        # Preserve extra historical correction candidates instead of dropping them.
        extras = list(dict.fromkeys(name for name in old if name and name not in ordered))
        desired = ordered + extras
        if old != desired:
            width = max(len(old), len(desired))
            values = [[entered(desired[i]) if i < len(desired) else None] for i in range(width)]
            sheet.spreadsheet.batch_update({"requests": [{"updateCells": {"start": {"sheetId": sheet.id, "rowIndex": 0, "columnIndex": 0},
                                                                          "rows": [{"values": [{"userEnteredValue": row[0]}] if row[0] else [{}]} for row in values],
                                                                          "fields": "userEnteredValue"}}]})

    def snapshot_character(self, kind, name, expected_hash):
        sheet = self.master(kind)
        rows = sheet.get_all_values(value_render_option="FORMULA")
        headers = rows[0]
        positions = [(index, row) for index, row in enumerate(rows[1:], 2) if len(row) > headers.index("キャラ名") and row[headers.index("キャラ名")] == name]
        if len(positions) != 1:
            raise Conflict("対象キャラを一意に確認できません。画面を読み込み直してください。")
        number, row = positions[0]
        before = row_object(kind, number, headers, row)
        if character_hash(before) != expected_hash:
            raise Conflict("このキャラの情報が更新されています。画面を読み込み直してください。")
        before["_headers"], before["_row_values"] = headers, row
        # Store restorable cell data, including formulas, formatting and validation.
        from gspread.utils import rowcol_to_a1
        metadata = sheet.spreadsheet.fetch_sheet_metadata(params={"includeGridData": "true",
                        "ranges": f"'{kind}'!A{number}:{rowcol_to_a1(number, len(headers))}", "fields": "sheets(data(rowData(values)))"})
        cells = metadata["sheets"][0]["data"][0]["rowData"][0]["values"]
        name_cell = cells[headers.index("キャラ名")].get("userEnteredValue", {})
        if name_cell.get("stringValue") != name:
            raise Conflict("読み取り中にキャラの行が変更されました。画面を読み込み直してください。")
        supported = ("userEnteredValue", "userEnteredFormat", "dataValidation", "note", "textFormatRuns")
        before["_cells"] = [{k: v for k, v in cell.items() if k in supported} for cell in cells]
        return before

    def save_character(self, expected, fields):
        kind = fields.get("kind")
        sheet = self.master(kind)
        name, image, original = ((fields.get(key) or "").strip() for key in ("name", "image", "original"))
        name = fullwidth_parentheses(name)
        url = urlsplit(image)
        if not name or len(name) > 120 or any(c in name for c in "\r\n\t"):
            raise ValueError("キャラ名を120文字以内で入力してください。")
        if len(image) > 2000 or url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("アイコンには https:// で始まる画像URLを入力してください。")
        if original and fullwidth_parentheses(original) != name:
            raise ValueError("登録済みの名前は戦績との照合に使うため変更できません。")
        reading = reading_for(name, fields.get("reading", ""))
        if re.search(r"[^ぁ-ゖーa-zA-Z0-9]", reading) or len(reading) > 160:
            raise ValueError("漢字名などの並び順を指定するため、よみがなをひらがな・カタカナで入力してください。")
        if kind == "STRIKER" and str(fields.get("range")) not in ("350", "450", "550", "650", "750", "850"):
            raise ValueError("射程を選択してください。")
        with file_lock("sheets"):
            chars = self.characters()
            matches = [c for c in chars if unicodedata.normalize("NFKC", c["name"]) == unicodedata.normalize("NFKC", name)]
            if (not original and matches) or (original and (len(matches) != 1 or matches[0]["kind"] != kind)):
                raise ValueError("同じ名前の登録状況を確認してください。種類は追加後に変更できません。")
            before = self.snapshot_character(kind, original, fields.get("base_hash")) if original else None
            after = {"name": name, "kind": kind, "image": image, "reading": reading,
                     "range": str(fields.get("range", "")) if kind == "STRIKER" else "", "cover": fields.get("cover") == "1" if kind == "STRIKER" else False}
            operation = uuid.uuid4().hex
            revision = self.commit(expected, "character_pending", {"operation": operation, "before": before, "after": after})
            rows = sheet.get_all_values(value_render_option="FORMULA")
            headers = rows[0]
            if READING_COLUMN not in headers:
                headers = headers + [""] * (max(map(len, rows)) - len(headers)) + [READING_COLUMN]
                sheet.update("A1", [headers], value_input_option="RAW")
            position = before["row"] if before else len(rows) + 1
            if before and (len(rows) < position or rows[position - 1][headers.index("キャラ名")] != original):
                raise Conflict("シートの行が移動しています。画面を読み込み直してください。")
            if before and character_hash(row_object(kind, position, headers, rows[position - 1])) != character_hash(before):
                raise Conflict("読み取り中にキャラ情報が変更されました。画面を読み込み直してください。")
            row = (rows[position - 1] + [""] * len(headers))[:len(headers)] if before else [""] * len(headers)
            updates = {"キャラ名": name, "アイコン": image, READING_COLUMN: reading}
            if kind == "STRIKER":
                updates.update({"射程": after["range"], "遮蔽": after["cover"]})
            for key, value in updates.items():
                row[headers.index(key)] = value
            if before:
                cells = [{"userEnteredValue": {"formulaValue": value} if isinstance(value, str) and value.startswith("=") and headers[i] not in updates else entered(value)} for i, value in enumerate(row)]
                sheet.spreadsheet.batch_update({"requests": [{"updateCells": {"start": {"sheetId": sheet.id, "rowIndex": position - 1, "columnIndex": 0}, "rows": [{"values": cells}], "fields": "userEnteredValue"}}]})
            else:
                self.insert_character_row(sheet, position, [{"userEnteredValue": entered(v)} for v in row])
            self.sort_master(kind)
            if original and original != name:
                self.remove_dictionary_name(original)
            self.sync_dictionary()
            self.commit(revision, "character_saved", {"operation": operation, "before": before, "after": after})
            return after

    def insert_character_row(self, sheet, position, cells):
        requests = [{"insertDimension": {"range": {"sheetId": sheet.id, "dimension": "ROWS", "startIndex": position - 1, "endIndex": position}, "inheritFromBefore": position > 2}}]
        if position > 2:
            for mode in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
                requests.append({"copyPaste": {"source": {"sheetId": sheet.id, "startRowIndex": position - 2, "endRowIndex": position - 1, "startColumnIndex": 0, "endColumnIndex": len(cells)},
                                               "destination": {"sheetId": sheet.id, "startRowIndex": position - 1, "endRowIndex": position, "startColumnIndex": 0, "endColumnIndex": len(cells)}, "pasteType": mode}})
        requests.append({"updateCells": {"start": {"sheetId": sheet.id, "rowIndex": position - 1, "columnIndex": 0}, "rows": [{"values": cells}],
                                         "fields": "userEnteredValue,userEnteredFormat,dataValidation,note,textFormatRuns" if any(any(k in c for k in ("userEnteredFormat", "dataValidation", "note", "textFormatRuns")) for c in cells) else "userEnteredValue"}})
        sheet.spreadsheet.batch_update({"requests": requests})

    def delete_character(self, expected, kind, name, base_hash):
        with file_lock("sheets"):
            before = self.snapshot_character(kind, name, base_hash)
            operation = uuid.uuid4().hex
            revision = self.commit(expected, "character_delete_pending", {"operation": operation, "before": before})
            sheet = self.master(kind)
            sheet.spreadsheet.batch_update({"requests": [{"deleteDimension": {"range": {"sheetId": sheet.id, "dimension": "ROWS", "startIndex": before["row"] - 1, "endIndex": before["row"]}}}]})
            if not any(c["name"] == name for c in self.characters()):
                self.remove_dictionary_name(name)
            self.commit(revision, "character_deleted", {"operation": operation, "before": before})

    def restore_deleted_character(self, expected, event_id):
        with file_lock("sheets"):
            latest = self.read(force=True)
            event = next((e for e in latest["events"] if e["revision"] == event_id and e["action"] in ("character_deleted", "character_delete_pending")), None)
            if not event:
                raise ValueError("削除履歴が見つかりません。")
            before = event["details"]["before"]
            if any(c["name"] == before["name"] for c in self.characters()):
                raise Conflict("同じ名前のキャラが登録済みです。上書きせずに処理を止めました。")
            revision = self.commit(expected, "character_restore_pending", {"before": before})
            sheet = self.master(before["kind"])
            rows = sheet.get_all_values(value_render_option="FORMULA")
            # Map the backed-up cells by header so added columns cannot shift attributes.
            cells = []
            for header in rows[0]:
                index = before["_headers"].index(header) if header in before["_headers"] else None
                cell = copy.deepcopy(before["_cells"][index]) if index is not None and index < len(before["_cells"]) else {}
                if header == READING_COLUMN:
                    cell["userEnteredValue"] = entered(before["reading"])
                cells.append(cell)
            self.insert_character_row(sheet, len(rows) + 1, cells)
            self.sort_master(before["kind"])
            self.sync_dictionary()
            self.commit(revision, "character_restored", {"before": before})

    def remove_dictionary_name(self, name):
        dictionary = self.sheet(os.environ.get("OUTPUT_SHEET_ID"), "辞書")
        positions = [index for index, value in enumerate(dictionary.col_values(1)) if value == name]
        if positions:
            dictionary.spreadsheet.batch_update({"requests": [{"deleteRange": {"range": {"sheetId": dictionary.id, "startRowIndex": index, "endRowIndex": index + 1, "startColumnIndex": 0, "endColumnIndex": 1}, "shiftDimension": "ROWS"}} for index in reversed(positions)]})

    def normalize_character_order(self, expected):
        with file_lock("sheets"):
            before = {}
            for kind in ("STRIKER", "SPECIAL"):
                rows = self.master(kind).get_all_values()
                column = rows[0].index("キャラ名")
                before[kind] = [row[column] for row in rows[1:] if len(row) > column and row[column]]
            dictionary = self.sheet(os.environ.get("OUTPUT_SHEET_ID"), "辞書").col_values(1)
            revision = self.commit(expected, "character_order_pending", {"master_names": before, "dictionary_before": dictionary})
            for kind in ("STRIKER", "SPECIAL"):
                self.sort_master(kind)
            self.sync_dictionary()
            self.commit(revision, "character_ordered", {"count": len(self.characters())})
