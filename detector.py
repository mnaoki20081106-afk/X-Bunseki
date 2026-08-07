"""
detector.py
「超短時間爆発」の判定ロジック(v2: 引用・ブックマーク優先版)

インプレッション数は無料では技術的に取得できないため、
公開データだけで取れる以下の指標を使う。

判定基準(投稿後 THRESHOLD_HOURS 以内に、以下をすべて満たす):
  - 引用(quotes)    >= QUOTE_THRESHOLD
  - ブックマーク数   >= BOOKMARK_THRESHOLD
  - いいね数         >= LIKE_THRESHOLD

ランキング優先順位(以下の順で重み付けしたスコア):
  1. 引用の伸び速度(1時間あたり)
  2. ブックマークの伸び速度(1時間あたり)
  3. いいねの絶対数
"""

from dataclasses import dataclass
from datetime import datetime, timezone

# ── 設定値(投稿後1時間の目安値。運用しながら調整すること) ──────────
THRESHOLD_HOURS = 1.0

QUOTE_THRESHOLD = 50        # 引用: 50〜100以上 → 下限の50を採用(緩め側)
BOOKMARK_THRESHOLD = 100     # ブックマーク: 100〜300以上 → 下限の100を採用
LIKE_THRESHOLD = 500         # いいね: 500〜1000以上 → 下限の500を採用

# ランキングスコアの重み(優先順位 引用 > ブックマーク > いいね を反映)
WEIGHT_QUOTE_VELOCITY = 3.0
WEIGHT_BOOKMARK_VELOCITY = 2.0
WEIGHT_LIKE_COUNT = 1.0
# ──────────────────────────────────────────────────────────


@dataclass
class PostMetrics:
    post_id: str
    url: str
    author_handle: str
    text_snippet: str
    posted_at: datetime
    likes: int
    retweets: int
    replies: int
    quotes: int
    bookmarks: int


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # ISO8601文字列を想定。Apify Actorの出力形式に応じて調整すること。
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt


def elapsed_hours(posted_at, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    posted = _parse_dt(posted_at)
    delta = now - posted
    return delta.total_seconds() / 3600.0


def _velocity(count: int, hours: float) -> float:
    """1時間あたりの増加速度(概算)。経過時間が短すぎる場合は過大評価を避けるため下限を設ける。"""
    safe_hours = max(hours, 0.25)  # 15分未満は15分として計算(初動のブレを抑える)
    return count / safe_hours


def is_explosive(post: dict, now=None) -> bool:
    """
    一次フィルタ: 投稿後 THRESHOLD_HOURS 以内に、
    引用・ブックマーク・いいねの全ての閾値を満たしたか
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    if hours > THRESHOLD_HOURS:
        return False
    if post.get("quotes", 0) < QUOTE_THRESHOLD:
        return False
    if post.get("bookmarks", 0) < BOOKMARK_THRESHOLD:
        return False
    if post.get("likes", 0) < LIKE_THRESHOLD:
        return False
    return True


def rank_candidates(posts: list[dict], now=None) -> list[dict]:
    """
    爆発候補を「引用速度 > ブックマーク速度 > いいね数」の優先順位でスコアリングし、
    降順にランキングする。
    """
    ranked = []
    for post in posts:
        hours = elapsed_hours(post["posted_at"], now=now)
        quote_v = _velocity(post.get("quotes", 0), hours)
        bookmark_v = _velocity(post.get("bookmarks", 0), hours)
        likes = post.get("likes", 0)

        score = (
            WEIGHT_QUOTE_VELOCITY * quote_v
            + WEIGHT_BOOKMARK_VELOCITY * bookmark_v
            + WEIGHT_LIKE_COUNT * likes
        )

        enriched = dict(post)
        enriched["elapsed_hours"] = round(hours, 2)
        enriched["quote_velocity_per_hour"] = round(quote_v, 1)
        enriched["bookmark_velocity_per_hour"] = round(bookmark_v, 1)
        enriched["buzz_score"] = round(score, 1)
        ranked.append(enriched)

    ranked.sort(key=lambda p: p["buzz_score"], reverse=True)
    return ranked


def filter_explosive(posts: list[dict], now=None) -> list[dict]:
    """収集した投稿群から、爆発条件を満たすものだけ抽出してランキング付きで返す"""
    candidates = [p for p in posts if is_explosive(p, now=now)]
    return rank_candidates(candidates, now=now)
