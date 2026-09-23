"""Evaluate persisted Gen1 shadow predictions against real 24h outcomes."""
import json
import math
from collections import defaultdict
from pathlib import Path

import training_store

SNAPSHOTS = Path("data/training_snapshots.jsonl")
OUTCOMES = Path("data/training_outcomes.json")
REGISTRY = Path("data/model_registry.json")


def main():
    if not OUTCOMES.exists() or not REGISTRY.exists() or not training_store.snapshot_paths():
        print("[shadow-eval] inputs missing")
        return
    outcomes = json.loads(OUTCOMES.read_text(encoding="utf-8"))
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    groups = defaultdict(list)
    for r in training_store.iter_snapshot_rows():
        if r.get("post_id"):
            groups[str(r["post_id"])].append(r)

    paired = []
    for pid, rows in groups.items():
        y = (outcomes.get(pid) or {}).get("imp_24h")
        if y is None:
            continue
        rows = [
            r for r in rows
            if r.get("shadow_gen1_predicted_24h") is not None
            and r.get("predicted_final_impressions") is not None
            and float(r.get("age_minutes") or 9999) <= 240
        ]
        if not rows:
            continue
        r = min(rows, key=lambda x: x.get("observed_at") or "")
        paired.append((pid, int(r["predicted_final_impressions"]), int(r["shadow_gen1_predicted_24h"]), int(y)))

    if not paired:
        reg["shadow_metrics"] = {"paired_posts": 0, "status": "collecting"}
    else:
        def mae(index):
            return sum(abs(math.log1p(x[index]) - math.log1p(x[3])) for x in paired) / len(paired)
        g0, g1 = mae(1), mae(2)
        # Ranking evidence: rank the same paired cohort by each model.
        def recall_at(threshold, k, pred_index):
            positives = [x for x in paired if x[3] >= threshold]
            if not positives:
                return None
            ranked = sorted(paired, key=lambda x: x[pred_index], reverse=True)[:k]
            hit = {x[0] for x in ranked}
            return sum(x[0] in hit for x in positives) / len(positives)

        r10_0, r10_1 = recall_at(10_000_000, 50, 1), recall_at(10_000_000, 50, 2)
        r5_0, r5_1 = recall_at(5_000_000, 50, 1), recall_at(5_000_000, 50, 2)

        # DCG over actual 24h impressions; normalized against ideal ordering.
        def ndcg(pred_index, k=100):
            ranked = sorted(paired, key=lambda x: x[pred_index], reverse=True)[:k]
            ideal = sorted(paired, key=lambda x: x[3], reverse=True)[:k]
            def dcg(rows):
                total = 0.0
                for i, x in enumerate(rows):
                    relevance = math.log1p(x[3])
                    total += relevance / math.log2(i + 2)
                return total
            ideal_dcg = dcg(ideal)
            return dcg(ranked) / ideal_dcg if ideal_dcg else None
        n0, n1 = ndcg(1), ndcg(2)

        # Time-to-detection across every persisted shadow observation.
        # Misses receive a 240-minute penalty so a model cannot look fast by
        # simply failing to detect difficult viral posts.
        ttd_pairs = []
        for pid, _, _, y in paired:
            threshold = 10_000_000 if y >= 10_000_000 else (5_000_000 if y >= 5_000_000 else None)
            if threshold is None:
                continue
            obs = [
                r for r in groups[pid]
                if r.get("shadow_gen1_predicted_24h") is not None
                and r.get("predicted_final_impressions") is not None
                and float(r.get("age_minutes") or 9999) <= 240
            ]
            g0_times = [float(r["age_minutes"]) for r in obs if int(r["predicted_final_impressions"]) >= threshold]
            g1_times = [float(r["age_minutes"]) for r in obs if int(r["shadow_gen1_predicted_24h"]) >= threshold]
            ttd_pairs.append((min(g0_times) if g0_times else 240.0, min(g1_times) if g1_times else 240.0))
        ttd0 = sum(x[0] for x in ttd_pairs) / len(ttd_pairs) if ttd_pairs else None
        ttd1 = sum(x[1] for x in ttd_pairs) / len(ttd_pairs) if ttd_pairs else None

        reg["shadow_metrics"] = {
            "paired_posts": len(paired),
            "status": "evaluated",
            "gen0_log_mae": round(g0, 6),
            "gen1_log_mae": round(g1, 6),
            "recall_10m_at_50_gen0": r10_0,
            "recall_10m_at_50_gen1": r10_1,
            "recall_5m_at_50_gen0": r5_0,
            "recall_5m_at_50_gen1": r5_1,
            "ndcg_100_gen0": n0,
            "ndcg_100_gen1": n1,
            "recall_10m_at_50_not_worse": r10_0 is not None and r10_1 is not None and r10_1 >= r10_0,
            "recall_5m_at_50_not_worse": r5_0 is not None and r5_1 is not None and r5_1 >= r5_0,
            "ndcg_100_not_worse": n0 is not None and n1 is not None and n1 >= n0,
            "ttd_viral_minutes_gen0": ttd0,
            "ttd_viral_minutes_gen1": ttd1,
            "time_to_detection_not_worse": ttd0 is not None and ttd1 is not None and ttd1 <= ttd0,
        }
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[shadow-eval] paired={reg.get('shadow_metrics',{}).get('paired_posts',0)}")


if __name__ == "__main__":
    main()
