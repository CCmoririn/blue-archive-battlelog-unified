import os
from dotenv import load_dotenv
load_dotenv()

import sys
import unicodedata
import subprocess
import requests
from flask import Flask, request, render_template, jsonify, redirect, url_for, send_from_directory
from spreadsheet_manager import (
    update_spreadsheet,
    get_striker_list_from_sheet,
    get_special_list_from_sheet,
    search_battlelog_output_sheet,
    get_other_icon,
    load_other_icon_cache,
    get_latest_loser_teams,
    fetch_latest_output_row_as_dict,  # ★追加
    append_battlelog_row_from_api      # ★追加
)
from config import CURRENT_SEASON, SEASON_LIST
from vote_api import vote_api

from defense_suggester import suggest_defense_teams, suggest_team_for_template

app = Flask(__name__)

app.register_blueprint(vote_api)

NEW_DOMAIN = "bluearchive-battlelog-p.com"  # 新ドメイン名のみ

@app.before_request
def redirect_to_custom_domain():
    if "onrender.com" in request.host:
        new_url = request.url.replace(request.host, NEW_DOMAIN)
        if not new_url.startswith("https://"):
            new_url = "https://" + new_url.split("://", 1)[-1]
        return redirect(new_url, code=301)

if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    print("GOOGLE_APPLICATION_CREDENTIALS not found in environment variables.")

load_other_icon_cache()

def normalize_sp_chars(chars: list, side: str) -> list:
    if not chars or len(chars) != 6:
        return chars
    main = chars[:4]
    sp = sorted(chars[4:6])
    return main + sp

def match_team(query, target, side):
    return normalize_sp_chars(query, side) == normalize_sp_chars(target, side)

@app.route("/", methods=["GET"])
def index():
    loser_teams = get_latest_loser_teams(5)
    return render_template(
        "index.html",
        SEASON_LIST=SEASON_LIST,
        CURRENT_SEASON=CURRENT_SEASON,
        loser_teams=loser_teams
    )

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    uploads_dir = os.path.abspath("uploads")
    return send_from_directory(uploads_dir, filename)

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html", SEASON_LIST=SEASON_LIST, CURRENT_SEASON=CURRENT_SEASON)
    file = request.files.get("image_file")
    if not file or file.filename == "":
        return "画像ファイルが選択されていません。", 400
    uploads_dir = os.path.abspath("uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    file_path = os.path.join(uploads_dir, file.filename)
    file.save(file_path)
    try:
        from main import process_image
        season = request.form.get("season", CURRENT_SEASON)
        row_data = process_image(file_path, season=season)
        labels = [
            "日付", "攻撃側プレイヤー", "攻撃結果",
            "攻撃キャラ1", "攻撃キャラ2", "攻撃キャラ3",
            "攻撃キャラ4", "攻撃キャラ5", "攻撃キャラ6",
            "（空白）", "防衛側プレイヤー", "防衛結果",
            "防衛キャラ1", "防衛キャラ2", "防衛キャラ3",
            "防衛キャラ4", "防衛キャラ5", "防衛キャラ6"
        ]
        preview_image_url = f"/uploads/{file.filename}"
        return render_template(
            "confirm.html",
            row_data=row_data,
            labels=labels,
            SEASON_LIST=SEASON_LIST,
            CURRENT_SEASON=season,
            preview_image_url=preview_image_url
        )
    except Exception as e:
        print(f"render_template失敗: {e}")
        return render_template(
            "complete.html",
            message=f"エラーが発生しました: {e}"
        )

@app.route("/upload/confirm", methods=["POST"])
def upload_confirm():
    try:
        from main import call_apps_script

        row_data = [
            request.form.get(f"field{i}", "")
            for i in range(18)
        ]
        row_data = [unicodedata.normalize("NFKC", v) for v in row_data]
        season = request.form.get("season", CURRENT_SEASON)
        # 変換前への出力
        update_spreadsheet(row_data, season=season)

        # しらす式GAS実行（seasonを引数で渡す）
        subprocess.run(
            [sys.executable, "call_gas.py", season],
            check=True
        )

        # ★GAS変換が終わった後に、最新行（3行目）のみキャッシュへ追加！
        row_dict = fetch_latest_output_row_as_dict(season=season)
        append_battlelog_row_from_api(row_dict, season=season, source="限定")

        return redirect(url_for("upload_complete"))
    except subprocess.CalledProcessError as e:
        print(f"しらす式変換エラー: {e}")
        return render_template(
            "complete.html",
            message=f"しらす式変換が失敗しました: {e}"
        )
    except Exception as e:
        print(f"スプレッドシート更新エラー: {e}")
        return render_template(
            "complete.html",
            message=f"スプレッドシートの更新に失敗しました: {e}"
        )

@app.route("/upload/complete", methods=["GET"])
def upload_complete():
    return render_template(
        "complete.html",
        message="アップロードが完了しました"
    )

@app.route("/search", methods=["GET"])
def search():
    try:
        striker_list = get_striker_list_from_sheet()
        special_list = get_special_list_from_sheet()
    except Exception as e:
        print(f"キャラリスト取得エラー: {e}")
        striker_list = []
        special_list = []
    loser_teams = get_latest_loser_teams(5)
    return render_template("db.html",
        striker_list=striker_list,
        special_list=special_list,
        SEASON_LIST=SEASON_LIST,
        CURRENT_SEASON=CURRENT_SEASON,
        request=request,
        loser_teams=loser_teams
    )

@app.route("/api/search", methods=["POST"])
def api_search():
    try:
        from datetime import datetime

        data = request.json
        if not data:
            return jsonify({"error": "No data received"}), 400
        side = data.get("side")
        characters = data.get("characters")
        ranges = data.get("ranges", ["", "", "", ""])
        covers = data.get("covers", ["", "", "", ""])
        only_limited = data.get("only_limited", False)
        season = data.get("season", CURRENT_SEASON)

        if side not in ["attack", "defense"]:
            return jsonify({"error": "Invalid parameters"}), 400

        has_any = False
        for i in range(4):
            if (characters and len(characters) == 6 and characters[i]) or ranges[i] or covers[i]:
                has_any = True
        for i in range(4, 6):
            if characters and len(characters) == 6 and characters[i]:
                has_any = True
        if not has_any:
            return jsonify({"error": "検索条件を1つ以上選択してください。"}), 400

        matched_rows = search_battlelog_output_sheet(
            query=characters or [""]*6,
            search_side=side,
            season=season,
            only_limited=only_limited,
            ranges=ranges,
            covers=covers,
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

            row_id = row.get("ID") or row.get("row_id") or ""
            good_count = int(row.get("GOOD", 0))
            bad_count = int(row.get("BAD", 0))

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
                    "row_id": row_id,
                    "good_count": good_count,
                    "bad_count": bad_count,
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
                    "row_id": row_id,
                    "good_count": good_count,
                    "bad_count": bad_count,
                })
        print("API返却データ:", response)
        return jsonify({"results": response})
    except Exception as e:
        print(f"/api/search エラー: {e}")
        return jsonify({"error": str(e)}), 500

# ▼▼▼ 防衛編成メーカー ▼▼▼
@app.route("/defense_suggest", methods=["GET", "POST"])
def defense_suggest():
    message = None
    result = None
    season = request.form.get("season", CURRENT_SEASON)
    striker_list = get_striker_list_from_sheet()
    special_list = get_special_list_from_sheet()
    attack_teams = [[None for _ in range(6)] for _ in range(5)]
    strict_pos = bool(request.form.get("strict_pos")) if request.method == "POST" else False

    if request.method == "POST":
        for i in range(5):
            print([request.form.get(f"attack_{i}_{j}", "") for j in range(6)])
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
            result = suggest_defense_teams(attacks, season=season, strict_pos=strict_pos)
        except Exception as e:
            message = f"提案ロジック実行時にエラーが発生しました: {e}"
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

# ▼▼▼ テンプレ決定過程テーブルAPI ▼▼▼
@app.route("/api/template_detail", methods=["POST"])
def api_template_detail():
    try:
        data = request.json
        template_tags = data.get("template_tags")
        attacks = data.get("attacks")
        season = data.get("season", CURRENT_SEASON)
        strict_pos = bool(data.get("strict_pos"))
        if not attacks or not any(any(x) for x in attacks):
            attacks = [["", "", "", "", "", ""]]
        res = suggest_team_for_template(template_tags, attacks=attacks, season=season, strict_pos=strict_pos)
        return jsonify(res)
    except Exception as e:
        print(f"/api/template_detail エラー: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/privacy.html")
def privacy():
    return render_template("privacy.html")

@app.route('/guide')
def guide():
    return render_template('guide.html')

@app.route('/tips')
def tips():
    return render_template('tips.html')

@app.route('/tips/character-growth')
def tips_character_growth():
    return render_template('character-growth.html')

@app.route("/contact.html")
def contact():
    return render_template("contact.html")

@app.route('/ads.txt')
def ads_txt():
    return send_from_directory('.', 'ads.txt')

@app.route("/sitemap.xml", methods=["GET"])
def sitemap():
    from flask import Response
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://bluearchive-battlelog-p.com/</loc>
    <lastmod>2025-06-23</lastmod>
  </url>
  <url>
    <loc>https://bluearchive-battlelog-p.com/contact</loc>
    <lastmod>2025-06-23</lastmod>
  </url>
  <url>
    <loc>https://bluearchive-battlelog-p.com/search</loc>
    <lastmod>2025-06-23</lastmod>
  </url>
  <url>
    <loc>https://bluearchive-battlelog-p.com/defense_suggest</loc>
    <lastmod>2025-06-23</lastmod>
  </url>
  <url>
    <loc>https://bluearchive-battlelog-p.com/guide</loc>
    <lastmod>2025-06-23</lastmod>
  </url>
  <url>
    <loc>https://bluearchive-battlelog-p.com/notices</loc>
    <lastmod>2025-06-15</lastmod>
  </url>
  <url>
    <loc>https://bluearchive-battlelog-p.com/privacy</loc>
    <lastmod>2025-06-23</lastmod>
  </url>
  <url>
    <loc>https://bluearchive-battlelog-p.com/upload</loc>
    <lastmod>2025-06-23</lastmod>
  </url>
</urlset>'''
    return Response(xml, mimetype='application/xml')


@app.route("/robots.txt")
def robots():
    from flask import Response
    content = """User-agent: *
Disallow: /limited/
Disallow: /limited
Disallow: /upload/confirm
Disallow: /upload/complete

Allow: /

Sitemap: https://bluearchive-battlelog-p.com/sitemap.xml
"""
    return Response(content, mimetype="text/plain")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

