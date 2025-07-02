from flask import Blueprint, request, jsonify
from spreadsheet_manager import update_vote_count, get_output_sheet_cache

vote_api = Blueprint("vote_api", __name__)

@vote_api.route("/api/vote", methods=["POST"])
def vote_endpoint():
    """
    グッド・バッド評価の加算・取り消し用APIエンドポイント

    POST JSON:
    {
        "row_id": <int or str>,
        "vote_type": "good" or "bad",
        "action": "toggle",         # トグル型（押してなければ+1、押していれば-1）
        "current_status": true/false, # 既に投票済みならtrue
        "season": "s9"             # 任意：指定がなければCURRENT_SEASON
    }

    レスポンス：
    {
        "success": true,
        "good_count": 5,
        "bad_count": 1,
        "message": "OK" or error
    }
    """
    try:
        data = request.get_json(force=True)
        row_id = data.get("row_id")
        vote_type = data.get("vote_type")  # "good" or "bad"
        action = data.get("action", "toggle")
        current_status = data.get("current_status", False)
        season = data.get("season", None)

        # 安全バリデーション
        if not row_id or vote_type not in ("good", "bad"):
            return jsonify({"success": False, "message": "row_idとvote_typeは必須"}), 400

        vote_col = "GOOD" if vote_type == "good" else "BAD"
        delta = -1 if current_status else 1  # 既に押していたら-1、押してなければ+1

        # 投票カウント更新（キャッシュ＆シート）
        updated = update_vote_count(row_id, vote_col, delta, season)

        if not updated:
            return jsonify({"success": False, "message": "該当データが見つかりません"}), 404

        # 最新カウント取得（キャッシュから探す）
        cache = get_output_sheet_cache(season)
        entry = next((row for row in cache if str(row.get("ID")) == str(row_id)), None)
        good_count = int(entry["GOOD"]) if entry and "GOOD" in entry else 0
        bad_count = int(entry["BAD"]) if entry and "BAD" in entry else 0

        return jsonify({
            "success": True,
            "good_count": good_count,
            "bad_count": bad_count,
            "message": "OK"
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"サーバーエラー: {e}"}), 500

# ↓ ここから下は任意（ヘルスチェックや管理用など）
@vote_api.route("/api/vote/health", methods=["GET"])
def vote_health():
    return jsonify({"status": "ok"})
