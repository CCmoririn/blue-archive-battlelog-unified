import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CURRENT_SEASON, SEASON_LIST, CACHE_DIR
from battlelog_storage import SheetStore, google_credentials, google_client, records_from_values
from battlelog_safety import validate_season
from battlelog_admin_store import public_settings

_store = SheetStore(CACHE_DIR, CURRENT_SEASON, SEASON_LIST,
                    "BATTLELOG_SHEET_ID", "OUTPUT_SHEET_ID", client_factory=lambda: google_client(), settings_provider=public_settings)

def get_google_credentials(scopes):
    return google_credentials(scopes)

def update_spreadsheet(data, season=None):
    return _store.insert(data, season)

def get_cache_filepath(season):
    return str(_store.path(season))

def save_output_cache(season, data):
    return _store.save(season, data)

def load_output_cache(season):
    return _store.load(season)

def refresh_output_sheet_cache(season=None):
    return _store.refresh(season)

def get_output_sheet_cache(season=None):
    return _store.get(season)

def fetch_latest_output_row_as_dict(season=None, expected_id=None, expected_row=None):
    return _store.fetch(season, expected_id=expected_id, expected_row=expected_row)

def append_battlelog_row_from_api(row_dict, season=None, source="限定", cache_only=False):
    return _store.append(row_dict, season, source, cache_only=cache_only)

def update_vote_count(row_id, vote_type, delta, season=None):
    return _store.vote(row_id, vote_type, delta, season)

# ========== キャラデータ（STRIKER/SPECIAL）6時間キャッシュ ==========

_CHAR_CACHE_LIFETIME = 6 * 60 * 60  # 6時間（秒）

_striker_cache = {
    "data": None,
    "timestamp": 0
}
_special_cache = {
    "data": None,
    "timestamp": 0
}

def _update_striker_cache():
    global _striker_cache
    try:
        print("STRIKERキャッシュを更新します...")
        SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
        client = google_client()
        SPREADSHEET_ID = os.environ.get("CHARDATA_SHEET_ID")
        if not SPREADSHEET_ID:
            raise Exception("CHARDATA_SHEET_ID environment variable is not set.")
        worksheet = client.open_by_key(SPREADSHEET_ID).worksheet("STRIKER")
        records = worksheet.get_all_records()
        char_list = []
        for row in records:
            name = row.get("キャラ名")
            icon_url = row.get("アイコン")
            try:
                s_range = int(row.get("射程", 0) or 0)
                if s_range not in (350, 450, 550, 650, 750, 850):
                    raise ValueError
            except Exception:
                print(f"[STRIKER] キャラ「{name}」の射程データ異常、スキップ")
                continue
            shield_raw = row.get("遮蔽", "")
            if isinstance(shield_raw, bool):
                shield = shield_raw
            else:
                shield = str(shield_raw).strip().upper() == "TRUE"
            if name and icon_url:
                char_list.append({
                    "name": name,
                    "image": icon_url,
                    "射程": s_range,
                    "遮蔽": shield,
                })
        _striker_cache = {
            "data": char_list,
            "timestamp": time.time()
        }
        print(f"STRIKERキャッシュ更新完了（{len(char_list)}件）")
    except Exception as e:
        print(f"STRIKERキャッシュ更新失敗: {e}")

def _update_special_cache():
    global _special_cache
    try:
        print("SPECIALキャッシュを更新します...")
        SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
        client = google_client()
        SPREADSHEET_ID = os.environ.get("CHARDATA_SHEET_ID")
        if not SPREADSHEET_ID:
            raise Exception("CHARDATA_SHEET_ID environment variable is not set.")
        worksheet = client.open_by_key(SPREADSHEET_ID).worksheet("SPECIAL")
        records = worksheet.get_all_records()
        char_list = []
        for row in records:
            name = row.get("キャラ名")
            icon_url = row.get("アイコン")
            if name and icon_url:
                char_list.append({"name": name, "image": icon_url})
        _special_cache = {
            "data": char_list,
            "timestamp": time.time()
        }
        print(f"SPECIALキャッシュ更新完了（{len(char_list)}件）")
    except Exception as e:
        print(f"SPECIALキャッシュ更新失敗: {e}")

def get_striker_list_from_sheet():
    global _striker_cache
    now = time.time()
    if (_striker_cache["data"] is None) or (now - _striker_cache["timestamp"] > _CHAR_CACHE_LIFETIME):
        _update_striker_cache()
    return _striker_cache["data"] or []

def get_special_list_from_sheet():
    global _special_cache
    now = time.time()
    if (_special_cache["data"] is None) or (now - _special_cache["timestamp"] > _CHAR_CACHE_LIFETIME):
        _update_special_cache()
    return _special_cache["data"] or []



# ========== 新規追加: 射程・遮蔽でSTRIKER名リストを取得 ==========

def get_striker_names_by_condition(s_range=None, cover=None):
    result = []
    for c in get_striker_list_from_sheet():
        if s_range is not None and c.get("射程") != s_range:
            continue
        if cover is not None and c.get("遮蔽") != cover:
            continue
        result.append(c["name"])
    return result

# ========== その他アイコンのキャッシュ ==========

_OTHER_ICON_SPREADSHEET_ID = os.environ.get("CHARDATA_SHEET_ID")
_OTHER_ICON_SHEET = "その他アイコン"
_other_icon_cache = {}
_other_icon_timestamp = 0

def load_other_icon_cache():
    global _other_icon_cache, _other_icon_timestamp
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    client = google_client()
    ws = client.open_by_key(_OTHER_ICON_SPREADSHEET_ID).worksheet(_OTHER_ICON_SHEET)
    records = ws.get_all_records()
    cache = {}
    for row in records:
        key = row.get('種別', '').strip()
        url = row.get('アイコン', '').strip()
        if key and url:
            cache[key] = url
    _other_icon_cache = cache
    _other_icon_timestamp = time.time()

def get_other_icon(key):
    if not _other_icon_cache or time.time() - _other_icon_timestamp > _CHAR_CACHE_LIFETIME:
        try:
            load_other_icon_cache()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Icon master unavailable")
    return _other_icon_cache.get(key, "")

def reload_other_icon_cache():
    load_other_icon_cache()

# ========== 空欄・重複ヘッダーでも安全な取得関数 ==========

def get_sheet_records_with_empty_safe(worksheet, head_row=2):
    return records_from_values(worksheet.get_all_values(), head_row)

def normalize(s):
    if s is None:
        return ""
    s = str(s)
    s = s.replace(" ", "").replace("　", "").replace("＊", "*")
    s = s.replace("（", "(").replace("）", ")").replace("(", "(").replace(")", ")")
    return s.strip()

# ========== キャッシュ参照での検索 ==========

def search_battlelog_output_sheet(query, search_side, season=None, only_limited=False, ranges=None, covers=None):
    cache = get_output_sheet_cache(season)
    all_records_main = cache or []

    if only_limited:
        all_records_main = [r for r in all_records_main if r.get("source") == "限定"]

    if search_side == "attack":
        char_cols = ["A1", "A2", "A3", "A4", "ASP1", "ASP2"]
    else:
        char_cols = ["D1", "D2", "D3", "D4", "DSP1", "DSP2"]

    query_norm = [normalize(x) for x in (query or [""]*6)]
    if not any(query_norm) and not (ranges or covers):
        print("全枠空欄かつ射程/遮蔽条件なしのため検索しません")
        return []

    ranges = ranges or [""]*4
    covers = covers or [""]*4

    needs_master = any(ranges) or any(covers)
    master_by_name = {}
    if needs_master:
        for char in get_striker_list_from_sheet():
            master_by_name.setdefault(normalize(char["name"]), char)
    result = []
    for row in all_records_main:
        match = True
        # --- 1～4枠目（ストライカー）：キャラ名／射程／遮蔽 ---
        for i in range(4):
            qname = query_norm[i]
            qrange = ranges[i]
            qcover = covers[i]
            target_name = normalize(row.get(char_cols[i], ""))
            if qname and target_name != qname:
                match = False
                break
            # 射程・遮蔽チェック（キャラ名未指定時のみ効く仕様）
            if not qname:
                if qrange:
                    try:
                        qrange_int = int(qrange)
                        char_info = master_by_name.get(target_name)
                        if not char_info or char_info.get("射程") != qrange_int:
                            match = False
                            break
                    except Exception:
                        match = False
                        break
                if qcover:
                    qcover_bool = True if str(qcover).lower() in ("1", "true", "yes", "on") else False
                    char_info = master_by_name.get(target_name)
                    if not char_info or char_info.get("遮蔽") != qcover_bool:
                        match = False
                        break
        if not match:
            continue

        # --- 5,6枠目（スペシャル）：順不同一致判定 ---
        sp_query = [normalize(query_norm[i]) for i in range(4, 6) if query_norm[i]]
        sp_target = [normalize(row.get(char_cols[i], "")) for i in range(4, 6) if row.get(char_cols[i], "")]
        if sp_query:
            if len(sp_query) == 2:
                if sorted(sp_query) != sorted(sp_target):
                    match = False
            elif len(sp_query) == 1:
                if sp_query[0] not in sp_target:
                    match = False

        if not match:
            continue

        result.append(row)
    return result

# =========================
# ▼▼▼ ここから新規追加 ▼▼▼
# =========================

def get_latest_loser_teams(n=5, season=None, only_limited=False):
    striker_list = get_striker_list_from_sheet()
    special_list = get_special_list_from_sheet()
    char_image_map = {c["name"]: c["image"] for c in striker_list + special_list}

    side_icon_map = {
        "attack": get_other_icon("攻撃側"),
        "defense": get_other_icon("防衛側"),
    }
    lose_icon = get_other_icon("負け")

    logs = get_output_sheet_cache(season) or []
    if only_limited:
        logs = [row for row in logs if row.get("source") == "限定"]
    result = []

    for row in logs:
        team = None
        side = None
        if row.get("勝敗", "") == "Lose":
            side = "attack"
            chars = [row.get(f"A{i+1}", "") for i in range(4)] + [row.get("ASP1", ""), row.get("ASP2", "")]
        elif row.get("勝敗_2", "") == "Lose":
            side = "defense"
            chars = [row.get(f"D{i+1}", "") for i in range(4)] + [row.get("DSP1", ""), row.get("DSP2", "")]
        if side:
            char_objs = []
            for name in chars:
                char_objs.append({
                    "name": name,
                    "image_url": char_image_map.get(name, ""),
                })
            team = {
                "side": side,
                "side_icon": side_icon_map.get(side, ""),
                "lose_icon": lose_icon,
                "characters": char_objs,
                "date": row.get("日付", ""),
                "source": row.get("source", ""),
            }
            result.append(team)
        if len(result) >= n:
            break
    return result

# =========================
# ▲▲▲ ここまで新規追加 ▲▲▲
