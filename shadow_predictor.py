"""Optional Gen1 shadow prediction.

Never affects production ranking. If no trained challenger exists, returns None.
"""
import math
from pathlib import Path

ARTIFACT = Path(__file__).parent / "data" / "gen1_challenger.joblib"
_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    if not ARTIFACT.exists():
        return None
    try:
        import joblib
        _cache = joblib.load(ARTIFACT)
        return _cache
    except Exception as e:
        print(f"[shadow] challenger load failed: {e}")
        return None


def predict(feature_row):
    obj = _load()
    if not obj:
        return None
    values = []
    for name in obj["features"]:
        v = feature_row.get(name)
        values.append(float(v) if v is not None else float("nan"))
    try:
        pred_log = float(obj["model"].predict([values])[0])
        return max(int(round(math.expm1(pred_log))), int(feature_row.get("impressions") or 0))
    except Exception as e:
        print(f"[shadow] prediction failed: {e}")
        return None
