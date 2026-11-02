"""Shared boundary checks, file locks and atomic JSON storage (no OCR changes)."""

import hashlib
import json
import logging
import os
import shutil
import struct
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
_locks = {}
_locks_guard = threading.Lock()
_depth = threading.local()


class OutputNotReady(RuntimeError):
    """The sheet has not exposed the submitted row yet; read-only retry is safe."""


def data_root():
    return Path(os.environ.get("BATTLELOG_DATA_DIR", os.getcwd())).resolve()


def data_path(name):
    path = Path(name)
    return path if path.is_absolute() else data_root() / path


def validate_season(value, seasons, current):
    value = current if value is None or value == "" else value
    if not isinstance(value, str) or value not in {s["key"] for s in seasons}:
        raise ValueError("シーズンの指定が正しくありません。")
    return value


def nonnegative_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def validate_search(data, seasons, current):
    if not isinstance(data, dict) or data.get("side") not in ("attack", "defense"):
        raise ValueError("検索条件が正しくありません。")
    season = validate_season(data.get("season"), seasons, current)
    chars = data.get("characters", [""] * 6)
    ranges = data.get("ranges", [""] * 4)
    covers = data.get("covers", [""] * 4)
    for values, length in ((chars, 6), (ranges, 4), (covers, 4)):
        if not isinstance(values, list) or len(values) != length:
            raise ValueError("検索条件の枠数が正しくありません。")
    if any(not isinstance(c, str) or len(c) > 120 for c in chars):
        raise ValueError("生徒名の指定が正しくありません。")
    allowed_ranges = {"", "350", "450", "550", "650", "750", "850"}
    if any(isinstance(v, bool) or not isinstance(v, (str, int)) or str(v) not in allowed_ranges for v in ranges):
        raise ValueError("射程の指定が正しくありません。")
    if any(not isinstance(v, str) or v.lower() not in ("", "0", "1", "true", "false", "yes", "no", "on", "off") for v in covers):
        raise ValueError("遮蔽の指定が正しくありません。")
    if type(data.get("only_limited", False)) is not bool:
        raise ValueError("検索条件が正しくありません。")
    if not any(chars + ranges + covers):
        raise ValueError("検索条件を1つ以上選択してください。")
    return season, chars, ranges, covers


def validate_attacks(attacks):
    if not isinstance(attacks, list) or not 1 <= len(attacks) <= 5:
        raise ValueError("攻撃編成は1件から5件まで指定してください。")
    for attack in attacks:
        if not isinstance(attack, list) or len(attack) != 6 or any(not isinstance(c, str) or len(c) > 120 for c in attack):
            raise ValueError("攻撃編成の指定が正しくありません。")
    return attacks


@contextmanager
def file_lock(name, timeout=120):
    """Serialize threads and worker processes sharing the same data directory."""
    key = hashlib.sha256(str(name).encode("utf-8")).hexdigest()
    with _locks_guard:
        mutex = _locks.setdefault(key, threading.RLock())
    if not mutex.acquire(timeout=timeout):
        raise TimeoutError("処理が混み合っています。しばらくしてからお試しください。")
    depths = getattr(_depth, "values", None)
    if depths is None:
        depths = _depth.values = {}
    handle = None
    try:
        if not depths.get(key):
            folder = data_path(".battlelog_runtime/locks")
            folder.mkdir(parents=True, exist_ok=True)
            handle = open(folder / (key + ".lock"), "a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("処理が混み合っています。しばらくしてからお試しください。")
                    time.sleep(0.05)
        depths[key] = depths.get(key, 0) + 1
        try:
            yield
        finally:
            depths[key] -= 1
            if handle:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle:
            handle.close()
        mutex.release()


def read_json(path):
    """Missing is None; a valid empty cache is []. Never erase a broken file."""
    path = Path(path)
    if not path.exists() and not Path(str(path) + ".bak").exists():
        return None
    for candidate in (path, Path(str(path) + ".bak")):
        try:
            with candidate.open(encoding="utf-8") as stream:
                rows = json.load(stream)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Invalid cache structure")
            if candidate != path:
                log.warning("Using previous cache snapshot: %s", path.name)
            return rows
        except (OSError, ValueError):
            continue
    raise RuntimeError("検索データを読み込めませんでした。管理者にお問い合わせください。")


def atomic_json(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path.resolve())):
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(rows, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            # A corrupt primary must not overwrite its last valid backup.
            if path.exists():
                try:
                    with path.open(encoding="utf-8") as stream:
                        previous = json.load(stream)
                    if isinstance(previous, list) and all(isinstance(row, dict) for row in previous):
                        backup_tmp = str(temporary) + ".bak"
                        try:
                            shutil.copyfile(path, backup_tmp)
                            os.replace(backup_tmp, str(path) + ".bak")
                        finally:
                            if os.path.exists(backup_tmp):
                                os.unlink(backup_tmp)
                except ValueError:
                    pass
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def save_upload(file):
    """Only the storage boundary changes; image bytes reach OCR unchanged."""
    header = file.stream.read(32)
    file.stream.seek(0)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
        if len(header) < 24:
            raise ValueError("画像ファイルを確認してください。")
        width, height = struct.unpack(">II", header[16:24])
        if not width or not height or width * height > 50_000_000:
            raise ValueError("画像のサイズが大きすぎます。")
    elif header.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        extension = ".webp"
    elif header.startswith(b"BM"):
        extension = ".bmp"
    elif header[:4] in (b"II*\x00", b"MM\x00*"):
        extension = ".tiff"
    else:
        raise ValueError("PNG・JPEG・WebP・BMP・TIFF形式の画像を選択してください。")
    folder = data_path("uploads")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid.uuid4().hex + extension)
    try:
        with path.open("xb") as stream:
            file.save(stream)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def configure_app(app):
    from flask import jsonify, request
    from werkzeug.exceptions import HTTPException

    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("BATTLELOG_MAX_UPLOAD_MB", "32")) * 1024 * 1024

    @app.errorhandler(HTTPException)
    def http_error(error):
        if error.code == 413:
            message = "画像の容量が大きすぎます。容量を減らしてお試しください。"
        else:
            message = "リクエストを処理できませんでした。入力内容を確認してください。"
        if request.path.startswith("/api/"):
            return jsonify(error=message), error.code
        return message, error.code

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if request.path.startswith(("/upload", "/uploads/", "/api/")):
            response.headers["Cache-Control"] = "no-store"
        return response
