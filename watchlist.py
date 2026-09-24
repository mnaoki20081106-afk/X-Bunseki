"""Persistent follow-up watchlist for viral posts.

Discovery and tracking are deliberately separate: once a post is interesting,
we keep its id and re-read TweetDetail even if SearchTimeline stops returning it.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).parent / "data" / "watchlist.json"
TARGET_MINUTES = [15, 30, 45, 60, 90, 120, 180, 240, 300, 360, 480, 600, 720, 960, 1200, 1440]


def _dt(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def load() -> dict:
    if not PATH.exists():
        return {}
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def due_posts(now=None, limit=40) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = []
    for row in load().values():
        if row.get("completed"):
            continue
        due = row.get("next_due_at")
        if not due or _dt(due) <= now:
            rows.append(row)
    rows.sort(key=lambda r: r.get("next_due_at") or "")
    return rows[:limit]


def _next_due(posted_at: str, age_min: float, now: datetime):
    posted = _dt(posted_at)
    for target in TARGET_MINUTES:
        if target > age_min + 2:
            return (posted.timestamp() + target * 60, target)
    return (None, None)


def _age_minutes(posted_at: str, now: datetime) -> float:
    return max(0.0, (now - _dt(posted_at)).total_seconds() / 60.0)


def mark_detail_skipped(post_ids: list[str], now=None) -> dict:
    """Advance checkpoints for post-local TweetDetail gaps.

    A deleted/private post or a response missing one learning metric must not stay
    permanently overdue. Re-requesting the same stale rows every cycle starves
    newer watchlist entries and incorrectly looks like a global collection failure.
    """
    now = now or datetime.now(timezone.utc)
    state = load()
    changed = False
    for raw_pid in dict.fromkeys(str(pid) for pid in post_ids if pid):
        row = state.get(raw_pid)
        if not row or row.get("completed"):
            continue
        row["last_detail_skip_at"] = now.isoformat()
        row["consecutive_detail_skips"] = int(row.get("consecutive_detail_skips") or 0) + 1
        posted_at = row.get("posted_at")
        if posted_at:
            try:
                age = _age_minutes(posted_at, now)
                ts, target = _next_due(posted_at, age, now)
                if ts is None:
                    row["completed"] = True
                    row["next_due_at"] = None
                    row["next_target_minutes"] = None
                else:
                    row["next_due_at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
                    row["next_target_minutes"] = target
            except Exception:
                # Bad legacy metadata should not make the whole monitor fail.
                row["next_due_at"] = now.isoformat()
        state[raw_pid] = row
        changed = True
    if changed:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def update(evaluated: list[dict], now=None) -> dict:
    """Add strong discoveries and advance existing entries to their next checkpoint."""
    now = now or datetime.now(timezone.utc)
    state = load()
    seen = {p.get("post_id"): p for p in evaluated}

    for pid, p in seen.items():
        if not pid:
            continue
        g = p.get("growth") or {}
        age = float(g.get("age_minutes") or 0)
        imp = int(p.get("impressions") or 0)
        source = p.get("discovery_source") or "keyword"
        # Broad viral discovery is intentionally permissive; TweetDetail tracking
        # will quickly discard dead posts. Million-rescue posts are always tracked.
        # Learning cohort: once a post is a meaningful early candidate, keep
        # following it even if it later stalls. Dead candidates are valuable
        # negative examples and must not disappear from the training set.
        early_candidate = (
            age <= 240
            and (
                not p.get("rejected_reason")
                or (p.get("predicted_final_impressions") or 0) >= 1_000_000
                or (p.get("buzz_score") or 0) >= 45
            )
        )
        interesting = (
            p.get("million_imp_bypass")
            or early_candidate
            or (source in ("viral_search", "trend", "for_you") and age <= 240
                and (imp >= 50_000 or (p.get("likes") or 0) >= 2_000 or (p.get("retweets") or 0) >= 400))
        )
        if pid not in state and not interesting:
            continue
        row = state.get(pid, {})
        row.update({
            "post_id": pid,
            "author_handle": p.get("author_handle") or row.get("author_handle", ""),
            "author_name": p.get("author_name") or row.get("author_name") or p.get("author_handle") or "",
            "url": p.get("url") or row.get("url", ""),
            "posted_at": p.get("posted_at") or row.get("posted_at"),
            "text_snippet": p.get("text_snippet") or row.get("text_snippet", ""),
            "discovery_source": source if source != "watchlist" else row.get("discovery_source", source),
            "last_impressions": imp,
            "last_likes": int(p.get("likes") or 0),
            "last_retweets": int(p.get("retweets") or 0),
            "last_replies": int(p.get("replies") or 0),
            "last_quotes": int(p.get("quotes") or 0),
            "last_bookmarks": int(p.get("bookmarks") or 0),
            "last_impressions_per_min": g.get("impressions_per_min"),
            "last_impressions_acceleration": g.get("impressions_acceleration"),
            "last_likes_per_min": g.get("likes_per_min"),
            "last_buzz_score": p.get("buzz_score"),
            "last_predicted_final_impressions": p.get("predicted_final_impressions"),
            "last_prediction_confidence": p.get("prediction_confidence"),
            "million_imp_bypass": bool(p.get("million_imp_bypass")),
            "million_imp_bypass_detail": p.get("million_imp_bypass_detail") or "",
            "last_age_minutes": age,
            "last_seen_at": now.isoformat(),
            "consecutive_detail_skips": 0,
        })
        if not row.get("added_at"):
            row["added_at"] = now.isoformat()
            row["first_observed_at"] = now.isoformat()
            row["first_observed_elapsed_min"] = age
            row["first_discovery_source"] = source
            row["was_early_observed"] = age <= 240
            row["is_rescue_only"] = bool(p.get("million_imp_bypass")) and age > 240
        row["learning_cohort"] = bool(row.get("learning_cohort") or early_candidate)
        if row.get("posted_at"):
            ts, target = _next_due(row["posted_at"], age, now)
            if ts is None:
                row["completed"] = True
                row["next_due_at"] = None
                row["next_target_minutes"] = None
            else:
                row["completed"] = False
                row["next_due_at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
                row["next_target_minutes"] = target
        state[pid] = row

    # Rows that were never observed again after becoming due must still age out.
    # Otherwise they remain permanently overdue and monopolize due_posts().
    for row in state.values():
        if row.get("completed") or not row.get("posted_at"):
            continue
        try:
            if _age_minutes(row["posted_at"], now) > TARGET_MINUTES[-1] + 2:
                row["completed"] = True
                row["next_due_at"] = None
                row["next_target_minutes"] = None
        except Exception:
            pass

    # Keep completed rows for 7 days for debugging, then compact.
    compact = {}
    for pid, row in state.items():
        last = row.get("last_seen_at") or row.get("added_at")
        if row.get("completed") and last:
            try:
                if (now - _dt(last)).total_seconds() > 7 * 86400:
                    continue
            except Exception:
                pass
        compact[pid] = row
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
    return compact
