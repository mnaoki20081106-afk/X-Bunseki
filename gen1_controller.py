"""Automatic Gen1 readiness, shadow evaluation and guarded promotion.

This controller never replaces the production Gen0 model merely because enough
rows exist. It uses only real 24h outcomes, keeps every post in one chronological
split, and records readiness/evaluation state for unattended operation.

Gen1 training is intentionally gated behind completed-post and positive-class
requirements. Until those gates are met, production remains Gen0.
"""
import json
import math
from collections import defaultdict
from pathlib import Path

import training_store

from feature_engineering import make_features

SNAPSHOTS = Path("data/training_snapshots.jsonl")
OUTCOMES = Path("data/training_outcomes.json")
STATE = Path("data/model_registry.json")

REGRESSION_MIN_COMPLETED = 300
P5_MIN_POSITIVES = 40
P5_MIN_STALLED = 50
P10_MIN_POSITIVES = 25
P10_MIN_5M_POSITIVES = 80
MIN_COMPLETION_RATE = 0.70
PROMOTION_MIN_10M_POSITIVES = 15


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_snapshots(path=SNAPSHOTS):
    groups = defaultdict(list)
    rows = training_store.iter_snapshot_rows() if path == SNAPSHOTS else training_store._iter_jsonl(path)
    for row in rows:
        pid = row.get("post_id")
        if pid and row.get("age_minutes") is not None:
            groups[str(pid)].append(row)
    return groups


def dataset_stats(groups, outcomes):
    early_ids = set()
    for pid, rows in groups.items():
        if any(r.get("was_early_observed") is True and not r.get("is_rescue_only") for r in rows):
            early_ids.add(pid)

    complete = []
    for pid in early_ids:
        outcome = outcomes.get(pid) or {}
        if outcome.get("imp_24h") is not None:
            complete.append((pid, int(outcome["imp_24h"])))

    p5 = sum(y >= 5_000_000 for _, y in complete)
    p10 = sum(y >= 10_000_000 for _, y in complete)
    stalled = sum(y < 1_500_000 for _, y in complete)
    completion_rate = len(complete) / max(len(early_ids), 1)
    return {
        "early_observed_posts": len(early_ids),
        "completed_24h_posts": len(complete),
        "completion_rate": round(completion_rate, 4),
        "positive_5m": p5,
        "positive_10m": p10,
        "stalled_under_1_5m": stalled,
    }


def readiness(stats):
    regression = stats["completed_24h_posts"] >= REGRESSION_MIN_COMPLETED
    p5 = stats["positive_5m"] >= P5_MIN_POSITIVES and stats["stalled_under_1_5m"] >= P5_MIN_STALLED
    p10 = stats["positive_10m"] >= P10_MIN_POSITIVES and stats["positive_5m"] >= P10_MIN_5M_POSITIVES
    completion_ok = stats["completion_rate"] >= MIN_COMPLETION_RATE
    return {
        "regression_ready": regression and completion_ok,
        "p5_classifier_ready": p5 and completion_ok,
        "p10_classifier_ready": p10 and completion_ok,
        "completion_rate_ok": completion_ok,
        "requirements": {
            "regression_completed": REGRESSION_MIN_COMPLETED,
            "p5_positives": P5_MIN_POSITIVES,
            "p5_stalled": P5_MIN_STALLED,
            "p10_positives": P10_MIN_POSITIVES,
            "p10_5m_positives": P10_MIN_5M_POSITIVES,
            "minimum_completion_rate": MIN_COMPLETION_RATE,
        },
    }


def _first_time(rows):
    vals = [r.get("first_observed_at") or r.get("observed_at") for r in rows if r.get("observed_at")]
    return min(vals) if vals else ""


def build_dataset(groups, outcomes):
    """Leakage-safe rows: early <=4h observations with real 24h labels only."""
    posts = []
    for pid, rows in groups.items():
        outcome = outcomes.get(pid) or {}
        target = outcome.get("imp_24h")
        if target is None:
            continue
        rows = sorted(rows, key=lambda r: r.get("observed_at") or "")
        eligible = [
            r for r in rows
            if r.get("was_early_observed") is True
            and not r.get("is_rescue_only")
            and float(r.get("age_minutes") or 9999) <= 240
        ]
        if not eligible:
            continue
        feats = make_features(eligible)
        posts.append({
            "post_id": pid,
            "first_observed_at": _first_time(eligible),
            "target_24h": int(target),
            "rows": feats,
        })
    posts.sort(key=lambda x: (x["first_observed_at"], x["post_id"]))
    n = len(posts)
    train_end = max(int(n * 0.70), 1) if n else 0
    val_end = max(int(n * 0.85), train_end) if n else 0
    for i, p in enumerate(posts):
        p["split"] = "train" if i < train_end else ("validation" if i < val_end else "test")
    return posts


def historical_gen0_metrics(groups, outcomes):
    """Evaluate only predictions actually persisted at observation time."""
    rows = []
    for pid, snaps in groups.items():
        target = (outcomes.get(pid) or {}).get("imp_24h")
        if target is None:
            continue
        eligible = [
            r for r in snaps
            if r.get("was_early_observed") is True
            and not r.get("is_rescue_only")
            and float(r.get("age_minutes") or 9999) <= 240
            and r.get("predicted_final_impressions") is not None
        ]
        if not eligible:
            continue
        # Earliest contemporaneous prediction is the hardest/most useful early benchmark.
        r = min(eligible, key=lambda x: x.get("observed_at") or "")
        rows.append((pid, int(r["predicted_final_impressions"]), int(target)))
    if not rows:
        return {"posts": 0, "log_mae": None}
    err = sum(abs(math.log1p(pred) - math.log1p(y)) for _, pred, y in rows) / len(rows)
    return {"posts": len(rows), "log_mae": round(err, 6)}


def main():
    groups = load_snapshots()
    outcomes = _load_json(OUTCOMES, {})
    stats = dataset_stats(groups, outcomes)
    ready = readiness(stats)
    dataset = build_dataset(groups, outcomes)
    gen0 = historical_gen0_metrics(groups, outcomes)

    previous = _load_json(STATE, {})
    registry = {
        "schema_version": 1,
        "champion": previous.get("champion") or {
            "generation": "gen0",
            "kind": "grok_prior_calibrated_multiplier",
            "status": "production",
        },
        "challenger": previous.get("challenger"),
        "dataset_stats": stats,
        "readiness": ready,
        "dataset_posts": len(dataset),
        "split_posts": {
            s: sum(p["split"] == s for p in dataset)
            for s in ("train", "validation", "test")
        },
        "gen0_historical_metrics": gen0,
        "promotion_policy": {
            "automatic": True,
            "shadow_first": True,
            "minimum_10m_positives_for_promotion": PROMOTION_MIN_10M_POSITIVES,
            "requirements": [
                "10M Recall@50 must be >= champion",
                "5M Recall@50 must not regress",
                "NDCG@100 must be >= champion",
                "log-MAE must not be >15% worse",
                "time-to-detection must not regress",
            ],
        },
    }

    # Safety rule: readiness alone never promotes or replaces production.
    # A future Gen1 trainer may populate challenger only after training and then
    # a separate shadow evaluator must prove every promotion condition.
    if not ready["regression_ready"]:
        registry["automation_state"] = "collecting"
    elif registry["challenger"] is None:
        registry["automation_state"] = "ready_for_gen1_training"
    else:
        registry["automation_state"] = "shadow_evaluation"

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "[gen1] state=%s completed=%d p5=%d p10=%d completion=%.1f%%"
        % (
            registry["automation_state"],
            stats["completed_24h_posts"],
            stats["positive_5m"],
            stats["positive_10m"],
            stats["completion_rate"] * 100,
        )
    )


if __name__ == "__main__":
    main()
