"""Dependency-free Gen1 shadow prediction from exported tree JSON.

Never affects production ranking until promotion gates explicitly pass.
"""
import json
import math
from pathlib import Path

ARTIFACT = Path(__file__).parent / "data" / "gen1_challenger_model.json"
_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    if not ARTIFACT.exists():
        return None
    try:
        _cache = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        return _cache
    except Exception as e:
        print(f"[shadow] challenger load failed: {e}")
        return None


def _tree_predict(tree, x):
    node = 0
    left, right = tree["children_left"], tree["children_right"]
    feature, threshold, value = tree["feature"], tree["threshold"], tree["value"]
    while left[node] != -1:
        idx = feature[node]
        node = left[node] if x[idx] <= threshold[node] else right[node]
    return value[node]


def predict(feature_row):
    obj = _load()
    if not obj:
        return None
    x = []
    for name in obj["features"]:
        v = feature_row.get(name)
        try:
            v = float(v)
            x.append(v if math.isfinite(v) else 0.0)
        except Exception:
            x.append(0.0)
    try:
        pred_log = float(obj["init_value"])
        lr = float(obj["learning_rate"])
        for tree in obj["trees"]:
            pred_log += lr * _tree_predict(tree, x)
        return max(int(round(math.expm1(pred_log))), int(feature_row.get("impressions") or 0))
    except Exception as e:
        print(f"[shadow] prediction failed: {e}")
        return None
