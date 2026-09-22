"""Feature engineering for the viral-impression learning system.

Pure functions only: features are derived from immutable raw snapshots so the
definitions can evolve without losing historical data.
"""
import math
from datetime import datetime

EPS = 1.0


def _dt(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _minutes(a, b):
    return max((_dt(b) - _dt(a)).total_seconds() / 60.0, 1e-6)


def _rate(new, old, minutes):
    return max(float(new or 0) - float(old or 0), 0.0) / max(minutes, 1e-6)


def make_features(rows: list[dict]) -> list[dict]:
    """Return one feature row per observation, ordered by observed_at."""
    rows = sorted(rows, key=lambda r: r.get("observed_at") or "")
    out, velocities = [], []
    reaccel_count = 0
    in_reaccel = False

    for i, row in enumerate(rows):
        imp = max(float(row.get("impressions") or 0), 0.0)
        prev = rows[i - 1] if i else None
        velocity = None
        accel_ratio = None
        window = None
        if prev:
            window = _minutes(prev["observed_at"], row["observed_at"])
            velocity = _rate(imp, prev.get("impressions"), window)
            if velocities and velocities[-1] is not None:
                accel_ratio = velocity / (velocities[-1] + EPS)

        velocities.append(velocity)
        is_reaccel = accel_ratio is not None and accel_ratio >= 1.4
        if is_reaccel and not in_reaccel:
            reaccel_count += 1
        in_reaccel = is_reaccel

        valid_v = [(j, v) for j, v in enumerate(velocities) if v is not None]
        peak_v = max((v for _, v in valid_v), default=None)
        since_peak = None
        if peak_v is not None:
            peak_idx = max(j for j, v in valid_v if v == peak_v)
            since_peak = _minutes(rows[peak_idx]["observed_at"], row["observed_at"])

        # Time-weighted velocity over intervals ending within the last 60 min.
        v60_num = v60_den = 0.0
        now = _dt(row["observed_at"])
        for j in range(1, i + 1):
            end = _dt(rows[j]["observed_at"])
            if (now - end).total_seconds() > 3600:
                continue
            mins = _minutes(rows[j - 1]["observed_at"], rows[j]["observed_at"])
            if velocities[j] is not None:
                v60_num += velocities[j] * mins
                v60_den += mins
        v60 = v60_num / v60_den if v60_den else None

        def ratio(name):
            return float(row.get(name) or 0) / max(imp, 1.0)

        feat = {
            "post_id": row.get("post_id"),
            "observed_at": row.get("observed_at"),
            "elapsed_minutes": row.get("age_minutes"),
            "impressions": int(imp),
            "log1p_impressions": math.log1p(imp),
            "velocity_imp_per_min": velocity,
            "log1p_velocity": math.log1p(max(velocity or 0.0, 0.0)),
            "velocity_ratio_prev": accel_ratio,
            "velocity_60m_avg": v60,
            "velocity_recent_vs_60m": (velocity / (v60 + EPS)) if velocity is not None and v60 is not None else None,
            "peak_velocity": peak_v,
            "minutes_since_peak": since_peak,
            "reacceleration_count": reaccel_count,
            "like_per_imp": ratio("likes"),
            "retweet_per_imp": ratio("retweets"),
            "reply_per_imp": ratio("replies"),
            "quote_per_imp": ratio("quotes"),
            "bookmark_per_imp": ratio("bookmarks"),
            "observation_count": i + 1,
            "window_minutes": window,
            "missing_previous_observation": prev is None,
            "discovery_source": row.get("discovery_source"),
            "was_early_observed": row.get("was_early_observed"),
            "is_rescue_only": row.get("is_rescue_only"),
        }
        if prev:
            prev_imp = max(float(prev.get("impressions") or 0), 1.0)
            for name in ("likes", "retweets", "replies", "quotes", "bookmarks"):
                feat[f"{name}_velocity"] = _rate(row.get(name), prev.get(name), window)
                feat[f"{name}_ratio_change"] = ratio(name) - float(prev.get(name) or 0) / prev_imp
        out.append(feat)
    return out
