"""Upload/confirmation boundaries; the existing OCR modules stay untouched."""

import importlib
import logging
import unicodedata

from flask import redirect, render_template, request, url_for

from battlelog_gas import run_conversion, wait_for_input
from battlelog_safety import OutputNotReady, file_lock, save_upload, validate_season
from battlelog_submission import SubmissionPending, issue_token, submit

log = logging.getLogger(__name__)


def upload_image(current, seasons, kind, processor_module, image_endpoint):
    if request.method == "GET":
        return render_template("upload.html", SEASON_LIST=seasons, CURRENT_SEASON=current)
    file = request.files.get("image_file")
    if not file or not file.filename:
        return "画像ファイルが選択されていません。", 400
    try:
        season = validate_season(request.form.get("season"), seasons, current)
        path = save_upload(file)
    except ValueError as error:
        return str(error), 400
    try:
        # Existing OCR writes fixed temporary filenames. Serialize its callers,
        # rather than changing a single byte of the recognition implementation.
        with file_lock("ocr"):
            processor = importlib.import_module(processor_module)
            row_data = processor.process_image(str(path), season=season)
        token = issue_token(kind, season)
        labels = ["日付", "攻撃側プレイヤー", "攻撃結果"] + [f"攻撃キャラ{i}" for i in range(1, 7)]
        labels += ["（空白）", "防衛側プレイヤー", "防衛結果"] + [f"防衛キャラ{i}" for i in range(1, 7)]
        return render_template("confirm.html", row_data=row_data, labels=labels,
                               SEASON_LIST=seasons, CURRENT_SEASON=season,
                               confirmation_token=token,
                               preview_image_url=url_for(image_endpoint, filename=path.name))
    except Exception:
        log.exception("Image processing failed")
        path.unlink(missing_ok=True)
        return render_template("complete.html", message="画像を読み取れませんでした。画像を確認して、もう一度お試しください。"), 422


def confirm_upload(current, seasons, kind, insert, fetch, append, complete_endpoint):
    try:
        season = validate_season(request.form.get("season"), seasons, current)
        fields = [unicodedata.normalize("NFKC", request.form.get(f"field{i}", "")) for i in range(18)]
        submit(request.form.get("confirmation_token"), kind, season, fields,
               insert=insert, fetch=fetch, append=append,
               ready=(lambda season, row_id: wait_for_input(season, row_id)) if kind == "general" else None,
               convert=lambda season: run_conversion(season, limited=kind == "limited"))
        return redirect(url_for(complete_endpoint))
    except ValueError as error:
        return render_template("complete.html", message=str(error)), 400
    except SubmissionPending as error:
        log.exception("Submission needs reconciliation")
        return render_template("complete.html", message=str(error)), 503
    except OutputNotReady as error:
        return render_template("complete.html", message=str(error)), 503
    except Exception:
        log.exception("Submission cache reconciliation failed")
        return render_template("complete.html", message="登録内容の確認中に問題が発生しました。同じ内容を再投稿せず、管理者にお問い合わせください。"), 503
