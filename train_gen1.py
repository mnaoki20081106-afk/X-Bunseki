"""Train a Gen1 nonlinear challenger from leakage-safe 24h outcomes.

Runs separately from the 15-minute monitor so model training can never delay
viral discovery. Requires the readiness gates written by gen1_controller.py.
"""
import json
import math
from pathlib import Path

from gen1_controller import load_snapshots, build_dataset, readiness, dataset_stats

OUTCOMES = Path("data/training_outcomes.json")
REGISTRY = Path("data/model_registry.json")
MODEL = Path("data/gen1_challenger.json")

FEATURES = [
    "elapsed_minutes", "log1p_impressions", "velocity_imp_per_min",
    "log1p_velocity", "velocity_ratio_prev", "velocity_60m_avg",
    "velocity_recent_vs_60m", "peak_velocity", "minutes_since_peak",
    "reacceleration_count", "like_per_imp", "retweet_per_imp",
    "reply_per_imp", "quote_per_imp", "bookmark_per_imp",
    "observation_count",
]


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _flatten(posts, split):
    rows = []
    for p in posts:
        if p["split"] != split:
            continue
        n = max(len(p["rows"]), 1)
        base_weight = 1.0 / math.sqrt(n)
        y = p["target_24h"]
        class_weight = 2.5 if y >= 10_000_000 else (1.5 if y >= 5_000_000 else 1.0)
        for f in p["rows"]:
            x = []
            for name in FEATURES:
                v = f.get(name)
                x.append(float(v) if v is not None else float("nan"))
            rows.append((p["post_id"], x, math.log1p(y), base_weight * class_weight))
    return rows


def _metrics(model, rows):
    if not rows:
        return {"rows": 0, "posts": 0, "log_mae": None}
    import numpy as np
    X = np.array([x for _, x, _, _ in rows], dtype=float)
    y = np.array([y for _, _, y, _ in rows], dtype=float)
    pred = model.predict(X)
    return {
        "rows": len(rows),
        "posts": len({pid for pid, *_ in rows}),
        "log_mae": round(float(np.mean(np.abs(pred - y))), 6),
    }


def main():
    outcomes = _load(OUTCOMES, {})
    groups = load_snapshots()
    stats = dataset_stats(groups, outcomes)
    gates = readiness(stats)
    if not gates["regression_ready"]:
        print("[gen1-train] not ready; production Gen0 unchanged")
        return

    # Import only when training is actually eligible. Monitor never needs sklearn.
    try:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor
    except Exception as e:
        print(f"[gen1-train] sklearn unavailable: {e}")
        return

    posts = build_dataset(groups, outcomes)
    train = _flatten(posts, "train")
    val = _flatten(posts, "validation")
    test = _flatten(posts, "test")
    if not train or not val:
        print("[gen1-train] insufficient leakage-safe split")
        return

    X = np.array([x for _, x, _, _ in train], dtype=float)
    y = np.array([y for _, _, y, _ in train], dtype=float)
    w = np.array([w for _, _, _, w in train], dtype=float)

    model = HistGradientBoostingRegressor(
        loss="absolute_error",
        learning_rate=0.05,
        max_iter=250,
        max_leaf_nodes=15,
        min_samples_leaf=15,
        l2_regularization=1.0,
        random_state=20260922,
    )
    model.fit(X, y, sample_weight=w)

    # JSON-native serialization: persist sklearn estimator through joblib in the
    # dedicated artifact, while registry contains auditable metrics/metadata.
    import joblib
    artifact = Path("data/gen1_challenger.joblib")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES}, artifact)

    val_metrics = _metrics(model, val)
    test_metrics = _metrics(model, test)
    payload = {
        "generation": "gen1",
        "kind": "hist_gradient_boosting_regression",
        "target": "log1p_24h_impressions",
        "features": FEATURES,
        "dataset_stats": stats,
        "validation": val_metrics,
        "test": test_metrics,
        "status": "shadow",
        "artifact": str(artifact),
    }
    MODEL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = _load(REGISTRY, {})
    registry["challenger"] = payload
    registry["automation_state"] = "shadow_evaluation"
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[gen1-train] challenger trained; validation log-MAE={val_metrics['log_mae']}")


if __name__ == "__main__":
    main()
