"""
db.py
SQLiteで投稿の検出履歴・通知済み状態を管理する。
「同じ投稿を何度も通知しない」ための重複防止が主目的。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

-- ★v4で追加。このプロジェクトの心臓部。
-- 収集した「全ての」投稿の指標を、実行のたびに1行ずつ追記していく。
-- 同じ post_id の行が複数たまることで初めて「前回からいくつ増えたか」
-- ＝ 本当の伸び率が計算できる。
-- v3までは通知した投稿しかDBに入れていなかったため、伸び率を
-- 計算する材料自体が存在しなかった。
CREATE TABLE IF NOT EXISTS observations (
    post_id         TEXT NOT NULL,
    observed_at     TEXT NOT NULL,         -- 観測時刻 (ISO8601)
    posted_at       TEXT,                  -- 投稿時刻 (ISO8601)
    likes           INTEGER DEFAULT 0,
    retweets        INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    quotes          INTEGER DEFAULT 0,
    bookmarks       INTEGER DEFAULT 0,
    impressions     INTEGER DEFAULT 0,
    PRIMARY KEY (post_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_observations_post
    ON observations (post_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_observations_time
    ON observations (observed_at);

-- 通知済みの話題(本文)を覚えておき、同じ出来事で何度も鳴らさないために使う。
CREATE TABLE IF NOT EXISTS notified_topics (
    post_id         TEXT PRIMARY KEY,
    notified_at     TEXT,
    text_snippet    TEXT
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


def get_last_notified_at_for_author(author_handle: str) -> str | None:
    """
    アカウント単位のクールダウン判定用: そのアカウントを最後に
    通知した時刻(ISO8601文字列)を取得する。未通知ならNone。
    meta テーブルを間借りして "author_last_notified:{handle}" という
    キーで保存している。
    """
    return get_meta(f"author_last_notified:{author_handle}")


def set_last_notified_at_for_author(author_handle: str, notified_at: str):
    set_meta(f"author_last_notified:{author_handle}", notified_at)


# ──────────────────────────────────────────────────────────
#  observations: 時系列の観測記録(v4で追加)
# ──────────────────────────────────────────────────────────

def record_observations(posts: list[dict], observed_at: str):
    """
    1回の実行で収集した投稿を、まとめて observations に記録する。

    ★重要: ここには「判定を通った投稿」ではなく「収集した全投稿」を入れる。
    今回は基準未達で通知しなかった投稿こそ、次回に伸び率を計算するための
    比較対象になるため。ここを絞ると伸び率が永久に計算できなくなる。
    """
    if not posts:
        return
    rows = [
        (
            p["post_id"], observed_at, p.get("posted_at"),
            p.get("likes") or 0, p.get("retweets") or 0, p.get("replies") or 0,
            p.get("quotes") or 0, p.get("bookmarks") or 0, p.get("impressions") or 0,
        )
        for p in posts
    ]
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO observations (
                post_id, observed_at, posted_at,
                likes, retweets, replies, quotes, bookmarks, impressions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id, observed_at) DO UPDATE SET
                likes = excluded.likes,
                retweets = excluded.retweets,
                replies = excluded.replies,
                quotes = excluded.quotes,
                bookmarks = excluded.bookmarks,
                impressions = excluded.impressions
            """,
            rows,
        )


def get_observation_history(post_ids: list[str]) -> dict[str, list[dict]]:
    """
    複数のpost_idの過去の観測履歴を、1回のクエリでまとめて取得する。
    戻り値: {post_id: [観測レコード(observed_at昇順), ...]}

    投稿ごとに個別クエリを投げると、1実行で数百回のクエリになってしまうため
    まとめて引く。
    """
    if not post_ids:
        return {}

    history: dict[str, list[dict]] = {pid: [] for pid in post_ids}
    with get_conn() as conn:
        # SQLiteのプレースホルダ上限(既定999)を考慮して分割する
        for i in range(0, len(post_ids), 400):
            chunk = post_ids[i:i + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT * FROM observations
                WHERE post_id IN ({placeholders})
                ORDER BY post_id, observed_at ASC
                """,
                chunk,
            ).fetchall()
            for row in rows:
                history[row["post_id"]].append(dict(row))
    return history


def prune_observations(keep_hours: int = 24):
    """
    古い観測記録を削除する。
    伸び率の計算に使うのはせいぜい直近数時間なので、それ以上は保持しない
    (GitHub Actionsのキャッシュに載せる都合上、DBは小さく保つ必要がある)。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_hours)).isoformat()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
        deleted = cur.rowcount
        conn.execute("DELETE FROM notified_topics WHERE notified_at < ?", (cutoff,))
    return deleted


def observation_stats() -> dict:
    """動作確認用: いま何件の観測が貯まっているか"""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT post_id) AS posts,
                   MIN(observed_at) AS oldest
            FROM observations
            """
        ).fetchone()
        return dict(row) if row else {}


# ──────────────────────────────────────────────────────────
#  話題(トピック)単位の重複通知防止(v4で追加)
# ──────────────────────────────────────────────────────────

def record_notified_topic(post_id: str, text_snippet: str, notified_at: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notified_topics (post_id, notified_at, text_snippet)
            VALUES (?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                notified_at = excluded.notified_at,
                text_snippet = excluded.text_snippet
            """,
            (post_id, notified_at, text_snippet),
        )


def recent_notified_texts(hours: float = 6.0) -> list[str]:
    """
    直近hours時間に通知した投稿の本文を返す。
    「同じ出来事についての別の投稿」を弾くための比較材料に使う。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT text_snippet FROM notified_topics WHERE notified_at >= ?",
            (cutoff,),
        ).fetchall()
        return [r["text_snippet"] or "" for r in rows]


def count_notifications_since(since_iso: str) -> int:
    """指定時刻以降に何件通知したか(1日あたりの通知上限を守るために使う)"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM notified_topics WHERE notified_at >= ?",
            (since_iso,),
        ).fetchone()
        return row["c"] if row else 0


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
    print(f"observations: {observation_stats()}")
