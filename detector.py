"""
detector.py (v3.3)
「瞬間バズ」+「持続バズ(ニュース・災害系)」の判定ロジック。
「1日0〜1件」レベルの厳しさを目標に、Grokとの複数回のレビューを経て
再設計した。

これまでの変遷:
  v2  : 投稿後1時間固定、引用・ブックマーク・いいねの3条件必須
  v3  : 瞬間/持続の2系統に分離。引用を補助指標に格下げ → 緩すぎて通知過多
  v3.1: 閾値引き上げ、激アツを厳格ANDに戻す → まだ緩い
  v3.2: 速度換算の抜け道(短時間の伸びを1時間換算で水増し)を発見・修正
        → さらに「インプレッション未取得を自動合格扱いにする」抜け道も発見・修正
  v3.3: Grok第3提案を反映。時間窓を短縮、インプレッションを完全必須化
        (未取得は無条件で不合格)、アカウント単位クールダウンを追加

★重要な設計判断の記録:
  - 引用数(quotes)は必須条件にしない。Xが公開しておらず、実際には
    「View quotes一覧を数件スクロールして数えた近似値(下限値)」でしか
    ないため。あくまで補助条件・激アツ判定・スコアリングにのみ使う。
  - 持続バズは「インプレッションが取得できていなければ無条件で不合格」。
    以前は代替判定で通してしまい、抜け道になっていた。
  - 早期警告(閾値未達でも通知)は採用しない。ノイズになるため。
    進捗情報自体は「動作確認」機能(rank_by_progress)で見れる。
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# ── 時間窓(v3.3で短縮) ──────────────────────────────
INSTANT_MAX_HOURS = 1.5      # 瞬間バズの対象窓(2.0→1.5)
SUSTAINED_MAX_HOURS = 5.0    # 持続バズの対象窓(6.0→5.0)

# ── 瞬間バズ用の閾値(v3.3) ──
INSTANT_LIKE_THRESHOLD = 2500
INSTANT_LIKE_VELOCITY = 1200    # いいね/時間
INSTANT_RT_THRESHOLD = 200
INSTANT_RT_VELOCITY = 80        # リポスト/時間
INSTANT_REPLY_THRESHOLD = 100
INSTANT_REPLY_VELOCITY = 40     # 返信/時間

# 速度換算による早期判定は、投稿からこの時間が経つまで使わせない。
# 理由: 投稿直後30〜40分の伸びを1時間換算すると異常に高い速度になり、
# 速度閾値をいくら上げても実質意味が無くなってしまうため(v3.2で発見)。
MIN_AGE_FOR_VELOCITY_HOURS = 1.0

# ── 持続バズ用(v3.3: 全条件AND必須、インプレッション必須) ──
SUSTAINED_LIKE_VELOCITY_BASE = 600   # 投稿直後の基準速度(いいね/時間)
SUSTAINED_DECAY_FACTOR = 0.2         # 時間経過による要求速度の緩和係数
                                       # (1h→500, 3h→375, 5h→300 相当)
SUSTAINED_MIN_IMPRESSIONS = 300_000  # 最低インプレッション。未取得は無条件不合格
SUSTAINED_MIN_AGE_HOURS = 0.3        # あまりに新しい投稿は持続バズ判定の対象外
SUSTAINED_MIN_REPLY_LIKE_RATIO = 0.06   # 返信÷いいね 6%以上(必須)
SUSTAINED_MIN_BOOKMARKS = 30             # ブックマーク30以上
SUSTAINED_MIN_QUOTES = 15                # または引用15以上(近似値。どちらか片方でOK)

# ── 激アツ判定(3指標すべて必須) ──
GEKIATSU_QUOTE_LIKE_RATIO = 0.06     # 引用÷いいね 6%以上
GEKIATSU_BOOKMARK_LIKE_RATIO = 0.12  # ブックマーク÷いいね 12%以上
GEKIATSU_REPLY_LIKE_RATIO = 0.07     # 返信÷いいね 7%以上

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

# 早期警告(通知トリガーとしては使わない。参考値として残すのみ)
EARLY_WARNING_PROGRESS = 0.90

# アカウント単位のクールダウン(同じ投稿者から立て続けに通知しない)
ACCOUNT_COOLDOWN_HOURS = 2.0
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
    瞬間バズ判定: 投稿後1.5時間以内に、いいね・RT・返信の
    絶対数を全て満たすか(v3.3で速度換算を完全撤廃)。

    ★経緯: 当初は「絶対数 または 速度換算」のORロジックだったが、
    速度換算(count÷経過時間)は短い時間で伸びた投稿を1時間換算で
    過大評価してしまい、速度側の閾値をいくら上げても実質意味が
    無くなる問題が繰り返し発生した。1.5時間という短い判定窓なら、
    絶対数だけで十分に「速さ」を表現できるため、速度換算を廃止して
    単純化した。
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    if hours > INSTANT_MAX_HOURS:
        return False

    likes = post.get("likes", 0)
    rts = post.get("retweets", 0)
    replies = post.get("replies", 0)

    return (
        likes >= INSTANT_LIKE_THRESHOLD
        and rts >= INSTANT_RT_THRESHOLD
        and replies >= INSTANT_REPLY_THRESHOLD
    )


def is_sustained_explosive(post: dict, now=None) -> bool:
    """
    持続バズ判定(ニュース・災害系): 投稿後最大5時間まで見る。
    以下を「全て」満たした場合のみ合格(v3.3でAND必須に統一):
      - いいね速度が、経過時間に応じた要求速度以上
      - インプレッションが30万以上(★未取得の場合は無条件で不合格。
        以前はここが代替判定で通ってしまう抜け道だった)
      - 返信÷いいね比率が6%以上
      - ブックマーク30以上、または引用15以上(近似値なのでどちらかでOK)
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    if hours > SUSTAINED_MAX_HOURS or hours < SUSTAINED_MIN_AGE_HOURS:
        return False
    if hours < MIN_AGE_FOR_VELOCITY_HOURS:
        return False  # 持続バズは元々「時間をかけて伸びる」ものなので早すぎる判定はしない

    likes = post.get("likes", 0)
    impressions = post.get("impressions", 0)
    replies = post.get("replies", 0)
    bookmarks = post.get("bookmarks", 0)
    quotes = post.get("quotes", 0)

    required_velocity = SUSTAINED_LIKE_VELOCITY_BASE / (1.0 + SUSTAINED_DECAY_FACTOR * hours)
    like_velocity = _velocity(likes, hours)
    velocity_ok = like_velocity >= required_velocity

    # インプレッション未取得は無条件で不合格(代替判定は無し)
    if not impressions:
        return False
    impression_ok = impressions >= SUSTAINED_MIN_IMPRESSIONS

    reply_ratio_ok = (replies / max(likes, 1)) >= SUSTAINED_MIN_REPLY_LIKE_RATIO
    bookmark_or_quote_ok = bookmarks >= SUSTAINED_MIN_BOOKMARKS or quotes >= SUSTAINED_MIN_QUOTES

    return velocity_ok and impression_ok and reply_ratio_ok and bookmark_or_quote_ok


def is_explosive(post: dict, now=None) -> bool:
    """瞬間バズ・持続バズのいずれかを満たせば「爆発」とみなす"""
    return is_instant_explosive(post, now=now) or is_sustained_explosive(post, now=now)


def explosive_type(post: dict, now=None) -> str | None:
    """どちらの種類の爆発として判定されたかを返す(優先順位: 瞬間 → 持続)"""
    if is_instant_explosive(post, now=now):
        return "instant"
    if is_sustained_explosive(post, now=now):
        return "sustained"
    return None


def is_gekiatsu(post: dict) -> bool:
    """「激アツ」判定: 引用比率・ブックマーク比率・返信比率の3つすべてを満たす厳格AND"""
    quote_ratio, bookmark_ratio, reply_ratio = _compute_ratios(post)
    return (
        quote_ratio >= GEKIATSU_QUOTE_LIKE_RATIO
        and bookmark_ratio >= GEKIATSU_BOOKMARK_LIKE_RATIO
        and reply_ratio >= GEKIATSU_REPLY_LIKE_RATIO
    )


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
    「動作確認」機能で参考情報として見せるためのもの(通知トリガーにはしない)。

    ★v3.3で修正した不具合: 以前は「いいね達成率×0.5 + RT達成率×0.3 +
    返信達成率×0.2」のような加重平均で計算していたが、実際の判定
    (is_instant_explosive等)は「全項目を同時に満たす」AND条件のため、
    いいねだけ突出していても他の項目が基準未達なら加重平均だけでは
    100%を超えてしまい、「進捗100%超なのに通知が来ない」という
    ズレが生じていた。
    修正: 各項目の達成率のうち「最も低いもの(ボトルネック)」を
    採用する方式に変更。これにより、進捗が100%に達した時点で、
    必ず実際の判定ロジックも合格する(整合性が保証される)。
    """
    hours = elapsed_hours(post["posted_at"], now=now)
    likes = post.get("likes", 0)
    rts = post.get("retweets", 0)
    replies = post.get("replies", 0)
    impressions = post.get("impressions", 0)
    bookmarks = post.get("bookmarks", 0)
    quotes = post.get("quotes", 0)

    # 瞬間バズ側の進捗: 3項目のうち最も低い達成率(ボトルネック)を採用。
    # 時間窓を超えていたら0にする。
    if hours <= INSTANT_MAX_HOURS:
        instant_like_p = likes / INSTANT_LIKE_THRESHOLD if INSTANT_LIKE_THRESHOLD else 0
        instant_rt_p = rts / INSTANT_RT_THRESHOLD if INSTANT_RT_THRESHOLD else 0
        instant_reply_p = replies / INSTANT_REPLY_THRESHOLD if INSTANT_REPLY_THRESHOLD else 0
        instant_score = min(instant_like_p, instant_rt_p, instant_reply_p)
    else:
        instant_score = 0.0

    # 持続バズ側の進捗: 4条件(速度・インプレッション・返信比率・
    # ブックマークor引用)のうち最も低い達成率を採用。
    # ブックマークと引用は「どちらか片方でOK」なので、2つのうち高い方を使う。
    # インプレッション未取得の場合は0(=必須条件を満たしていない)にする。
    if SUSTAINED_MIN_AGE_HOURS <= hours <= SUSTAINED_MAX_HOURS and hours >= MIN_AGE_FOR_VELOCITY_HOURS:
        required_v = SUSTAINED_LIKE_VELOCITY_BASE / (1.0 + SUSTAINED_DECAY_FACTOR * hours)
        actual_v = _velocity(likes, hours)
        velocity_p = actual_v / required_v if required_v else 0

        impression_p = impressions / SUSTAINED_MIN_IMPRESSIONS if impressions else 0.0

        reply_ratio = replies / max(likes, 1)
        reply_ratio_p = reply_ratio / SUSTAINED_MIN_REPLY_LIKE_RATIO if SUSTAINED_MIN_REPLY_LIKE_RATIO else 0

        bookmark_p = bookmarks / SUSTAINED_MIN_BOOKMARKS if SUSTAINED_MIN_BOOKMARKS else 0
        quote_p = quotes / SUSTAINED_MIN_QUOTES if SUSTAINED_MIN_QUOTES else 0
        bookmark_or_quote_p = max(bookmark_p, quote_p)

        sustained_score = min(velocity_p, impression_p, reply_ratio_p, bookmark_or_quote_p)
    else:
        sustained_score = 0.0

    return round(max(instant_score, sustained_score), 3)


def rank_by_progress(posts: list[dict], now=None, limit: int = 5) -> list[dict]:
    """
    「爆発条件への近さ」順に投稿を並べ、上位limit件を返す。
    早期発見・動作確認の目的で使う(通知はしない、あくまで参考情報)。
    """
    enriched = []
    for post in posts:
        p = dict(post)
        p["progress"] = explosive_progress(post, now=now)
        enriched.append(p)

    enriched.sort(key=lambda p: p["progress"], reverse=True)
    return enriched[:limit]


def is_in_cooldown(author_handle: str, last_notified_at, now=None) -> bool:
    """
    アカウント単位のクールダウン判定。同じ投稿者から
    ACCOUNT_COOLDOWN_HOURS以内に既に通知済みなら True(=今回は見送る)。
    last_notified_at は None(未通知)またはISO8601文字列。
    """
    if not last_notified_at:
        return False
    now = now or datetime.now(timezone.utc)
    last_dt = _parse_dt(last_notified_at)
    return (now - last_dt) < timedelta(hours=ACCOUNT_COOLDOWN_HOURS)
