"""
detector.py
「超短時間爆発」の判定ロジック。

一次フィルタ(絶対値): 投稿からTHRESHOLD_HOURS時間以内に
                       IMPRESSION_THRESHOLD インプレッションに到達
二次スコア(相対値):   同じ条件を満たした投稿の中で、伸び速度(imp/時間)でランキング
"""

from dataclasses import dataclass
from datetime import datetime, timezone

# ── 設定値(必要に応じて調整) ─────────────────────────────
IMPRESSION_THRESHOLD = 5_000_000   # 500万インプレッション
THRESHOLD_HOURS = 6.0              # 「数時間」の目安。まずは6時間で運用し様子見
MIN_VELOCITY_FOR_ALERT = None      # 二次フィルタを追加したくなったらここに閾値を入れる
# ──────────────────────────────────────────────────────


@dataclass
class PostMetrics:
    post_id: str
    url: str
    author_handle: str
    text_snippet: str
    posted_at: datetime
    impressions: int
    likes: int
    retweets: int
    replies: int


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


def velocity(impressions: int, hours: float) -> float:
    """1時間あたりのインプレッション増加速度(概算)"""
    if hours <= 0:
        return float(impressions)
    return impressions / hours


def is_explosive(post: dict, now=None) -> bool:
    """
    一次フィルタ: 投稿後 THRESHOLD_HOURS 以内に IMPRESSION_THRESHOLD 到達したか
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    if hours > THRESHOLD_HOURS:
        return False
    if post.get("impressions", 0) < IMPRESSION_THRESHOLD:
        return False
    return True


def rank_candidates(posts: list[dict], now=None) -> list[dict]:
    """
    爆発候補を伸び速度でランキング(降順)。
    各要素に velocity_per_hour を付与して返す。
    """
    ranked = []
    for post in posts:
        hours = elapsed_hours(post["posted_at"], now=now)
        v = velocity(post.get("impressions", 0), hours)
        enriched = dict(post)
        enriched["elapsed_hours"] = round(hours, 2)
        enriched["velocity_per_hour"] = round(v, 0)
        ranked.append(enriched)
    ranked.sort(key=lambda p: p["velocity_per_hour"], reverse=True)
    return ranked


def filter_explosive(posts: list[dict], now=None) -> list[dict]:
    """収集した投稿群から、爆発条件を満たすものだけ抽出してランキング付きで返す"""
    candidates = [p for p in posts if is_explosive(p, now=now)]
    return rank_candidates(candidates, now=now)
