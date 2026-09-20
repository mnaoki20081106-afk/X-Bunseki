"""
early_signal.py
4時間以内の初動ハードルール（X For You 拡散シグナル寄り）。

Xが強く見るもの:
  - 返信速度・議論密度（会話が伸びると配信が広がる）
  - 引用（クラスタ横断）
  - RT率（ニュース性）
  - いいね絶対数より「分速」
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
# 分速: 15分で20/分=300いいね相当 → 一次拡散の入口として妥当
EARLY_LPM_AT_15 = _envf("EARLY_LPM_AT_15", 20)
EARLY_LPM_AT_60 = _envf("EARLY_LPM_AT_60", 12)
EARLY_LPM_AT_180 = _envf("EARLY_LPM_AT_180", 6)
# 議論 or 拡散のどれかが立っていればOK（いいね稼ぎ単体を弾く）
EARLY_MIN_REPLY_RATIO = _envf("EARLY_MIN_REPLY_RATIO", 0.06)
EARLY_MIN_RT_RATIO = _envf("EARLY_MIN_RT_RATIO", 0.10)
EARLY_MIN_QUOTE_RATIO = _envf("EARLY_MIN_QUOTE_RATIO", 0.03)
EARLY_BURST_BONUS = _envf("EARLY_BURST_BONUS", 15)
# 初動だけは足切りを少し緩める（速度側で担保）
EARLY_MIN_LIKES = _envf("EARLY_MIN_LIKES", 80)


def evaluate_early_burst(post: dict, g: dict) -> dict:
    age = g.get("age_minutes") or 0
    if age <= 0 or age > EARLY_WINDOW_MINUTES:
        return {"hit": False, "bonus": 0.0, "reason": ""}

    lpm = g.get("likes_per_min") or 0
    likes = post.get("likes") or 0
    replies = post.get("replies") or 0
    rts = post.get("retweets") or 0
    quotes = post.get("quotes") or 0

    if age <= 15:
        need_lpm = EARLY_LPM_AT_15
    elif age <= 60:
        need_lpm = EARLY_LPM_AT_60
    else:
        need_lpm = EARLY_LPM_AT_180

    reply_ratio = (replies / likes) if likes > 0 else 0.0
    rt_ratio = (rts / likes) if likes > 0 else 0.0
    quote_ratio = (quotes / likes) if likes > 0 else 0.0

    lpm_ok = lpm >= need_lpm
    # 返信・RT・引用のいずれか = クラスタ外へ出やすい形
    discussion_ok = (
        reply_ratio >= EARLY_MIN_REPLY_RATIO
        or rt_ratio >= EARLY_MIN_RT_RATIO
        or quote_ratio >= EARLY_MIN_QUOTE_RATIO
    )
    hit = bool(lpm_ok and discussion_ok and likes >= EARLY_MIN_LIKES)

    reason = ""
    if hit:
        reason = (
            f"初動爆発 age={age:.0f}m lpm={lpm:.1f}>={need_lpm} "
            f"replyR={reply_ratio:.2f} rtR={rt_ratio:.2f} qR={quote_ratio:.2f}"
        )
    return {
        "hit": hit,
        "bonus": float(EARLY_BURST_BONUS) if hit else 0.0,
        "reason": reason,
        "need_lpm": need_lpm,
        "reply_ratio": round(reply_ratio, 3),
        "rt_ratio": round(rt_ratio, 3),
        "quote_ratio": round(quote_ratio, 3),
    }
