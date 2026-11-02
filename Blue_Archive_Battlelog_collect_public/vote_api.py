import logging
from flask import Blueprint, request, jsonify
from spreadsheet_manager import update_vote_count, get_output_sheet_cache
from battlelog_admin_store import current_season, season_list
from battlelog_safety import nonnegative_int, validate_season

vote_api = Blueprint("vote_api", __name__)
log = logging.getLogger(__name__)


@vote_api.route("/api/vote", methods=["POST"])
def vote_endpoint():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("評価の指定が正しくありません。")
        row_id = data.get("row_id")
        if type(row_id) not in (str, int) or not str(row_id).isascii() or not str(row_id).isdigit() or len(str(row_id)) > 20:
            raise ValueError("評価するデータを確認してください。")
        if data.get("vote_type") not in ("good", "bad") or type(data.get("current_status")) is not bool:
            raise ValueError("評価の指定が正しくありません。")
        # Missing season must never silently target the current season.
        if not isinstance(data.get("season"), str) or not data["season"]:
            raise ValueError("対象シーズンを指定してください。ページを再読み込みしてお試しください。")
        season = validate_season(data["season"], season_list(), current_season())
        vote_col = "GOOD" if data["vote_type"] == "good" else "BAD"
        updated = update_vote_count(str(row_id), vote_col, -1 if data["current_status"] else 1, season)
        if not updated:
            return jsonify(success=False, message="評価するデータを一意に確認できませんでした。"), 404
        entry = next(row for row in get_output_sheet_cache(season) if str(row.get("ID")) == str(row_id))
        return jsonify(success=True, good_count=nonnegative_int(entry.get("GOOD")),
                       bad_count=nonnegative_int(entry.get("BAD")), message="OK")
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400
    except Exception:
        log.exception("Vote update failed")
        return jsonify(success=False, message="評価結果を確認できませんでした。ページを再読み込みしてください。"), 503


@vote_api.route("/api/vote/health", methods=["GET"])
def vote_health():
    return jsonify(status="ok")
