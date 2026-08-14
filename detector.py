"""
detector.py (v3)
「瞬間バズ」+「持続バズ(ニュース・災害系)」の両方を拾う判定ロジック。

v2からの変更点(Grokへのレビュー依頼を経て、2026-08に再設計):
  - 投稿後1時間固定だった判定を、「瞬間バズ(2時間以内・速度重視)」と
    「持続バズ(最大8時間・減衰する速度基準+インプレッション成長)」の
    2系統に分離。これにより「3時間かけて600万インプレッションに到達した
    ニュース投稿」のような、じわじわ伸びるタイプも拾えるようにした。
  - 引用数(quotes)を必須条件から外し、スコアリングの補助指標に格下げ。
    理由: Xが引用数を公開しておらず、実際には「View quotes一覧を数件
    スクロールして数えた近似値(下限値)」でしかないため、これを必須条件に
    すると不正確な値で通知の可否が左右されてしまう。
  - 「激アツ」判定を、引用・ブックマーク・返信の3つの比率のうち
    「2つ以上」満たせばOKという緩やかな基準に変更(元は引用+ブックマーク
    の両方が必須で厳しすぎた)。
  - Grok提案の「早期警告(進捗55%で通知)」は採用していない。理由:
    閾値未達の投稿まで通知対象にすると、本来の「本当にバズった投稿だけ
    知りたい」という目的に反し通知過多になるため。進捗情報自体は
    「動作確認」機能(rank_by_progress)で見れるようにしている。
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

# ── 時間窓 ──────────────────────────────────────────
INSTANT_MAX_HOURS = 2.0      # 瞬間バズの対象窓
SUSTAINED_MAX_HOURS = 8.0    # 持続バズの対象窓(ニュース系はここまで見る)

# ── 瞬間バズ用の閾値(絶対数 or 速度のどちらかを満たせばOK) ──
INSTANT_LIKE_THRESHOLD = 800
INSTANT_LIKE_VELOCITY = 400     # いいね/時間
INSTANT_RT_THRESHOLD = 50
INSTANT_RT_VELOCITY = 30        # リポスト/時間
INSTANT_REPLY_THRESHOLD = 30
INSTANT_REPLY_VELOCITY = 20     # 返信/時間

# ── 持続バズ用(速度の減衰しにくさ + インプレッション成長) ──
SUSTAINED_LIKE_VELOCITY_BASE = 250   # 投稿直後の基準速度(いいね/時間)
SUSTAINED_DECAY_FACTOR = 0.25        # 時間経過による要求速度の緩和係数
SUSTAINED_MIN_IMPRESSIONS = 50_000   # 詳細ページ取得後の最低インプレッション
SUSTAINED_MIN_AGE_HOURS = 0.3        # あまりに新しい投稿は持続バズ判定の対象外

# ── 激アツ判定(3指標中2つ以上を満たせばOK) ──
GEKIATSU_QUOTE_LIKE_RATIO = 0.04     # 引用÷いいね 4%以上(近似値なので緩め)
GEKIATSU_BOOKMARK_LIKE_RATIO = 0.08  # ブックマーク÷いいね 8%以上
GEKIATSU_REPLY_LIKE_RATIO = 0.05     # 返信÷いいね 5%以上(ニュース系で重要)

# ── ランキングスコアの重み ──
WEIGHT_LIKE_VELOCITY = 2.5
WEIGHT_RT_VELOCITY = 2.0
WEIGHT_REPLY_VELOCITY = 1.8
WEIGHT_IMPRESSION_GROWTH = 1.5
WEIGHT_QUOTE_APPROX = 0.8    # 引用は近似値なので重みを下げる(補助指標)
WEIGHT_BOOKMARK_VELOCITY = 1.2

# 速度計算時の経過時間の下限(15分未満は15分として計算し、投稿直後の
# ブレによる速度の過大評価を防ぐ)
MIN_VELOCITY_HOURS = 0.25
# ──────────────────────────────────────────────────────


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
    return max(delta.total_seconds() / 3600.0, 0.01)  # ゼロ除算防止


def _velocity(count: int, hours: float) -> float:
    """1時間あたりの増加速度(概算)。経過時間が短すぎる場合は過大評価を避けるため下限を設ける。"""
    safe_hours = max(hours, MIN_VELOCITY_HOURS)
    return count / safe_hours


def _compute_ratios(post: dict) -> tuple[float, float, float]:
    """(引用÷いいね, ブックマーク÷いいね, 返信÷いいね) の比率を計算する"""
    likes = max(post.get("likes", 0), 1)
    quote_ratio = post.get("quotes", 0) / likes
    bookmark_ratio = post.get("bookmarks", 0) / likes
    reply_ratio = post.get("replies", 0) / likes
    return quote_ratio, bookmark_ratio, reply_ratio


def is_instant_explosive(post: dict, now=None) -> bool:
    """
    瞬間バズ判定: 投稿後2時間以内に、いいね・RT・返信の
    絶対数または速度が一定以上か(柔軟にORで判定)。
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    if hours > INSTANT_MAX_HOURS:
        return False

    likes = post.get("likes", 0)
    rts = post.get("retweets", 0)
    replies = post.get("replies", 0)

    like_ok = likes >= INSTANT_LIKE_THRESHOLD or _velocity(likes, hours) >= INSTANT_LIKE_VELOCITY
    rt_ok = rts >= INSTANT_RT_THRESHOLD or _velocity(rts, hours) >= INSTANT_RT_VELOCITY
    reply_ok = replies >= INSTANT_REPLY_THRESHOLD or _velocity(replies, hours) >= INSTANT_REPLY_VELOCITY

    return like_ok and (rt_ok or reply_ok)


def is_sustained_explosive(post: dict, now=None) -> bool:
    """
    持続バズ判定(ニュース・災害系): 投稿後最大8時間まで見る。
    いいね速度が「時間が経っても大きく減衰していない」ことと、
    インプレッション成長(取得できていれば)・エンゲージメント比率を見る。
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    if hours > SUSTAINED_MAX_HOURS or hours < SUSTAINED_MIN_AGE_HOURS:
        return False

    likes = post.get("likes", 0)
    impressions = post.get("impressions", 0)
    replies = post.get("replies", 0)
    bookmarks = post.get("bookmarks", 0)
    quotes = post.get("quotes", 0)

    # 投稿直後は速い速度を要求し、時間が経つにつれて要求を緩める
    required_velocity = SUSTAINED_LIKE_VELOCITY_BASE / (1.0 + SUSTAINED_DECAY_FACTOR * hours)
    like_velocity = _velocity(likes, hours)

    velocity_ok = like_velocity >= required_velocity
    # impressionsが未取得(0)の場合は、この条件はスキップ扱いにする
    impression_ok = impressions >= SUSTAINED_MIN_IMPRESSIONS if impressions else True
    engagement_ok = (replies / max(likes, 1)) >= 0.03 or bookmarks >= 20 or quotes >= 8

    return velocity_ok and (impression_ok or engagement_ok)


def is_explosive(post: dict, now=None) -> bool:
    """瞬間バズ・持続バズのいずれかを満たせば「爆発」とみなす"""
    return is_instant_explosive(post, now=now) or is_sustained_explosive(post, now=now)


def explosive_type(post: dict, now=None) -> str | None:
    """どちらの種類の爆発として判定されたかを返す(通知文言の出し分け用)"""
    if is_instant_explosive(post, now=now):
        return "instant"
    if is_sustained_explosive(post, now=now):
        return "sustained"
    return None


def is_gekiatsu(post: dict) -> bool:
    """
    「激アツ」判定: 引用比率・ブックマーク比率・返信比率のうち、
    3つ中2つ以上が閾値を超えているか(引用は近似値なのでANDにせず緩めている)
    """
    quote_ratio, bookmark_ratio, reply_ratio = _compute_ratios(post)
    conditions = [
        quote_ratio >= GEKIATSU_QUOTE_LIKE_RATIO,
        bookmark_ratio >= GEKIATSU_BOOKMARK_LIKE_RATIO,
        reply_ratio >= GEKIATSU_REPLY_LIKE_RATIO,
    ]
    return sum(conditions) >= 2


def rank_score(post: dict, now=None) -> float:
    """通知候補の中での優先順位付け用スコア"""
    hours = elapsed_hours(post["posted_at"], now=now)
    likes = post.get("likes", 0)
    rts = post.get("retweets", 0)
    replies = post.get("replies", 0)
    quotes = post.get("quotes", 0)
    bookmarks = post.get("bookmarks", 0)
    impressions = post.get("impressions", 0)

    like_v = _velocity(likes, hours)
    rt_v = _velocity(rts, hours)
    reply_v = _velocity(replies, hours)
    quote_v = _velocity(quotes, hours)
    bookmark_v = _velocity(bookmarks, hours)

    score = (
        WEIGHT_LIKE_VELOCITY * like_v
        + WEIGHT_RT_VELOCITY * rt_v
        + WEIGHT_REPLY_VELOCITY * reply_v
        + WEIGHT_QUOTE_APPROX * quote_v
        + WEIGHT_BOOKMARK_VELOCITY * bookmark_v
    )

    if impressions:
        score += WEIGHT_IMPRESSION_GROWTH * math.log1p(impressions / 10_000)

    return score


def rank_candidates(posts: list[dict], now=None) -> list[dict]:
    """
    爆発判定を通過した投稿群を rank_score 降順で並べ、
    各種フィールド(スコア・比率・激アツ判定・種別)を付与して返す。
    """
    ranked = []
    for post in posts:
        hours = elapsed_hours(post["posted_at"], now=now)
        quote_ratio, bookmark_ratio, reply_ratio = _compute_ratios(post)

        enriched = dict(post)
        enriched["elapsed_hours"] = round(hours, 2)
        enriched["buzz_score"] = round(rank_score(post, now=now), 1)
        enriched["quote_like_ratio"] = round(quote_ratio, 3)
        enriched["bookmark_like_ratio"] = round(bookmark_ratio, 3)
        enriched["reply_like_ratio"] = round(reply_ratio, 3)
        enriched["is_gekiatsu"] = is_gekiatsu(post)
        enriched["explosive_type"] = explosive_type(post, now=now)
        ranked.append(enriched)

    ranked.sort(key=lambda p: p["buzz_score"], reverse=True)
    return ranked


def filter_explosive(posts: list[dict], now=None) -> list[dict]:
    """収集した投稿群から、爆発条件(瞬間 or 持続)を満たすものだけ抽出してランキング付きで返す"""
    candidates = [p for p in posts if is_explosive(p, now=now)]
    return rank_candidates(candidates, now=now)


def explosive_progress(post: dict, now=None) -> float:
    """
    「爆発条件にどれだけ近づいているか」を0.0〜1.0+の値で返す。
    瞬間バズ側の進捗と持続バズ側の進捗をそれぞれ計算し、高い方を採用する。
    「動作確認」機能で、まだ通知条件は満たしていないが最も近い投稿を
    見せるために使う(早期発見の目安。ただし通知トリガーにはしない)。
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    likes = post.get("likes", 0)
    rts = post.get("retweets", 0)
    replies = post.get("replies", 0)
    impressions = post.get("impressions", 0)

    # 瞬間側の進捗(0〜1.5でクリップ)
    instant_like_p = min(likes / INSTANT_LIKE_THRESHOLD, 1.5) if INSTANT_LIKE_THRESHOLD else 0
    instant_rt_p = min(rts / INSTANT_RT_THRESHOLD, 1.5) if INSTANT_RT_THRESHOLD else 0
    instant_reply_p = min(replies / INSTANT_REPLY_THRESHOLD, 1.5) if INSTANT_REPLY_THRESHOLD else 0
    instant_score = instant_like_p * 0.5 + instant_rt_p * 0.3 + instant_reply_p * 0.2

    # 持続側の進捗(速度ベース)
    required_v = SUSTAINED_LIKE_VELOCITY_BASE / (1.0 + SUSTAINED_DECAY_FACTOR * hours)
    actual_v = _velocity(likes, hours)
    sustained_score = min(actual_v / required_v, 1.8) if required_v else 0

    if impressions:
        imp_score = min(impressions / SUSTAINED_MIN_IMPRESSIONS, 1.5)
        sustained_score = sustained_score * 0.7 + imp_score * 0.3

    return round(max(instant_score, sustained_score), 3)


def rank_by_progress(posts: list[dict], now=None, limit: int = 5) -> list[dict]:
    """
    「爆発条件への近さ」順に投稿を並べ、上位limit件を返す。
    各投稿には progress(0.0〜1.0+)フィールドを付与する。
    早期発見・動作確認の目的で使う(通知はしない、あくまで参考情報)。
    """
    enriched = []
    for post in posts:
        p = dict(post)
        p["progress"] = explosive_progress(post, now=now)
        enriched.append(p)

    enriched.sort(key=lambda p: p["progress"], reverse=True)
    return enriched[:limit]
