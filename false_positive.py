"""
false_positive.py
誤検知を減らすためのネガティブ判定。
数百万imp級の一次ネタを落とさない範囲で、典型ノイズだけ切る。
"""
from __future__ import annotations

import os
import re


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# 実測で失速しているのに数字だけ残っている投稿
STALL_ACCEL_MAX = _envf("STALL_ACCEL_MAX", 0.55)
STALL_MIN_AGE_MINUTES = _envf("STALL_MIN_AGE_MINUTES", 45)

# いいねだけ伸びて議論・拡散がほぼ無い（いいね稼ぎ・公式定型）
LIKE_FARM_MAX_REPLY = _envf("LIKE_FARM_MAX_REPLY", 0.02)
LIKE_FARM_MAX_RT = _envf("LIKE_FARM_MAX_RT", 0.04)
LIKE_FARM_MAX_QUOTE = _envf("LIKE_FARM_MAX_QUOTE", 0.01)
LIKE_FARM_MAX_BM = _envf("LIKE_FARM_MAX_BM", 0.03)
LIKE_FARM_MIN_LIKES = _envf("LIKE_FARM_MIN_LIKES", 300)

# 本文がほぼ応募誘導っぽい追加パターン（NGリストの補助）
_BAIT_PATTERNS = [
    re.compile(r"フォロー.?リツイート|フォロー.?リポスト|フォロー.?RT", re.I),
    re.compile(r"いいね.?RT|RT.?いいね|いいねとRT"),
    re.compile(r"抽選で\d|名様に当た|プレゼントキャンペーン"),
    re.compile(r"#PR\b|【PR】|\[PR\]"),
]


def reason(post: dict, g: dict) -> str | None:
    """誤検知っぽければ理由文字列、問題なければ None。"""
    likes = post.get("likes") or 0
    replies = post.get("replies") or 0
    rts = post.get("retweets") or 0
    quotes = post.get("quotes") or 0
    bms = post.get("bookmarks") or 0
    age = g.get("age_minutes") or 0
    accel = g.get("acceleration")
    text = post.get("text_snippet") or ""

    # 1) 実測で明確に失速 → もう伸び切っている
    if (
        g.get("is_measured")
        and accel is not None
        and accel < STALL_ACCEL_MAX
        and age >= STALL_MIN_AGE_MINUTES
    ):
        return f"失速済み(加速度{accel:.2f}<{STALL_ACCEL_MAX})"

    # 2) いいね稼ぎ: 数字は大きいが会話・拡散・保存がほぼ無い
    if likes >= LIKE_FARM_MIN_LIKES:
        rr = replies / likes
        tr = rts / likes
        qr = quotes / likes
        br = bms / likes
        if (
            rr < LIKE_FARM_MAX_REPLY
            and tr < LIKE_FARM_MAX_RT
            and qr < LIKE_FARM_MAX_QUOTE
            and br < LIKE_FARM_MAX_BM
        ):
            return (
                f"いいね稼ぎ疑い(返信{rr:.2f}/RT{tr:.2f}/引用{qr:.2f}/BM{br:.2f})"
            )

    # 3) 本文パターン（NGワードの取りこぼし補助）
    for pat in _BAIT_PATTERNS:
        if pat.search(text):
            return f"誘導文パターン({pat.pattern[:24]})"

    return None
