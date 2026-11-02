import os
from dotenv import load_dotenv
if os.environ.get("BATTLELOG_SKIP_DOTENV") != "1":
    load_dotenv()

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unicodedata
import subprocess
from flask import (
    Flask, request, render_template, jsonify, redirect, url_for,
    send_from_directory, Response
)
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
from battlelog_admin_store import current_season, season_list
from vote_api import vote_api

from battlelog_safety import (configure_app, data_path, nonnegative_int,
                              validate_attacks, validate_search, validate_season)
from battlelog_web import upload_image, confirm_upload

from defense_suggester import suggest_defense_teams, suggest_team_for_template

app = Flask(__name__)
configure_app(app)
from battlelog_admin import register_admin
register_admin(app)


@app.before_request
def remove_www():
    if request.host.startswith("www."):
        new_url = request.url.replace("://www.", "://", 1)
        return redirect(new_url, code=301)


app.register_blueprint(vote_api)

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

@app.route("/", methods=["GET"])
def index():
    loser_teams = get_latest_loser_teams(5)
    return render_template(
        "index.html",
        SEASON_LIST=season_list(),
        CURRENT_SEASON=current_season(),
        loser_teams=loser_teams
    )

@app.route("/healthz", methods=["GET"])
def health():
    return jsonify(status="ok")

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    uploads_dir = str(data_path("uploads"))
    return send_from_directory(uploads_dir, filename)

@app.route("/upload", methods=["GET", "POST"])
def upload():
    return upload_image(current_season(), season_list(), "general", "main", "uploaded_file")

@app.route("/upload/confirm", methods=["POST"])
def upload_confirm():
    return confirm_upload(current_season(), season_list(), "general",
                          update_spreadsheet, fetch_latest_output_row_as_dict,
                          append_battlelog_row_from_api, "upload_complete")

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
        SEASON_LIST=season_list(),
        CURRENT_SEASON=current_season(),
        request=request,
        loser_teams=loser_teams
    )

@app.route("/api/search", methods=["POST"])
def api_search():
    try:
        from datetime import datetime

        data = request.get_json(silent=True)
        season, characters, ranges, covers = validate_search(data, season_list(), current_season())
        side = data["side"]
        only_limited = data.get("only_limited", False)

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
            good_count = nonnegative_int(row.get("GOOD"))
            bad_count = nonnegative_int(row.get("BAD"))

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
        return jsonify({"results": response})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"/api/search エラー: {e}")
        return jsonify({"error": "処理に失敗しました。しばらくしてからお試しください。"}), 503

# ▼▼▼ 防衛編成メーカー ▼▼▼
@app.route("/defense_suggest", methods=["GET", "POST"])
def defense_suggest():
    message = None
    result = None
    try:
        season = validate_season(request.form.get("season"), season_list(), current_season())
    except ValueError as error:
        return str(error), 400
    striker_list = get_striker_list_from_sheet()
    special_list = get_special_list_from_sheet()
    attack_teams = [[None for _ in range(6)] for _ in range(5)]
    strict_pos = bool(request.form.get("strict_pos")) if request.method == "POST" else False

    if request.method == "POST":
        # ★ここでprintでデバッグ！
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
            app.logger.exception("Defense suggestion failed")
            message = "提案を作成できませんでした。しばらくしてからお試しください。"
    else:
        attack_teams = [[None for _ in range(6)] for _ in range(5)]
    return render_template(
        "defense_suggest.html",
        striker_list=striker_list,
        special_list=special_list,
        SEASON_LIST=season_list(),
        CURRENT_SEASON=season,
        message=message,
        result=result,
        strict_pos=strict_pos,
        attack_teams=attack_teams
    )

# ▼▼ notices（お知らせ一覧ページ） ▼▼
@app.route("/notices")
def notices():
    return render_template("notices.html")

# ▼▼▼ テンプレ決定過程テーブルAPI ▼▼▼
@app.route("/api/template_detail", methods=["POST"])
def api_template_detail():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("入力内容を確認してください。")
        template_tags = data.get("template_tags")
        attacks = data.get("attacks")
        season = validate_season(data.get("season"), season_list(), current_season())
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


@app.route("/privacy.html")
def privacy():
    return render_template("privacy.html")

@app.route('/guide')
def guide():
    return render_template('guide.html')

@app.route('/tips')
def tips():
    return render_template('tips.html')

# Tips記事（article01～article15）
for i in range(1, 16):
    route_path = f"/tips/article{str(i).zfill(2)}"
    template_name = f"tips_article{str(i).zfill(2)}.html"
    def _tips_article(template_name=template_name):
        return render_template(template_name)
    _tips_article.__name__ = f"tips_article_{i:02d}"
    app.add_url_rule(route_path, view_func=_tips_article)

@app.route("/contact.html")
def contact():
    return render_template("contact.html")

@app.route('/ads.txt')
def ads_txt():
    return send_from_directory(app.root_path, "ads.txt")

@app.route("/sitemap.xml", methods=["GET"])
def sitemap():
    # ★ Tipsは除外。/notices は掲載して公開維持
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://bluearchive-battlelog-p.com/</loc><lastmod>2025-06-23</lastmod></url>
  <url><loc>https://bluearchive-battlelog-p.com/contact.html</loc><lastmod>2025-06-23</lastmod></url>
  <url><loc>https://bluearchive-battlelog-p.com/search</loc><lastmod>2025-06-23</lastmod></url>
  <url><loc>https://bluearchive-battlelog-p.com/defense_suggest</loc><lastmod>2025-06-23</lastmod></url>
  <url><loc>https://bluearchive-battlelog-p.com/guide</loc><lastmod>2025-06-23</lastmod></url>
  <url><loc>https://bluearchive-battlelog-p.com/notices</loc><lastmod>2025-08-22</lastmod></url>
  <url><loc>https://bluearchive-battlelog-p.com/privacy.html</loc><lastmod>2025-06-23</lastmod></url>
  <url><loc>https://bluearchive-battlelog-p.com/upload</loc><lastmod>2025-06-23</lastmod></url>
</urlset>'''
    return Response(xml, mimetype='application/xml')

@app.route("/robots.txt")
def robots():
    # ★ Tips配下はクロール対象外（ダミー保護）
    content = """User-agent: *
Disallow: /limited/
Disallow: /limited
Disallow: /upload/confirm
Disallow: /upload/complete
Disallow: /tips
Disallow: /tips/

Allow: /

Sitemap: https://bluearchive-battlelog-p.com/sitemap.xml
"""
    return Response(content, mimetype="text/plain")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
