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

# 「激アツ」判定: 絶対値の条件を満たした上で、さらに比率が高い投稿を特別扱いする
# - 引用 ÷ いいね が10%を超える → 「意見を付け加えたい」熱量が強いサイン
# - ブックマーク ÷ いいね が20%を超える → 「保存してでも残したい」心理が強いサイン
GEKIATSU_QUOTE_LIKE_RATIO = 0.10
GEKIATSU_BOOKMARK_LIKE_RATIO = 0.20
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
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt


def elapsed_hours(posted_at, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    posted = _parse_dt(posted_at)
    delta = now - posted
    return delta.total_seconds() / 3600.0


def _velocity(count: int, hours: float) -> float:
    """1時間あたりの増加速度(概算)。経過時間が短すぎる場合は過大評価を避けるため下限を設ける。"""
    safe_hours = max(hours, 0.25)
    return count / safe_hours


def is_explosive(post: dict, now=None) -> bool:
    """
    一次フィルタ: 投稿後 THRESHOLD_HOURS 以内に、
    引用・ブックマーク・いいねの全ての閾値を満たしたか

    ★引用数は、Xが数字として公開していないため、実際に「View quotes」
    リンク先の一覧を開いて件数を数える近似値(playwright_collector.pyの
    _count_quote_tweets参照)。スクロールで確認できた範囲の件数なので、
    非常に多くの引用がある投稿では実際より少なく出ることがある。
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


def _compute_ratios(post: dict) -> tuple[float, float]:
    """
    (引用÷いいね, ブックマーク÷いいね) の比率を計算する。
    いいねが0の場合はゼロ除算を避けて0.0を返す。
    """
    likes = post.get("likes", 0)
    if likes <= 0:
        return 0.0, 0.0
    quote_ratio = post.get("quotes", 0) / likes
    bookmark_ratio = post.get("bookmarks", 0) / likes
    return quote_ratio, bookmark_ratio


def is_gekiatsu(post: dict) -> bool:
    """
    「激アツ」判定: 引用÷いいね と ブックマーク÷いいね の両方が
    閾値を超えているか(絶対値の閾値は is_explosive 側で別途チェック済み前提)
    """
    quote_ratio, bookmark_ratio = _compute_ratios(post)
    return (
        quote_ratio >= GEKIATSU_QUOTE_LIKE_RATIO
        and bookmark_ratio >= GEKIATSU_BOOKMARK_LIKE_RATIO
    )


def rank_candidates(posts: list[dict], now=None) -> list[dict]:
    """
    爆発候補を「引用速度 > ブックマーク速度 > いいね数」の優先順位でスコアリングし、
    降順にランキングする。あわせて「激アツ」判定・比率も付与する。
    """
    ranked = []
    for post in posts:
        hours = elapsed_hours(post["posted_at"], now=now)
        quote_v = _velocity(post.get("quotes", 0), hours)
        bookmark_v = _velocity(post.get("bookmarks", 0), hours)
        likes = post.get("likes", 0)
        quote_ratio, bookmark_ratio = _compute_ratios(post)

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
        enriched["quote_like_ratio"] = round(quote_ratio, 3)
        enriched["bookmark_like_ratio"] = round(bookmark_ratio, 3)
        enriched["is_gekiatsu"] = is_gekiatsu(post)
        ranked.append(enriched)

    ranked.sort(key=lambda p: p["buzz_score"], reverse=True)
    return ranked


def filter_explosive(posts: list[dict], now=None) -> list[dict]:
    """収集した投稿群から、爆発条件を満たすものだけ抽出してランキング付きで返す"""
    candidates = [p for p in posts if is_explosive(p, now=now)]
    return rank_candidates(candidates, now=now)


def explosive_progress(post: dict, now=None) -> float:
    """
    「爆発条件にどれだけ近づいているか」を0.0〜1.0+の値で返す。
    引用・ブックマーク・いいね、それぞれの「閾値に対する達成率」を計算し、
    その中で最も低い値(=一番足りていない項目)を採用する。
    1.0以上なら、その投稿は既に is_explosive の条件を満たしている。

    「動作確認」通知で、まだ通知条件は満たしていないが最も近い投稿を
    見せるために使う(早期発見の目安として)。
    投稿からの経過時間が THRESHOLD_HOURS を超えている場合は対象外として 0.0 を返す
    (もう通知され得ない投稿なので、近さを見る意味が無いため)。
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    if hours > THRESHOLD_HOURS:
        return 0.0

    quote_progress = post.get("quotes", 0) / QUOTE_THRESHOLD if QUOTE_THRESHOLD else 1.0
    bookmark_progress = post.get("bookmarks", 0) / BOOKMARK_THRESHOLD if BOOKMARK_THRESHOLD else 1.0
    like_progress = post.get("likes", 0) / LIKE_THRESHOLD if LIKE_THRESHOLD else 1.0

    return min(quote_progress, bookmark_progress, like_progress)


def rank_by_progress(posts: list[dict], now=None, limit: int = 5) -> list[dict]:
    """
    「爆発条件への近さ」順に投稿を並べ、上位limit件を返す。
    各投稿には progress(0.0〜1.0+)フィールドを付与する。
    早期発見・動作確認の目的で使う。
    """
    enriched = []
    for post in posts:
        p = dict(post)
        p["progress"] = round(explosive_progress(post, now=now), 3)
        enriched.append(p)

    enriched.sort(key=lambda p: p["progress"], reverse=True)
    return enriched[:limit]
