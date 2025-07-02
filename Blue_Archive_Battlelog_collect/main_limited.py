import os
import cv2
import datetime
import numpy as np
import requests  # URLからの画像ダウンロード用
import subprocess
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from spreadsheet_manager_limited import update_spreadsheet
from Blue_Archive_Battlelog_collect.config_limited import CURRENT_SEASON  # ← season対応

# 日本時間 (JST) 定義
JST = datetime.timezone(datetime.timedelta(hours=9))

def get_template_urls():
    """
    Google スプレッドシートID "12rEszTQ20FdLoxBewBtz0kF8-MUt0TnBYrIwMj1E7c4"
    の「sonotaicon」シートからテンプレート画像のURLを取得するプレースホルダ関数。
    """
    attack_url = "https://drive.google.com/uc?export=download&id=1fvs35cCs0aKtxNZ1hX_myjiA_RrPufmB"
    defense_url = "https://drive.google.com/uc?export=download&id=17AdY1q9ZynxTNlBVUvJTmZMd220uC_bs"
    return attack_url, defense_url

def load_template(url):
    """
    URLからテンプレート画像をダウンロードし、80×80にリサイズして返す。
    デバッグ用に "debug_template_resized.jpg" に保存。
    """
    response = requests.get(url)
    response.raise_for_status()
    arr = np.asarray(bytearray(response.content), dtype=np.uint8)
    template = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    template = cv2.resize(template, (80, 80))
    cv2.imwrite("debug_template_resized.jpg", template)
    print("Resized template image saved as 'debug_template_resized.jpg'.")
    return template

def match_icon(roi_img, template_img, thresh=0.5):
    """
    テンプレートマッチングで最大類似度が閾値以上か判定。
    """
    res = cv2.matchTemplate(roi_img, template_img, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    print("Template match max value:", max_val)
    return max_val >= thresh

def clean_text(text):
    """
    OCRテキストから'*','改行','空白'を除去して返す。
    """
    return "".join(text.replace('*','').replace('\n','').replace('\r','').split())

def preprocess_image(image_path):
    """
    画像の前処理：グレースケール→二値化→最大輪郭でクロップ→1611×696にリサイズ。
    """
    print("Attempting to load image from:", image_path)
    if not os.path.exists(image_path):
        raise Exception("ファイルが存在しません: " + image_path)
    img = cv2.imread(image_path)
    if img is None:
        raise Exception("画像が読み込めませんでした。")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5,5), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        x, y, w, h = cv2.boundingRect(max(contours, key=cv2.contourArea))
        cropped = img[y:y+h, x:x+w]
    else:
        cropped = img
    resized = cv2.resize(cropped, (1611,696), interpolation=cv2.INTER_AREA)
    return resized

def mask_regions(image):
    """
    指定領域を白で塗りつぶしてマスク。
    """
    cv2.rectangle(image, (425,150), (700,225), (255,255,255), -1)
    cv2.rectangle(image, (1260,150), (1535,225), (255,255,255), -1)
    cv2.rectangle(image, (0,240), (1611,565), (255,255,255), -1)
    return image

def parse_ocr_text(ocr_text):
    """
    ヘッダー部のLv.表記・Win/Loseを解析して左右プレイヤー名と結果を返す。
    """
    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    try:
        vs_idx = lines.index("VS")
    except ValueError:
        vs_idx = None
    header = lines[:vs_idx] if vs_idx is not None else lines[:len(lines)//2]
    lv_entries = [(i, l) for i,l in enumerate(header) if l.startswith("Lv.")]
    if len(lv_entries) >= 2:
        left_idx, left_line = lv_entries[0]
        right_idx, right_line = lv_entries[1]
        left_name = left_line.replace("Lv.90","").strip()
        right_name = right_line.replace("Lv.90","").strip()
    else:
        left_name, right_name = "LeftPlayer","RightPlayer"
        left_idx = right_idx = -1
    win_pos  = [i for i,l in enumerate(header) if "Win" in l]
    lose_pos = [i for i,l in enumerate(header) if "Lose" in l]
    # 左右の最短距離でWin/Loseを割当て
    if win_pos and lose_pos:
        lw = min(abs(left_idx - p) for p in win_pos)
        ll = min(abs(left_idx - p) for p in lose_pos)
        if lw <= ll:
            left_res, right_res = "Win","Lose"
        else:
            left_res, right_res = "Lose","Win"
    elif win_pos:
        lw = min(abs(left_idx - p) for p in win_pos)
        rw = min(abs(right_idx - p) for p in win_pos)
        if lw <= rw:
            left_res, right_res = "Win","Lose"
        else:
            left_res, right_res = "Lose","Win"
    elif lose_pos:
        ll = min(abs(left_idx - p) for p in lose_pos)
        rl = min(abs(right_idx - p) for p in lose_pos)
        if ll <= rl:
            left_res, right_res = "Lose","Win"
        else:
            left_res, right_res = "Win","Lose"
    else:
        left_res = right_res = ""
    return left_name, left_res, right_name, right_res, None, None

def ocr_region(image, region):
    """
    指定領域からOCRを実行し、clean_textして返す。
    """
    x1,y1,x2,y2 = region
    sub = image[y1:y2, x1:x2]
    tmp = "temp_region.jpg"
    cv2.imwrite(tmp, sub)
    from ocr_processing import perform_google_vision_ocr
    text = perform_google_vision_ocr(tmp)
    os.remove(tmp)
    return clean_text(text)

def process_image(image_path, season=None):
    """
    画像を受け取って以下を実行し、row_dataを返す。
      1. 前処理＋マスク
      2. OCR (ヘッダー部抽出)
      3. parse_ocr_text で左右名＆結果
      4. アイコンROI取得＋テンプレマッチ
      5. キャラ領域OCR（攻撃側・防衛側を動的に割り当て）
      6. row_data組立て
    """
    img = preprocess_image(image_path)
    masked = mask_regions(img.copy())
    cv2.imwrite("debug_preprocessed.jpg", masked)
    tmp = "temp_preprocessed.jpg"
    cv2.imwrite(tmp, masked)
    from ocr_processing import perform_google_vision_ocr
    full_text = perform_google_vision_ocr(tmp)
    os.remove(tmp)
    print("OCR recognized text (header extraction):")
    print(full_text)

    left_name, left_res, right_name, right_res, _, _ = parse_ocr_text(full_text)

    # アイコンROI
    roi = img[115:195, 35:115]
    cv2.imwrite("debug_icon_roi.jpg", roi)
    attack_url, _ = get_template_urls()
    template = load_template(attack_url)
    left_sword = match_icon(roi, template, thresh=0.4)
    print("Left has sword:", left_sword)

    # キャラ領域座標
    left_regs = [(87,637,183,680),(186,637,280,680),(284,637,379,680),
                 (383,637,478,680),(481,637,576,680),(579,637,679,680)]
    right_regs= [(922,637,1017,680),(1020,637,1115,680),(1118,637,1213,680),
                 (1216,637,1311,680),(1314,637,1409,680),(1412,637,1512,680)]

    # プレイヤー・キャラ割当（攻撃側が左なら left_regsが攻撃キャラ、右なら逆）
    if left_sword:
        atk_name, atk_res = left_name, left_res
        def_name, def_res = right_name, right_res
        atk_chars  = [ocr_region(img, r) for r in left_regs]
        def_chars  = [ocr_region(img, r) for r in right_regs]
    else:
        atk_name, atk_res = right_name, right_res
        def_name, def_res = left_name, left_res
        atk_chars  = [ocr_region(img, r) for r in right_regs]
        def_chars  = [ocr_region(img, r) for r in left_regs]

    # 日付・結果行組立
    date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    row = [date_str, atk_name, atk_res] + atk_chars + [""] + [def_name, def_res] + def_chars
    return row

def call_apps_script():
    """
    Apps Script 呼び出し。サービスアカウント認証で token を取得し POST。
    GASエンドポイントURLは環境変数「GAS_SCRIPT_URL_limited」から取得
    """
    cred_cont = os.environ.get("credentials")
    if not cred_cont:
        raise Exception("Environment variable 'credentials' is not set or empty.")
    tmp = "/tmp/google_credentials.json"
    with open(tmp, "w") as f:
        f.write(cred_cont)

    SCOPES = ['https://www.googleapis.com/auth/script.external_request']
    creds = Credentials.from_service_account_file(tmp, scopes=SCOPES)
    if not creds.valid or creds.expired:
        creds.refresh(Request())
    token = creds.token

    url = os.environ.get("GAS_SCRIPT_URL_limited")  # ← 環境変数から取得
    if not url:
        raise Exception("GAS_SCRIPT_URL_limited environment variable is not set.")
    payload = {'function': 'main'}
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        raise Exception(f"Error calling Apps Script: {resp.status_code} {resp.text}")
    return resp.text

def main():
    """
    CLI実行用。引数に画像パスを渡すと、一連の処理を行う。
    """
    import sys
    if len(sys.argv) < 2:
        print("Usage: python main.py <image_path>")
        sys.exit(1)
    path = sys.argv[1]
    row = process_image(path, season=CURRENT_SEASON)  # ← シーズンも必ず指定

    # スプレッドシート更新
    update_spreadsheet(row, season=CURRENT_SEASON)
    print("スプレッドシートを更新しました:", row)

    # しらす式変換
    result = call_apps_script()
    print("Apps Script 実行結果：", result)


# ===================================
# ▼▼▼ 新・条件指定検索ロジック ▼▼▼
# ===================================
def search_battlelog_with_conditions(conditions, search_side, season=None, only_limited=False):
    """
    各枠について
      - キャラ名指定: 完全一致
      - キャラ名未指定で条件あり: 該当キャラ全員が候補、いずれかに一致でOK
    6枠分のOR+AND組み合わせ判定
    """
    cache = get_output_sheet_cache(season)
    all_records = cache or []
    if only_limited:
        all_records = [r for r in all_records if r.get("source") == "限定"]

    # 攻防で列名を切り替え
    if search_side == "attack":
        char_cols = ["A1", "A2", "A3", "A4", "ASP1", "ASP2"]
    else:
        char_cols = ["D1", "D2", "D3", "D4", "DSP1", "DSP2"]

    # キャラマスタを取得（射程・遮蔽判定用）
    striker_list = get_striker_list_from_sheet()

    # SPECIALは今回「条件検索なし」想定
    def get_candidates(idx, cond):
        # 0-3: STRIKER枠, 4-5: SPECIAL枠
        if cond.get("name"):
            return [cond["name"]]
        elif idx <= 3 and (cond.get("range") or cond.get("cover") is not None):
            s_range = int(cond["range"]) if cond.get("range") else None
            cover = cond.get("cover")
            # cover: None, True, Falseのいずれか
            # spreadsheet_manager.pyでget_striker_names_by_conditionを実装
            return get_striker_names_by_condition(s_range, cover)
        else:
            return []

    def normalize(s):
        if s is None:
            return ""
        return str(s).strip()

    result = []
    for row in all_records:
        match = True
        for idx, cond in enumerate(conditions):
            col = char_cols[idx]
            val = normalize(row.get(col, ""))
            candidates = get_candidates(idx, cond)
            if candidates:
                if val not in candidates:
                    match = False
                    break
            # 条件もキャラ名も指定なし→どれでもOK
        if match:
            result.append(row)
    return result

# ===================================

if __name__ == "__main__":
    main()
