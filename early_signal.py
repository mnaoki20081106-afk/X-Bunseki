"""
early_signal.py
目安は投稿後4時間。その中で「数十分以内」を最優先に拾う。
目標: 数百万imp級になる一次ネタの初速を逃さない。
"""
from __future__ import annotations

import os


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# 監視の目安 = 4時間
EARLY_WINDOW_MINUTES = _envf("EARLY_WINDOW_MINUTES", 240)

# 分速の段階（数十分以内を厳しめ＝本物の初速だけ通す）
EARLY_LPM_AT_30 = _envf("EARLY_LPM_AT_30", 18)   # 〜30分: 数十分以内ゾーン
EARLY_LPM_AT_60 = _envf("EARLY_LPM_AT_60", 12)
EARLY_LPM_AT_120 = _envf("EARLY_LPM_AT_120", 8)
EARLY_LPM_AT_240 = _envf("EARLY_LPM_AT_240", 5)

# 議論・拡散（いいね稼ぎ単体を弾く）
EARLY_MIN_REPLY_RATIO = _envf("EARLY_MIN_REPLY_RATIO", 0.06)
EARLY_MIN_RT_RATIO = _envf("EARLY_MIN_RT_RATIO", 0.10)
EARLY_MIN_QUOTE_RATIO = _envf("EARLY_MIN_QUOTE_RATIO", 0.03)

# ボーナス: 若いほど厚い
EARLY_BONUS_AT_30 = _envf("EARLY_BONUS_AT_30", 18)
EARLY_BONUS_AT_60 = _envf("EARLY_BONUS_AT_60", 14)
EARLY_BONUS_AT_120 = _envf("EARLY_BONUS_AT_120", 10)
EARLY_BONUS_AT_240 = _envf("EARLY_BONUS_AT_240", 6)

EARLY_MIN_LIKES = _envf("EARLY_MIN_LIKES", 80)

# 互換（config_summary / Variables）
EARLY_LPM_AT_15 = EARLY_LPM_AT_30
EARLY_BURST_BONUS = EARLY_BONUS_AT_30


def _need_lpm(age: float) -> float:
    if age <= 30:
        return EARLY_LPM_AT_30
    if age <= 60:
        return EARLY_LPM_AT_60
    if age <= 120:
        return EARLY_LPM_AT_120
    return EARLY_LPM_AT_240


def _bonus(age: float) -> float:
    if age <= 30:
        return EARLY_BONUS_AT_30
    if age <= 60:
        return EARLY_BONUS_AT_60
    if age <= 120:
        return EARLY_BONUS_AT_120
    return EARLY_BONUS_AT_240


def evaluate_early_burst(post: dict, g: dict) -> dict:
    age = g.get("age_minutes") or 0
    if age <= 0 or age > EARLY_WINDOW_MINUTES:
        return {"hit": False, "bonus": 0.0, "reason": ""}

    lpm = g.get("likes_per_min") or 0
    likes = post.get("likes") or 0
    replies = post.get("replies") or 0
    rts = post.get("retweets") or 0
    quotes = post.get("quotes") or 0

    need_lpm = _need_lpm(age)
    reply_ratio = (replies / likes) if likes > 0 else 0.0
    rt_ratio = (rts / likes) if likes > 0 else 0.0
    quote_ratio = (quotes / likes) if likes > 0 else 0.0

    lpm_ok = lpm >= need_lpm
    discussion_ok = (
        reply_ratio >= EARLY_MIN_REPLY_RATIO
        or rt_ratio >= EARLY_MIN_RT_RATIO
        or quote_ratio >= EARLY_MIN_QUOTE_RATIO
    )
    hit = bool(lpm_ok and discussion_ok and likes >= EARLY_MIN_LIKES)
    bonus = float(_bonus(age)) if hit else 0.0

    reason = ""
    if hit:
        reason = (
            f"初動 age={age:.0f}m lpm={lpm:.1f}>={need_lpm} "
            f"replyR={reply_ratio:.2f} rtR={rt_ratio:.2f} qR={quote_ratio:.2f} +{bonus:.0f}"
        )
    return {
        "hit": hit,
        "bonus": bonus,
        "reason": reason,
        "need_lpm": need_lpm,
        "reply_ratio": round(reply_ratio, 3),
        "rt_ratio": round(rt_ratio, 3),
        "quote_ratio": round(quote_ratio, 3),
    }
