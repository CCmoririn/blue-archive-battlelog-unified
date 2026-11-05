import os
from dotenv import load_dotenv
if os.environ.get("BATTLELOG_SKIP_DOTENV") != "1":
    load_dotenv()

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unicodedata
import subprocess
from flask import Blueprint, request, render_template, jsonify, redirect, url_for, send_from_directory

from spreadsheet_manager_limited import (
    update_spreadsheet,
    get_striker_list_from_sheet,
    get_special_list_from_sheet,
    search_battlelog_output_sheet,
    get_other_icon,
    load_other_icon_cache,
    append_battlelog_row_from_api,
    fetch_latest_output_row_as_dict,
    get_latest_loser_teams
)
from Blue_Archive_Battlelog_collect.config_limited import CURRENT_SEASON, SEASON_LIST, CACHE_DIR

from battlelog_safety import (configure_app, data_path, nonnegative_int,
                              validate_attacks, validate_search, validate_season)
from battlelog_web import upload_image, confirm_upload

from defense_suggester_limited import suggest_defense_teams, suggest_team_for_template

# ▼▼ ここで限定用ルートprefixを定数で一元管理（configでもOK） ▼▼
LIMITED_PREFIX = "/asdfg"

print("[限定版app_limited.py] CACHE_DIR =", CACHE_DIR)

# ▼▼ Blueprint定義。Flask本体ではなくlimitedとして登録する ▼▼
limited = Blueprint(
    "limited", __name__,
    url_prefix=LIMITED_PREFIX,       # ← ここでprefixを一元管理
    template_folder="templates",     # 必要に応じて限定用ディレクトリに変更
    static_folder="static"           # 必要なら限定用static
)

if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    print("GOOGLE_APPLICATION_CREDENTIALS not found in environment variables.")


def normalize_sp_chars(chars: list, side: str) -> list:
    if not chars or len(chars) != 6:
        return chars
    main = chars[:4]
    sp = sorted(chars[4:6])
    return main + sp

def match_team(query, target, side):
    return normalize_sp_chars(query, side) == normalize_sp_chars(target, side)

# 以降、@app.routeをすべて@limited.routeに修正
@limited.route("/", methods=["GET"])
def index():
    loser_teams = get_latest_loser_teams(5)
    return render_template(
        "index.html",
        SEASON_LIST=SEASON_LIST,
        CURRENT_SEASON=CURRENT_SEASON,
        loser_teams=loser_teams
    )

@limited.route('/uploads/<filename>')
def uploaded_file(filename):
    uploads_dir = str(data_path("uploads"))
    return send_from_directory(uploads_dir, filename)

@limited.route("/upload", methods=["GET", "POST"])
def upload():
    return upload_image(CURRENT_SEASON, SEASON_LIST, "limited", "main_limited", "limited.uploaded_file")

@limited.route("/upload/confirm", methods=["POST"])
def upload_confirm():
    return confirm_upload(CURRENT_SEASON, SEASON_LIST, "limited",
                          update_spreadsheet, fetch_latest_output_row_as_dict,
                          append_battlelog_row_from_api, "limited.upload_complete")

@limited.route("/api/add_battlelog", methods=["POST"])
def api_add_battlelog():
    try:
        row_dict = request.json
        if not row_dict:
            return jsonify({"result": "error", "detail": "No data received"}), 400
        source = row_dict.get("source", "一般")
        season = row_dict.get("season", CURRENT_SEASON)
        append_battlelog_row_from_api(row_dict, season=season, source=source, cache_only=True)
        print(f"API経由でデータ受信・追加完了: {row_dict}")
        return jsonify({"result": "ok"})
    except Exception as e:
        print(f"API追加エラー: {e}")
        return jsonify({"result": "error", "detail": str(e)}), 500

@limited.route("/upload/complete", methods=["GET"])
def upload_complete():
    return render_template(
        "complete.html",
        message="アップロードが完了しました"
    )

@limited.route("/search", methods=["GET"])
def search():
    try:
        striker_list = get_striker_list_from_sheet()
        special_list = get_special_list_from_sheet()
    except Exception as e:
        print(f"キャラリスト取得エラー: {e}")
        striker_list = []
        special_list = []
    loser_teams = get_latest_loser_teams(5)
    return render_template(
        "db.html",
        striker_list=striker_list,
        special_list=special_list,
        SEASON_LIST=SEASON_LIST,
        CURRENT_SEASON=CURRENT_SEASON,
        request=request,
        loser_teams=loser_teams
    )

@limited.route("/api/search", methods=["POST"])
def api_search():
    try:
        from datetime import datetime

        data = request.get_json(silent=True)
        season, characters, ranges, covers = validate_search(data, SEASON_LIST, CURRENT_SEASON)
        side = data["side"]
        only_limited = data.get("only_limited", False)

        matched_rows = search_battlelog_output_sheet(
            characters, side, season=season, only_limited=only_limited, ranges=ranges, covers=covers
        )

        def parse_date(row):
            try:
                return datetime.strptime(row.get("日付", ""), "%Y-%m-%d %H:%M:%S")
            except Exception:
                return datetime.min

        matched_rows = sorted(matched_rows, key=parse_date, reverse=True)

        response = []
        win_icon = get_other_icon("勝ち")
        lose_icon = get_other_icon("負け")
        attack_icon = get_other_icon("攻撃側")
        defense_icon = get_other_icon("防衛側")

        for row in matched_rows:
            if only_limited and row.get("source") != "限定":
                continue

            if side == "attack":
                if row.get("勝敗_2", "") != "Win":
                    continue
                response.append({
                    "source": row.get("source", ""),
                    "winner_type": "defense",
                    "winner_icon": defense_icon,
                    "winner_winlose_icon": win_icon,
                    "winner_player": row.get("プレイヤー名_2", ""),
                    "winner_characters": [
                        row.get("D1", ""),
                        row.get("D2", ""),
                        row.get("D3", ""),
                        row.get("D4", ""),
                        row.get("DSP1", ""),
                        row.get("DSP2", ""),
                    ],
                    "loser_type": "attack",
                    "loser_icon": attack_icon,
                    "loser_winlose_icon": lose_icon,
                    "loser_player": row.get("プレイヤー名", ""),
                    "loser_characters": [
                        row.get("A1", ""),
                        row.get("A2", ""),
                        row.get("A3", ""),
                        row.get("A4", ""),
                        row.get("ASP1", ""),
                        row.get("ASP2", ""),
                    ],
                    "date": row.get("日付", ""),
                })
            else:
                if row.get("勝敗", "") != "Win":
                    continue
                response.append({
                    "source": row.get("source", ""),
                    "winner_type": "attack",
                    "winner_icon": attack_icon,
                    "winner_winlose_icon": win_icon,
                    "winner_player": row.get("プレイヤー名", ""),
                    "winner_characters": [
                        row.get("A1", ""),
                        row.get("A2", ""),
                        row.get("A3", ""),
                        row.get("A4", ""),
                        row.get("ASP1", ""),
                        row.get("ASP2", ""),
                    ],
                    "loser_type": "defense",
                    "loser_icon": defense_icon,
                    "loser_winlose_icon": lose_icon,
                    "loser_player": row.get("プレイヤー名_2", ""),
                    "loser_characters": [
                        row.get("D1", ""),
                        row.get("D2", ""),
                        row.get("D3", ""),
                        row.get("D4", ""),
                        row.get("DSP1", ""),
                        row.get("DSP2", ""),
                    ],
                    "date": row.get("日付", ""),
                })
        return jsonify({"results": response})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"/api/search エラー: {e}")
        return jsonify({"error": "処理に失敗しました。しばらくしてからお試しください。"}), 503

# ▼▼▼ 防衛提案ページ
@limited.route("/defense_suggest", methods=["GET", "POST"])
def defense_suggest():
    message = None
    result = None
    try:
        season = validate_season(request.form.get("season"), SEASON_LIST, CURRENT_SEASON)
    except ValueError as error:
        return str(error), 400
    striker_list = get_striker_list_from_sheet()
    special_list = get_special_list_from_sheet()
    attack_teams = [[None for _ in range(6)] for _ in range(5)]
    strict_pos = bool(request.form.get("strict_pos")) if request.method == "POST" else False
    if request.method == "POST":
        try:
            attacks = []
            for i in range(5):
                chars = []
                for j in range(6):
                    char_name = request.form.get(f"attack_{i}_{j}", "").strip()
                    chars.append(char_name)
                if any(chars):
                    attacks.append(chars)
                attack_teams[i] = [
                    next((c for c in striker_list if c["name"] == chars[j]), None) if j < 4 else
                    next((c for c in special_list if c["name"] == chars[j]), None)
                    if chars[j] else None
                    for j in range(6)
                ]
            if not attacks:
                attacks = [["", "", "", "", "", ""]]
            validate_attacks(attacks)
            result = suggest_defense_teams(attacks, season=season, strict_pos=strict_pos)
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Defense suggestion failed")
            message = "提案を作成できませんでした。しばらくしてからお試しください。"
    else:
        attack_teams = [[None for _ in range(6)] for _ in range(5)]
    return render_template(
        "defense_suggest.html",
        striker_list=striker_list,
        special_list=special_list,
        SEASON_LIST=SEASON_LIST,
        CURRENT_SEASON=season,
        message=message,
        result=result,
        strict_pos=strict_pos,
        attack_teams=attack_teams
    )

@limited.route("/api/template_detail", methods=["POST"])
def api_template_detail():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("入力内容を確認してください。")
        template_tags = data.get("template_tags")
        attacks = data.get("attacks")
        season = validate_season(data.get("season"), SEASON_LIST, CURRENT_SEASON)
        strict_pos = bool(data.get("strict_pos"))
        if attacks is None or attacks == []:
            attacks = [["", "", "", "", "", ""]]
        validate_attacks(attacks)
        if type(data.get("strict_pos", False)) is not bool:
            raise ValueError("配置の指定が正しくありません。")
        if (not isinstance(template_tags, list) or len(template_tags) != 4
                or any(not isinstance(tag, list) or len(tag) != 2
                       or type(tag[0]) is not int or tag[0] not in (350,450,550,650,750,850)
                       or type(tag[1]) is not bool for tag in template_tags)):
            raise ValueError("テンプレートの指定が正しくありません。")
        res = suggest_team_for_template(template_tags, attacks=attacks, season=season, strict_pos=strict_pos)
        return jsonify(res)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"/api/template_detail エラー: {e}")
        return jsonify({"error": "処理に失敗しました。しばらくしてからお試しください。"}), 503

@limited.route("/privacy.html")
def privacy():
    return render_template("privacy.html")

@limited.route('/guide')
def guide():
    return render_template('guide.html')

@limited.route('/tips')
def tips():
    return render_template('tips.html')

@limited.route('/tips/character-growth')
def tips_character_growth():
    return render_template('character-growth.html')

@limited.route("/contact.html")
def contact():
    return render_template("contact.html")
