"""
db.py
SQLiteで投稿の検出履歴・通知済み状態を管理する。
「同じ投稿を何度も通知しない」ための重複防止が主目的。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "monitor.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id         TEXT PRIMARY KEY,      -- XのステータスID
    author_handle   TEXT,
    url             TEXT,
    posted_at       TEXT,                  -- 投稿時刻 (ISO8601)
    text_snippet    TEXT,                  -- 本文の先頭一部(ジャンル分類用)
    likes           INTEGER,
    retweets        INTEGER,
    replies         INTEGER,
    quotes          INTEGER,
    bookmarks       INTEGER,
    genre           TEXT,                  -- Groqで分類したジャンル
    detected_at     TEXT,                  -- 「爆発」判定された時刻
    notified        INTEGER DEFAULT 0,     -- 0/1 通知済みフラグ
    notified_at     TEXT
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    posts_scanned   INTEGER,
    posts_flagged   INTEGER,
    status          TEXT,                  -- success / error
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def is_known(post_id: str) -> bool:
    """既に収集済み(=通知判定済み)の投稿かどうか"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        return row is not None


def upsert_post(post: dict):
    """投稿データを保存/更新する"""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO posts (
                post_id, author_handle, url, posted_at, text_snippet,
                likes, retweets, replies, quotes, bookmarks, genre, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                likes = excluded.likes,
                retweets = excluded.retweets,
                replies = excluded.replies,
                quotes = excluded.quotes,
                bookmarks = excluded.bookmarks,
                genre = excluded.genre
            """,
            (
                post["post_id"],
                post.get("author_handle"),
                post.get("url"),
                post.get("posted_at"),
                post.get("text_snippet"),
                post.get("likes"),
                post.get("retweets"),
                post.get("replies"),
                post.get("quotes"),
                post.get("bookmarks"),
                post.get("genre"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def mark_notified(post_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE posts SET notified = 1, notified_at = ? WHERE post_id = ?",
            (datetime.now(timezone.utc).isoformat(), post_id),
        )


def log_run(started_at, finished_at, scanned, flagged, status, error_message=None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO collection_runs
                (started_at, finished_at, posts_scanned, posts_flagged, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (started_at, finished_at, scanned, flagged, status, error_message),
        )


def recent_flagged(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM posts
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_meta(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
