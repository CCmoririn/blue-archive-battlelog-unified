import os
import gspread
from google.oauth2.service_account import Credentials
import threading
import time
import json

from Blue_Archive_Battlelog_collect.config_limited import CURRENT_SEASON, SEASON_LIST, CACHE_DIR

print("[限定版spreadsheet_manager.py] CACHE_DIR =", CACHE_DIR)

# ========== アップロード時スプレッドシート追加 ==========

def update_spreadsheet(data, season=None):
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        raise Exception("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set.")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    SPREADSHEET_ID = os.environ.get("BATTLELOG_SHEET_ID_limited")
    if not SPREADSHEET_ID:
        raise Exception("BATTLELOG_SHEET_ID_limited environment variable is not set.")
    season_key = season or CURRENT_SEASON
    sheet_name = f"変換前_{season_key}"
    worksheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
    worksheet.insert_row(data, 3)
    print(f"[{sheet_name}]シートにスプレッドシートを更新しました:", data)

# ===== シーズンごとのキャッシュ管理 =====

def get_cache_filepath(season):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{season}.json")

def save_output_cache(season, data):
    path = get_cache_filepath(season)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"キャッシュ保存完了: {path}（{len(data)}件）")

def load_output_cache(season):
    path = get_cache_filepath(season)
    if not os.path.exists(path):
        print(f"キャッシュファイルなし: {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            return data
        except Exception as e:
            print(f"キャッシュ読込失敗: {e}")
            return []

def refresh_output_sheet_cache(season=None):
    """
    限定版：限定DB「出力結果_シーズン名」と「一般版から転送」シート両方から取得し
    source="限定"/"一般"を付けてマージ、日付順でまとめてキャッシュ保存
    """
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        raise Exception("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set.")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)

    # 限定DBのスプレッドシートID
    LIMITED_OUTPUT_SHEET_ID = os.environ.get("OUTPUT_SHEET_ID_limited")
    if not LIMITED_OUTPUT_SHEET_ID:
        raise Exception("OUTPUT_SHEET_ID_limited environment variable is not set.")
    season_key = season or CURRENT_SEASON

    # 一般DBの「転送」シートID（＝同じファイル内 or 環境変数で取得可）
    GENERAL_TRANSFER_SHEET_ID = os.environ.get("GENERAL_TRANSFER_SHEET_ID") \
        or LIMITED_OUTPUT_SHEET_ID  # デフォは同一ファイル

    # 1. 限定データ
    limited_sheet_name = f"出力結果_{season_key}"
    limited_ws = client.open_by_key(LIMITED_OUTPUT_SHEET_ID).worksheet(limited_sheet_name)
    limited_records = get_sheet_records_with_empty_safe(limited_ws, head_row=2)
    for row in limited_records:
        row["source"] = "限定"

    # 2. 一般版から転送
    try:
        general_ws = client.open_by_key(GENERAL_TRANSFER_SHEET_ID).worksheet("一般版から転送")
        general_records = get_sheet_records_with_empty_safe(general_ws, head_row=2)
        for row in general_records:
            row["source"] = "一般"
    except Exception as e:
        print(f"一般版から転送シートの取得失敗: {e}")
        general_records = []

    # 3. 日付順に結合（新しい順）へ
    def parse_datetime(row):
        import datetime
        v = row.get("日付", "")
        try:
            return datetime.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.datetime.min

    all_data = limited_records + general_records
    all_data.sort(key=parse_datetime, reverse=True)

    save_output_cache(season_key, all_data)
    return all_data

def get_output_sheet_cache(season=None):
    season_key = season or CURRENT_SEASON
    data = load_output_cache(season_key)
    if not data:
        print(f"{season_key}のキャッシュが無いので再生成します")
        data = refresh_output_sheet_cache(season_key)
    return data

def fetch_latest_output_row_as_dict(season=None):
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        raise Exception("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set.")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    SPREADSHEET_ID = os.environ.get("OUTPUT_SHEET_ID_limited")
    if not SPREADSHEET_ID:
        raise Exception("OUTPUT_SHEET_ID_limited environment variable is not set.")
    season_key = season or CURRENT_SEASON
    sheet_name = f"出力結果_{season_key}"
    worksheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
    headers = worksheet.row_values(2)    # 2行目がヘッダー
    latest_row = worksheet.row_values(3) # 3行目が最新データ

    seen = {}
    uniq_headers = []
    for h in headers:
        base = h.strip() if h.strip() else "空欄"
        count = seen.get(base, 0)
        if count > 0:
            uniq_headers.append(f"{base}_{count+1}")
        else:
            uniq_headers.append(base)
        seen[base] = count + 1

    row_dict = {}
    for idx, key in enumerate(uniq_headers):
        row_dict[key] = latest_row[idx] if idx < len(latest_row) else ""
    return row_dict

def append_battlelog_row_from_api(row_dict, season=None, source="一般"):
    season_key = season or CURRENT_SEASON
    data = get_output_sheet_cache(season_key)
    row_dict["source"] = source
    data.insert(0, row_dict)
    save_output_cache(season_key, data)
    print(f"API経由で{source}データをキャッシュ[{season_key}]に追加: {row_dict}")

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
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds_path:
            raise Exception("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set.")
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        client = gspread.authorize(creds)
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
                # 射程は整数型に変換。異常値はスキップ
                s_range = int(row.get("射程", 0) or 0)
                if s_range not in (350, 450, 550, 650, 750, 850):
                    raise ValueError
            except Exception:
                print(f"[STRIKER] キャラ「{name}」の射程データ異常、スキップ")
                continue
            shield_raw = row.get("遮蔽", "")
            # TRUE/FALSE（str, boolどちらでも対応）に変換
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
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds_path:
            raise Exception("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set.")
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        client = gspread.authorize(creds)
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

_update_striker_cache()
_update_special_cache()

def char_cache_scheduler():
    while True:
        time.sleep(_CHAR_CACHE_LIFETIME)
        _update_striker_cache()
        _update_special_cache()

threading.Thread(target=char_cache_scheduler, daemon=True).start()

# ========== その他アイコンのキャッシュ ==========

_OTHER_ICON_SPREADSHEET_ID = os.environ.get("CHARDATA_SHEET_ID")
_OTHER_ICON_SHEET = "その他アイコン"
_other_icon_cache = {}

def load_other_icon_cache():
    global _other_icon_cache
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        raise Exception("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set.")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    ws = client.open_by_key(_OTHER_ICON_SPREADSHEET_ID).worksheet(_OTHER_ICON_SHEET)
    records = ws.get_all_records()
    cache = {}
    for row in records:
        key = row.get('種別', '').strip()
        url = row.get('アイコン', '').strip()
        if key and url:
            cache[key] = url
    _other_icon_cache = cache

def get_other_icon(key):
    return _other_icon_cache.get(key, "")

def reload_other_icon_cache():
    load_other_icon_cache()

# ========== 空欄・重複ヘッダーでも安全な取得関数 ==========

def get_sheet_records_with_empty_safe(worksheet, head_row=2):
    rows = worksheet.get_all_values()
    headers = rows[head_row - 1]
    seen = {}
    uniq_headers = []
    for h in headers:
        base = h.strip() if h.strip() else "空欄"
        count = seen.get(base, 0)
        if count > 0:
            uniq_headers.append(f"{base}_{count+1}")
        else:
            uniq_headers.append(base)
        seen[base] = count + 1

    data = []
    for row in rows[head_row:]:
        record = {}
        for idx, val in enumerate(row):
            if idx < len(uniq_headers):
                record[uniq_headers[idx]] = val
        data.append(record)
    return data

# ========== 表記ゆれを吸収して一致判定 ==========

def normalize(s):
    if s is None:
        return ""
    s = str(s)
    s = s.replace(" ", "").replace("　", "").replace("＊", "*")
    s = s.replace("（", "(").replace("）", ")").replace("(", "(").replace(")", ")")
    return s.strip()

# ========== キャッシュ参照での検索 ==========

def search_battlelog_output_sheet(query, search_side, season=None, only_limited=False, ranges=None, covers=None):
    """
    射程/遮蔽検索対応版
    ・query: ["キャラ名", ...]6枠
    ・ranges: [str/int or ""]*4
    ・covers: [bool/str or ""]*4
    """
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
                # 射程判定
                if qrange:
                    try:
                        qrange_int = int(qrange)
                        char_info = next(
                            (c for c in get_striker_list_from_sheet() if normalize(c["name"]) == target_name),
                            None
                        )
                        if not char_info or char_info.get("射程") != qrange_int:
                            match = False
                            break
                    except Exception:
                        match = False
                        break
                # 遮蔽判定
                if qcover:
                    qcover_bool = True if str(qcover).lower() in ("1", "true", "yes", "on") else False
                    char_info = next(
                        (c for c in get_striker_list_from_sheet() if normalize(c["name"]) == target_name),
                        None
                    )
                    if not char_info or char_info.get("遮蔽") != qcover_bool:
                        match = False
                        break
        if not match:
            continue

        # --- 5,6枠目（スペシャル）：順不同一致判定 ---
        sp_query = [normalize(query_norm[i]) for i in range(4, 6) if query_norm[i]]
        sp_target = [normalize(row.get(char_cols[i], "")) for i in range(4, 6) if row.get(char_cols[i], "")]
        if sp_query:
            # 両方指定：完全一致（順不同）
            if len(sp_query) == 2:
                if sorted(sp_query) != sorted(sp_target):
                    match = False
            # 片方だけ指定：どちらかに含まれていればOK
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
# =========================
