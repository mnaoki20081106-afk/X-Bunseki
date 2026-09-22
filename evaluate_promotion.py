"""Promotion evaluator for Gen1 challenger.

Promotion is conservative and automatic only after enough 10M outcomes exist.
The evaluator compares contemporaneous Gen0 predictions with out-of-sample Gen1
test metrics. Ranking gates remain blocked until enough persisted rank evidence
exists, so lack of evidence can never trigger promotion.
"""
import json
from pathlib import Path

REGISTRY = Path("data/model_registry.json")
PROMOTION = Path("data/gen1_promotion.json")


def main():
    if not REGISTRY.exists():
        print("[promote] no registry")
        return
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    ch = reg.get("challenger")
    stats = reg.get("dataset_stats") or {}
    gen0 = reg.get("gen0_historical_metrics") or {}

    decision = {
        "promote": False,
        "reason": "",
        "checks": {},
    }
    if not ch:
        decision["reason"] = "no_challenger"
    elif stats.get("positive_10m", 0) < 15:
        decision["reason"] = "insufficient_10m_positive_evidence"
    else:
        gen0_mae = gen0.get("log_mae")
        gen1_mae = (ch.get("test") or {}).get("log_mae")
        decision["checks"]["log_mae_not_worse_15pct"] = (
            gen0_mae is not None and gen1_mae is not None and gen1_mae <= gen0_mae * 1.15
        )
        # Recall@K/NDCG/TTD require contemporaneous Gen1 shadow predictions.
        # Until that evidence exists, fail closed rather than guessing.
        shadow = reg.get("shadow_metrics") or {}
        required = [
            "recall_10m_at_50_not_worse",
            "recall_5m_at_50_not_worse",
            "ndcg_100_not_worse",
            "time_to_detection_not_worse",
        ]
        for key in required:
            decision["checks"][key] = shadow.get(key) is True
        if all(decision["checks"].values()):
            decision["promote"] = True
            decision["reason"] = "all_promotion_gates_passed"
            reg["champion"] = {
                "generation": "gen1",
                "kind": ch.get("kind"),
                "status": "production",
                "artifact": ch.get("artifact"),
            }
            reg["automation_state"] = "gen1_promoted"
        else:
            decision["reason"] = "shadow_evidence_incomplete_or_not_better"

    PROMOTION.parent.mkdir(parents=True, exist_ok=True)
    PROMOTION.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[promote] promote={decision['promote']} reason={decision['reason']}")


if __name__ == "__main__":
    main()
