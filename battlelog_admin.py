"""Authenticated browser management. Public pages never expose these controls."""
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from battlelog_admin_store import Conflict, JST, character_hash, effective_settings, store

admin = Blueprint("battlelog_admin", __name__, url_prefix="/admin", template_folder="admin_templates",
                  static_folder="admin_static", static_url_path="/assets")
log = logging.getLogger(__name__)
_attempts = {}
_attempt_lock = threading.Lock()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def auth_tag():
    return hashlib.sha256(os.environ.get("BATTLELOG_ADMIN_PASSWORD_HASH", "").encode()).hexdigest()


@admin.before_request
def protect():
    if request.endpoint == "battlelog_admin.static":
        return
    if not os.environ.get("BATTLELOG_ADMIN_PASSWORD_HASH") or not current_app.secret_key:
        return render_template("admin/login.html", unavailable=True), 503
    if request.method == "POST":
        supplied = request.form.get("csrf", "")
        if not supplied or not hmac.compare_digest(supplied, session.get("csrf", "")):
            abort(400, "Invalid form token")
    if request.endpoint != "battlelog_admin.login" and session.get("admin_auth") != auth_tag():
        return redirect(url_for("battlelog_admin.login"))


@admin.after_request
def private_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https:; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@admin.app_template_filter("admin_time")
def admin_time(value):
    if not value:
        return "未設定"
    try:
        try:
            instant = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            instant = None
        if instant is None:
            instant = datetime.fromtimestamp(float(value), timezone.utc)
        return instant.astimezone(JST).strftime("%Y/%m/%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "確認が必要です"


@admin.context_processor
def template_helpers():
    return {"csrf_token": csrf_token}


@admin.route("/login", methods=["GET", "POST"])
def login():
    error = None
    status = 200
    if request.method == "POST":
        address = request.remote_addr or "unknown"
        now = time.monotonic()
        with _attempt_lock:
            # Bound memory even when arbitrary addresses are supplied by a test/proxy.
            for key in list(_attempts):
                if now - _attempts[key][0] > 900:
                    del _attempts[key]
            start, count = _attempts.get(address, (now, 0))
            if count >= 10:
                error, status = "試行回数が多いため、15分ほど待ってからお試しください。", 429
            else:
                _attempts[address] = (start, count + 1)
        if not error:
            password = request.form.get("password", "")
            if len(password) <= 256 and check_password_hash(os.environ["BATTLELOG_ADMIN_PASSWORD_HASH"], password):
                with _attempt_lock:
                    _attempts.pop(address, None)
                session.clear()
                session["admin_auth"] = auth_tag()
                session.permanent = True
                return redirect(url_for("battlelog_admin.home"))
            error, status = "パスワードを確認してください。", 401
    return render_template("admin/login.html", error=error), status


@admin.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("battlelog_admin.login"))


def context():
    store.publish_due_season_notice()
    snapshot = store.read(force=True)
    current, published = effective_settings(snapshot["state"])
    planned = snapshot["state"].get("schedule")
    applied = bool(planned and datetime.now(timezone.utc) >= datetime.fromisoformat(planned["at"]))
    return {"snapshot": snapshot, "state": snapshot["state"], "current": current,
            "published": published, "planned": planned, "applied": applied}


def invalidate_characters():
    import spreadsheet_manager as manager
    manager._striker_cache["timestamp"] = 0
    manager._special_cache["timestamp"] = 0


def refresh_characters():
    import spreadsheet_manager as manager
    invalidate_characters()
    manager.get_striker_list_from_sheet()
    manager.get_special_list_from_sheet()
    if manager._striker_cache["timestamp"] == 0 or manager._special_cache["timestamp"] == 0:
        raise RuntimeError("Character cache refresh failed")


def perform(action, destination, success):
    try:
        action()
        flash(success, "success")
    except Conflict as error:
        flash(str(error), "error")
        return render_template("admin/error.html", message=str(error)), 409
    except ValueError as error:
        return render_template("admin/error.html", message=str(error)), 400
    except Exception:
        log.exception("Management action needs review")
        return render_template("admin/error.html", message="保存結果を確認できませんでした。履歴と現在の内容を確認してから操作してください。"), 503
    return redirect(url_for(destination))


@admin.errorhandler(Exception)
def read_failure(error):
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error
    log.exception("Management page unavailable")
    return render_template("admin/error.html", message="管理データを読み取れませんでした。しばらくしてから画面を読み込み直してください。"), 503


@admin.get("")
@admin.get("/")
def home():
    return render_template("admin/home.html", **context())


@admin.get("/characters")
def characters():
    chars = store.characters()
    selected = next((c for c in chars if c["kind"] == request.args.get("kind") and c["name"] == request.args.get("name")), None)
    return render_template("admin/characters.html", chars=chars, selected=selected,
                           base_hash=character_hash(selected) if selected else "", **context())


@admin.post("/characters/save")
def save_character():
    def action():
        store.save_character(request.form.get("revision"), request.form)
        invalidate_characters()
        try:
            refresh_characters()
        except Exception:
            log.exception("Saved character awaits cache refresh")
            flash("シートへの保存は完了しました。即時反映を再実行して、サイトへの反映を確認してください。", "error")
    return perform(action, "battlelog_admin.characters", "キャラ情報を保存しました。")


@admin.post("/characters/refresh")
def refresh():
    return perform(refresh_characters, "battlelog_admin.characters", "サイトのキャラ一覧を更新しました。")


@admin.post("/characters/delete")
def delete_character():
    def action():
        store.delete_character(request.form.get("revision"), request.form.get("kind"), request.form.get("name"), request.form.get("base_hash"))
        invalidate_characters()
        try:
            refresh_characters()
        except Exception:
            flash("削除は完了しました。サイトへ今すぐ反映を再実行してください。", "error")
    return perform(action, "battlelog_admin.characters", "キャラを削除しました。戦績データは残し、削除前の情報を履歴に保存しています。")


@admin.post("/characters/order")
def order_characters():
    def action():
        store.normalize_character_order(request.form.get("revision"))
        invalidate_characters()
    return perform(action, "battlelog_admin.characters", "キャラ一覧と辞書を、種類ごとの五十音順に整えました。")


@admin.post("/history/restore-deleted")
def restore_deleted():
    def action():
        store.restore_deleted_character(request.form.get("revision"), request.form.get("event"))
        invalidate_characters()
    return perform(action, "battlelog_admin.history", "削除前のキャラ情報を復元しました。")


@admin.get("/notices")
def notices():
    items = store.notices(force=True)
    selected = next((n for n in items if n["id"] == request.args.get("id")), None)
    return render_template("admin/notices.html", items=items, selected=selected,
                           today=datetime.now(JST).strftime("%Y-%m-%d"), **context())


@admin.post("/notices/save")
def save_notice():
    return perform(lambda: store.save_notice(request.form.get("revision"), request.form),
                   "battlelog_admin.notices", "お知らせを保存しました。公開にした内容はトップページへ反映されます。")


@admin.post("/history/restore-notice")
def restore_notice():
    return perform(lambda: store.restore_notice(request.form.get("revision"), request.form.get("event")),
                   "battlelog_admin.history", "お知らせを編集前の内容へ戻しました。")


@admin.get("/seasons")
def seasons():
    return render_template("admin/seasons.html", **context())


@admin.post("/seasons/prepare")
def prepare():
    def action():
        problems = store.prepare_season(request.form.get("revision"), request.form.get("season"))
        for problem in problems:
            flash(problem, "error")
    return perform(action, "battlelog_admin.seasons", "次シーズンのシートを準備しました。予約前に準備チェックを行ってください。")


@admin.post("/seasons/check")
def check():
    def action():
        problems = store.check_season(request.form.get("season"))
        if problems:
            raise ValueError(" ".join(problems))
    return perform(action, "battlelog_admin.seasons", "入力・出力・転送・辞書を確認しました。切り替えを予約できます。")


@admin.post("/seasons/schedule")
def schedule():
    return perform(lambda: store.set_schedule(request.form.get("revision"), request.form.get("season"), request.form.get("at")),
                   "battlelog_admin.seasons", "切り替えを予約しました。日時は日本時間です。")


@admin.post("/seasons/cancel")
def cancel():
    return perform(lambda: store.cancel_schedule(request.form.get("revision")), "battlelog_admin.seasons", "切り替え予約を解除しました。")


@admin.post("/seasons/current")
def change_current():
    return perform(lambda: store.set_current(request.form.get("revision"), request.form.get("season")),
                   "battlelog_admin.seasons", "現行シーズンを変更しました。戦績データは変更していません。")


@admin.get("/history")
def history():
    return render_template("admin/history.html", **context())


@admin.post("/history/restore")
def restore():
    def action():
        latest = store.read(force=True)
        event = next((e for e in latest["events"] if e["revision"] == request.form.get("event") and e["action"] == "character_saved"), None)
        if not event or not event["details"].get("before"):
            raise ValueError("戻せる編集履歴が見つかりません。")
        before, after = event["details"]["before"], event["details"]["after"]
        fields = {**before, "original": before["name"], "cover": "1" if before["cover"] else "0",
                  "base_hash": character_hash(after)}
        store.save_character(request.form.get("revision"), fields)
        invalidate_characters()
    return perform(action, "battlelog_admin.history", "編集前のキャラ情報へ戻しました。")


@admin.get("/status")
def status():
    import battlelog_journal as journal
    import spreadsheet_manager as manager
    records = []
    if journal.enabled():
        values = journal.worksheet().get_all_values()
        if not values or values[0] != journal.HEADERS:
            raise RuntimeError("Submission headers do not match")
        for row in values[1:]:
            record = dict(zip(journal.HEADERS, row + [""] * 7))
            if record.get("state") not in ("prepared", "complete"):
                records.append({k: record.get(k, "") for k in ("kind", "season", "state", "row_id", "updated")})
    chars = store.characters()
    invalid = [c for c in chars if not c["image"] or (c["kind"] == "STRIKER" and str(c["range"]) not in ("350", "450", "550", "650", "750", "850"))]
    cache_times = {"STRIKER": manager._striker_cache.get("timestamp") or None,
                   "SPECIAL": manager._special_cache.get("timestamp") or None}
    return render_template("admin/status.html", records=records[-30:][::-1], chars=chars, invalid=invalid,
                           cache_times=cache_times, **context())


def register_admin(app):
    app.secret_key = os.environ.get("BATTLELOG_ADMIN_SESSION_KEY")
    app.config.update(SESSION_COOKIE_NAME="battlelog_admin", SESSION_COOKIE_PATH="/admin",
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=14))
    app.register_blueprint(admin)
