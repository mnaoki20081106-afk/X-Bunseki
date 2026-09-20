"""
early_signal.py
4時間以内の「クラスタを越えて爆発しうる一次ネタ」の初動ハードルール。
detector.score からボーナス加点で使う。
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


EARLY_WINDOW_MINUTES = _envf("EARLY_WINDOW_MINUTES", 240)
EARLY_LPM_AT_15 = _envf("EARLY_LPM_AT_15", 25)
EARLY_LPM_AT_60 = _envf("EARLY_LPM_AT_60", 15)
EARLY_LPM_AT_180 = _envf("EARLY_LPM_AT_180", 8)
EARLY_MIN_REPLY_RATIO = _envf("EARLY_MIN_REPLY_RATIO", 0.08)
EARLY_MIN_RT_RATIO = _envf("EARLY_MIN_RT_RATIO", 0.12)
EARLY_BURST_BONUS = _envf("EARLY_BURST_BONUS", 12)
MIN_LIKES_FLOOR = _envf("MIN_LIKES_FLOOR", 150)


def evaluate_early_burst(post: dict, g: dict) -> dict:
    age = g.get("age_minutes") or 0
    if age <= 0 or age > EARLY_WINDOW_MINUTES:
        return {"hit": False, "bonus": 0.0, "reason": ""}

    lpm = g.get("likes_per_min") or 0
    likes = post.get("likes") or 0
    replies = post.get("replies") or 0
    rts = post.get("retweets") or 0

    if age <= 15:
        need_lpm = EARLY_LPM_AT_15
    elif age <= 60:
        need_lpm = EARLY_LPM_AT_60
    else:
        need_lpm = EARLY_LPM_AT_180

    reply_ratio = (replies / likes) if likes > 0 else 0.0
    rt_ratio = (rts / likes) if likes > 0 else 0.0
    lpm_ok = lpm >= need_lpm
    discussion_ok = reply_ratio >= EARLY_MIN_REPLY_RATIO or rt_ratio >= EARLY_MIN_RT_RATIO
    hit = bool(lpm_ok and discussion_ok and likes >= MIN_LIKES_FLOOR)

    reason = ""
    if hit:
        reason = (
            f"初動爆発 age={age:.0f}m lpm={lpm:.1f}>={need_lpm} "
            f"replyR={reply_ratio:.2f} rtR={rt_ratio:.2f}"
        )
    return {
        "hit": hit,
        "bonus": float(EARLY_BURST_BONUS) if hit else 0.0,
        "reason": reason,
        "need_lpm": need_lpm,
        "reply_ratio": round(reply_ratio, 3),
        "rt_ratio": round(rt_ratio, 3),
    }
