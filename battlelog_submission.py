"""Durable confirmation states; never blindly repeat an ambiguous remote write."""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from battlelog_safety import data_path, file_lock


class SubmissionPending(RuntimeError):
    pass


@contextmanager
def connection():
    path = data_path(".battlelog_runtime/submissions.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=30)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA synchronous=FULL")
    database.execute("""CREATE TABLE IF NOT EXISTS submissions (
        token TEXT PRIMARY KEY, kind TEXT NOT NULL, season TEXT NOT NULL,
        state TEXT NOT NULL, payload_hash TEXT, row_id TEXT, updated REAL NOT NULL
    )""")
    database.commit()
    try:
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def issue_token(kind, season):
    token = uuid.uuid4().hex
    with connection() as database:
        database.execute("INSERT INTO submissions(token,kind,season,state,updated) VALUES(?,?,?,?,?)",
                         (token, kind, season, "prepared", time.time()))
    return token


def mark(token, state, row_id=None):
    with connection() as database:
        database.execute("UPDATE submissions SET state=?, row_id=COALESCE(?,row_id), updated=? WHERE token=?",
                         (state, row_id, time.time(), token))


def submit(token, kind, season, fields, insert, convert, fetch, append):
    if not isinstance(token, str) or len(token) != 32:
        raise ValueError("確認画面の有効期限が切れています。画像を読み込み直してください。")
    if len(fields) != 18 or any(not isinstance(v, str) or len(v) > 500 for v in fields):
        raise ValueError("登録内容を確認してください。")
    digest = hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()
    # Lock spans ID allocation, insert, GAS and output lookup, across workers.
    with file_lock("sheets"):
        with connection() as database:
            record = database.execute("SELECT * FROM submissions WHERE token=?", (token,)).fetchone()
            if not record or record["kind"] != kind or record["season"] != season:
                raise ValueError("確認画面が無効です。画像を読み込み直してください。")
            state, row_id = record["state"], record["row_id"]
            if state == "complete":
                return
            if record["payload_hash"] and record["payload_hash"] != digest:
                raise ValueError("送信済みの内容は変更できません。管理者にお問い合わせください。")
            if state in ("inserting", "converting", "uncertain"):
                raise SubmissionPending("登録状況を確認する必要があります。重複を避けるため再投稿せず、管理者にお問い合わせください。")
            database.execute("UPDATE submissions SET payload_hash=?,updated=? WHERE token=?", (digest, time.time(), token))
        if state == "prepared":
            mark(token, "inserting")
            try:
                row_id = insert(fields, season=season)
            except Exception as error:
                mark(token, "uncertain")
                raise SubmissionPending("送信結果を確認できませんでした。重複を避けるため再投稿せず、管理者にお問い合わせください。") from error
            mark(token, "inserted", row_id)
            state = "inserted"
        if state == "inserted":
            mark(token, "converting")
            try:
                convert(season)
            except Exception as error:
                mark(token, "uncertain")
                raise SubmissionPending("データの変換結果を確認できませんでした。再投稿せず、管理者にお問い合わせください。") from error
            mark(token, "converted")
        # Retrying this stage only reads Sheets and upserts the cache.
        row = fetch(season=season, expected_id=row_id, expected_row=fields)
        append(row, season=season, source="限定")
        mark(token, "complete")
