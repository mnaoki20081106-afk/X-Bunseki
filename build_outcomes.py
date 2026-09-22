"""Build immutable learning outcomes from raw training snapshots.

Outcomes are labels, not predictions. A horizon is emitted only when there is
an observation close enough to that horizon; missing labels remain missing.
"""
import json
from collections import defaultdict
from pathlib import Path

DATA = Path("data/training_snapshots.jsonl")
OUT = Path("data/training_outcomes.json")
HORIZONS = {12: 720, 24: 1440}
TOLERANCE_MINUTES = {12: 90, 24: 120}


def load_groups(path=DATA):
    groups = defaultdict(list)
    if not path.exists():
        return groups
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("post_id") and row.get("age_minutes") is not None:
            groups[str(row["post_id"])].append(row)
    return groups


def _nearest(rows, target, tolerance):
    candidates = [r for r in rows if r.get("impressions") is not None]
    if not candidates:
        return None
    row = min(candidates, key=lambda r: abs(float(r["age_minutes"]) - target))
    return row if abs(float(row["age_minutes"]) - target) <= tolerance else None


def build_outcomes(groups):
    outcomes = {}
    for pid, rows in groups.items():
        rows = sorted(rows, key=lambda r: float(r["age_minutes"]))
        first = rows[0]
        out = {
            "post_id": pid,
            "posted_at": first.get("posted_at"),
            "first_observed_at": first.get("first_observed_at") or first.get("observed_at"),
            "first_observed_elapsed_min": first.get("first_observed_elapsed_min", first.get("age_minutes")),
            "was_early_observed": first.get("was_early_observed"),
            "is_rescue_only": first.get("is_rescue_only"),
            "max_imp_observed": max(int(r.get("impressions") or 0) for r in rows),
        }
        for hours, target in HORIZONS.items():
            row = _nearest(rows, target, TOLERANCE_MINUTES[hours])
            out[f"imp_{hours}h"] = int(row.get("impressions") or 0) if row else None
            out[f"observed_age_{hours}h"] = float(row["age_minutes"]) if row else None
        if out["imp_24h"] is not None:
            out["reached_5m_24h"] = out["imp_24h"] >= 5_000_000
            out["reached_10m_24h"] = out["imp_24h"] >= 10_000_000
        else:
            out["reached_5m_24h"] = None
            out["reached_10m_24h"] = None
        outcomes[pid] = out
    return outcomes


def main():
    outcomes = build_outcomes(load_groups())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2), encoding="utf-8")
    completed = sum(1 for x in outcomes.values() if x.get("imp_24h") is not None)
    print(f"[outcomes] posts={len(outcomes)} 24h_complete={completed}")


if __name__ == "__main__":
    main()
