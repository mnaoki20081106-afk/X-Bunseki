"""detector.py
目的: 数百万imp級になりうる投稿を、目安4時間以内（可能なら数十分）で拾う。
誤検知は false_positive.py で典型ノイズだけ切る。
"""

import math
import os
from datetime import datetime, timedelta, timezone

import growth
import early_signal
import false_positive


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[detector] 環境変数 {name}={raw!r} は数値として解釈できません。既定値{default}を使います")
        return default


MAX_AGE_MINUTES = _envf("MAX_AGE_MINUTES", 240)
MIN_LIKES_FLOOR = _envf("MIN_LIKES_FLOOR", 100)
FIRST_SIGHT_MIN_LIKES_PER_MIN = _envf("FIRST_SIGHT_MIN_LIKES_PER_MIN", 10)
NOTIFY_SCORE = _envf("NOTIFY_SCORE", 55)
GEKIATSU_SCORE = _envf("GEKIATSU_SCORE", 78)

POINTS_GROWTH = _envf("POINTS_GROWTH", 36)
POINTS_ACCEL = _envf("POINTS_ACCEL", 18)
POINTS_DISCUSSION = _envf("POINTS_DISCUSSION", 22)
POINTS_SAVE = _envf("POINTS_SAVE", 14)
POINTS_SPREAD = _envf("POINTS_SPREAD", 10)
POINTS_QUOTE = _envf("POINTS_QUOTE", 8)

RELEVANCE_FLOOR = _envf("RELEVANCE_FLOOR", 0.90)
GROWTH_FULL_LIKES_PER_MIN = _envf("GROWTH_FULL_LIKES_PER_MIN", 80)
ACCEL_FULL = _envf("ACCEL_FULL", 1.8)
DISCUSSION_FULL_RATIO = _envf("DISCUSSION_FULL_RATIO", 0.08)
SAVE_FULL_RATIO = _envf("SAVE_FULL_RATIO", 0.12)
SPREAD_FULL_RATIO = _envf("SPREAD_FULL_RATIO", 0.20)
QUOTE_FULL_RATIO = _envf("QUOTE_FULL_RATIO", 0.05)

UNMEASURED_CONFIDENCE = _envf("UNMEASURED_CONFIDENCE", 0.78)
FRESHNESS_FULL_MINUTES = _envf("FRESHNESS_FULL_MINUTES", 90)
FRESHNESS_MIN_MULTIPLIER = _envf("FRESHNESS_MIN_MULTIPLIER", 0.55)

NOTIFY_MIN_INTERVAL_MINUTES = _envf("NOTIFY_MIN_INTERVAL_MINUTES", 45)
NOTIFY_INTERVAL_OVERRIDE_SCORE = _envf("NOTIFY_INTERVAL_OVERRIDE_SCORE", 78)
NOTIFY_HARD_MIN_INTERVAL_MINUTES = _envf("NOTIFY_HARD_MIN_INTERVAL_MINUTES", 15)
NOTIFY_MAX_PER_RUN = int(_envf("NOTIFY_MAX_PER_RUN", 1))
NOTIFY_MAX_PER_DAY = int(_envf("NOTIFY_MAX_PER_DAY", 6))
ACCOUNT_COOLDOWN_HOURS = _envf("ACCOUNT_COOLDOWN_HOURS", 3.0)
TOPIC_COOLDOWN_HOURS = _envf("TOPIC_COOLDOWN_HOURS", 8.0)


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def elapsed_hours(posted_at, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    return max((now - _parse_dt(posted_at)).total_seconds() / 3600.0, 0.01)


def is_in_cooldown(author_handle: str, last_notified_at, now=None) -> bool:
    if not last_notified_at:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - _parse_dt(last_notified_at)) < timedelta(hours=ACCOUNT_COOLDOWN_HOURS)


def hard_filter_reason(post: dict, g: dict) -> str | None:
    if g["age_minutes"] > MAX_AGE_MINUTES:
        return f"古すぎる({g['age_minutes']:.0f}分 > {MAX_AGE_MINUTES:.0f}分)"
    if (post.get("likes") or 0) < MIN_LIKES_FLOOR:
        return f"いいねが少なすぎる({post.get('likes', 0)} < {MIN_LIKES_FLOOR:.0f})"
    if not g["is_measured"] and g["likes_per_min"] < FIRST_SIGHT_MIN_LIKES_PER_MIN:
        return (
            f"初回観測かつ平均速度が低い"
            f"({g['likes_per_min']:.1f} < {FIRST_SIGHT_MIN_LIKES_PER_MIN:.1f}/分)"
        )
    # 誤検知ガード（失速・いいね稼ぎ・誘導文）
    fp = false_positive.reason(post, g)
    if fp:
        return fp
    return None


def _ratio_points(numerator: int, denominator: int, full_ratio: float, max_points: float) -> float:
    if denominator <= 0 or full_ratio <= 0:
        return 0.0
    ratio = numerator / denominator
    return max_points * min(ratio / full_ratio, 1.0)


def score(post: dict, g: dict, relevance: float = 0.0) -> dict:
    likes = post.get("likes") or 0
    breakdown = {}

    lpm = g["likes_per_min"]
    growth_ratio = math.log1p(max(lpm, 0)) / math.log1p(GROWTH_FULL_LIKES_PER_MIN)
    growth_points = POINTS_GROWTH * min(growth_ratio, 1.0)
    if not g["is_measured"]:
        growth_points *= UNMEASURED_CONFIDENCE
    breakdown["伸び率"] = round(growth_points, 1)

    accel = g.get("acceleration")
    if accel is None:
        accel_points = POINTS_ACCEL * 0.5
    else:
        accel_points = POINTS_ACCEL * max(min(accel / ACCEL_FULL, 1.0), 0.0)
    breakdown["加速度"] = round(accel_points, 1)

    discussion_points = _ratio_points(
        post.get("replies") or 0, likes, DISCUSSION_FULL_RATIO, POINTS_DISCUSSION
    )
    breakdown["議論量"] = round(discussion_points, 1)

    save_points = _ratio_points(
        post.get("bookmarks") or 0, likes, SAVE_FULL_RATIO, POINTS_SAVE
    )
    breakdown["保存率"] = round(save_points, 1)

    spread_points = _ratio_points(
        post.get("retweets") or 0, likes, SPREAD_FULL_RATIO, POINTS_SPREAD
    )
    breakdown["拡散率"] = round(spread_points, 1)

    quote_points = _ratio_points(
        post.get("quotes") or 0, likes, QUOTE_FULL_RATIO, POINTS_QUOTE
    )
    if quote_points > 0:
        breakdown["引用"] = round(quote_points, 1)

    raw_total = sum(breakdown.values())

    early = early_signal.evaluate_early_burst(post, g)
    if early.get("hit"):
        breakdown["初動"] = round(early["bonus"], 1)
        raw_total += early["bonus"]

    relevance = max(min(relevance, 1.0), 0.0)
    relevance_multiplier = RELEVANCE_FLOOR + (1.0 - RELEVANCE_FLOOR) * relevance

    age = g["age_minutes"]
    if age <= FRESHNESS_FULL_MINUTES:
        freshness = 1.0
    else:
        over = (age - FRESHNESS_FULL_MINUTES) / max(MAX_AGE_MINUTES - FRESHNESS_FULL_MINUTES, 1)
        freshness = max(1.0 - over * (1.0 - FRESHNESS_MIN_MULTIPLIER), FRESHNESS_MIN_MULTIPLIER)

    total = min(raw_total * freshness * relevance_multiplier, 100.0)

    return {
        "buzz_score": round(total, 1),
        "score_breakdown": breakdown,
        "freshness_multiplier": round(freshness, 2),
        "relevance_multiplier": round(relevance_multiplier, 2),
        "relevance": round(relevance, 2),
        "is_gekiatsu": total >= GEKIATSU_SCORE,
        "early_burst": early.get("hit", False),
    }


def _million_velocity_floor(age: float) -> float:
    """Grok prior, linearly interpolated to avoid a pile of hard boundaries."""
    anchors = [(30, 2500), (60, 1600), (90, 1100), (120, 750), (180, 450), (240, 300)]
    if age <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= age <= x1:
            t = (age - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return anchors[-1][1]


def million_imp_rescue(post: dict, g: dict) -> tuple[bool, str]:
    """Rescue proven million-view posts even when keyword/like scoring rejects them."""
    age = float(g.get("age_minutes") or 9999)
    imp = int(post.get("impressions") or 0)
    if age > 240 or imp < 1_000_000:
        return False, ""

    velocity = float(g.get("impressions_per_min") or 0)
    accel = g.get("impressions_acceleration")
    floor = _million_velocity_floor(age)
    # A post already at 3M/5M has proven distribution, so allow slower cruising.
    if imp >= 5_000_000:
        floor *= 0.60
    elif imp >= 3_000_000:
        floor *= 0.75

    accel_factor = 1.0 if accel is None else max(0.5, min(2.0, float(accel))) ** 0.7
    effective = velocity * accel_factor

    # Do not reject a huge absolute velocity for mild deceleration. Only call it
    # a stall when both acceleration and absolute speed are weak.
    clear_stall = accel is not None and float(accel) <= 0.65 and velocity < floor * 1.5
    if clear_stall:
        return False, f"100万imp到達済みだが失速(accel={accel}, {velocity:.0f}imp/分)"

    hit = effective >= floor
    detail = f"{imp:,}imp / {velocity:.0f}imp/分 / effective={effective:.0f} >= {floor:.0f}"
    return hit, detail


def evaluate(post: dict, history: list[dict], relevance: float = 0.0, now=None) -> dict:
    g = growth.compute(post, history, now=now)
    enriched = dict(post)
    enriched["growth"] = g
    enriched["elapsed_minutes"] = g["age_minutes"]
    enriched["elapsed_hours"] = round(g["age_minutes"] / 60.0, 2)
    # Forecast is useful even when the ordinary like/keyword filters reject a post.
    enriched.update(growth.predict_final_impressions(post, g))
    rescue, rescue_detail = million_imp_rescue(post, g)
    enriched["million_imp_bypass"] = rescue
    enriched["million_imp_bypass_detail"] = rescue_detail
    reason = hard_filter_reason(post, g)
    if reason and not rescue:
        enriched["rejected_reason"] = reason
        enriched["buzz_score"] = 0.0
        enriched["score_breakdown"] = {}
        enriched["is_gekiatsu"] = False
        enriched["should_notify"] = False
        enriched["early_burst"] = False
        return enriched
    enriched["rejected_reason"] = None
    enriched.update(score(post, g, relevance=relevance))
    # Rescue means "show/track this post"; it does not inflate the normal score.
    enriched["should_notify"] = enriched["buzz_score"] >= NOTIFY_SCORE or rescue
    return enriched


def evaluate_all(posts: list[dict], history_map: dict, relevance_fn=None, now=None) -> list[dict]:
    results = []
    for post in posts:
        relevance = relevance_fn(post) if relevance_fn else 0.0
        results.append(
            evaluate(post, history_map.get(post["post_id"], []), relevance=relevance, now=now)
        )
    results.sort(key=lambda p: (p.get("predicted_final_impressions") or 0, p["buzz_score"]), reverse=True)
    return results


def can_notify_now(score: float, minutes_since_last: float | None) -> tuple[bool, str]:
    if minutes_since_last is None:
        return True, ""
    if score >= NOTIFY_INTERVAL_OVERRIDE_SCORE:
        if minutes_since_last >= NOTIFY_HARD_MIN_INTERVAL_MINUTES:
            return True, ""
        return False, (
            f"前回の通知から{minutes_since_last:.0f}分"
            f"(大ネタでも最短{NOTIFY_HARD_MIN_INTERVAL_MINUTES:.0f}分は空ける)"
        )
    if minutes_since_last >= NOTIFY_MIN_INTERVAL_MINUTES:
        return True, ""
    return False, (
        f"前回の通知から{minutes_since_last:.0f}分"
        f"(最短間隔{NOTIFY_MIN_INTERVAL_MINUTES:.0f}分 / "
        f"{NOTIFY_INTERVAL_OVERRIDE_SCORE:.0f}点以上なら短縮可)"
    )


def config_summary() -> dict:
    return {
        "NOTIFY_SCORE": NOTIFY_SCORE,
        "MIN_LIKES_FLOOR": MIN_LIKES_FLOOR,
        "MAX_AGE_MINUTES": MAX_AGE_MINUTES,
        "FIRST_SIGHT_MIN_LIKES_PER_MIN": FIRST_SIGHT_MIN_LIKES_PER_MIN,
        "NOTIFY_MAX_PER_RUN": NOTIFY_MAX_PER_RUN,
        "NOTIFY_MAX_PER_DAY": NOTIFY_MAX_PER_DAY,
        "NOTIFY_MIN_INTERVAL_MINUTES": NOTIFY_MIN_INTERVAL_MINUTES,
        "RELEVANCE_FLOOR": RELEVANCE_FLOOR,
        "EARLY_LPM_AT_30": early_signal.EARLY_LPM_AT_30,
        "EARLY_BONUS_AT_30": early_signal.EARLY_BONUS_AT_30,
        "EARLY_LPM_AT_15": early_signal.EARLY_LPM_AT_15,
        "EARLY_BURST_BONUS": early_signal.EARLY_BURST_BONUS,
    }
