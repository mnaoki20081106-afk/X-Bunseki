"""Guarded self-calibration of final-impression multipliers.

Uses 6h+ follow-up observations as labels. Train/validation is split by post,
not by snapshot, so observations from one post cannot leak across the split.
"""
import json, math
from collections import defaultdict
from pathlib import Path

import training_store

DATA=Path("data/training_snapshots.jsonl")
MODEL=Path("data/impression_model.json")
MIN_POSTS=120
BINS=[15,30,45,60,90,120,180,240]


def load():
    d=defaultdict(list)
    for r in training_store.iter_snapshot_rows():
        try:
            if r.get("post_id") and r.get("impressions",0)>0 and r.get("age_minutes") is not None:
                d[r["post_id"]].append(r)
        except Exception:
            pass
    return d


def examples(groups):
    out=[]
    for pid, rows in groups.items():
        rows=sorted(rows,key=lambda x:x["age_minutes"])
        final=max(rows,key=lambda x:x["age_minutes"])
        if final["age_minutes"] < 360:
            continue
        y=final["impressions"]
        first_time=min(r["observed_at"] for r in rows)
        for r in rows:
            age=r["age_minutes"]
            if age>240 or r["impressions"]<=0:
                continue
            b=min(BINS,key=lambda x:abs(x-age))
            if abs(b-age)>12:
                continue
            out.append((first_time,pid,b,r["impressions"],y))
    return sorted(out)


def mae_log(ex, mult):
    if not ex:return 999
    return sum(abs(math.log1p(cur*mult.get(str(b),1))-math.log1p(y))
               for _,_,b,cur,y in ex)/len(ex)


def main():
    ex=examples(load())
    post_ids=sorted({pid for _,pid,_,_,_ in ex},
                    key=lambda pid:min(t for t,p,_,_,_ in ex if p==pid))
    if len(post_ids)<MIN_POSTS:
        print(f"[trainer] insufficient completed posts: {len(post_ids)}/{MIN_POSTS}")
        return

    cut=max(int(len(post_ids)*.8),1)
    train_ids=set(post_ids[:cut]); val_ids=set(post_ids[cut:])
    train=[x for x in ex if x[1] in train_ids]
    val=[x for x in ex if x[1] in val_ids]
    learned={}
    for b in BINS:
        ratios=sorted(y/cur for _,_,bb,cur,y in train if bb==b and cur>0)
        if len(ratios)<10: continue
        learned[str(b)]=round(ratios[len(ratios)//2],3)
    if len(learned)<3:
        print("[trainer] insufficient bins"); return

    default={"15":15.0,"30":8.0,"45":6.0,"60":4.5,"90":3.5,"120":2.5,"180":2.0,"240":1.5}
    old=default
    if MODEL.exists():
        try: old=json.loads(MODEL.read_text(encoding="utf-8"))["multipliers"]
        except Exception: pass
    old_err=mae_log(val,old); new_err=mae_log(val,{**old,**learned})
    print(f"[trainer] posts={len(post_ids)} validation log-MAE old={old_err:.4f} new={new_err:.4f}")
    if new_err >= old_err*0.98:
        print("[trainer] rejected: improvement <2%"); return
    payload={"version":2,"completed_posts":len(post_ids),"examples":len(ex),
             "validation_log_mae":round(new_err,5),"previous_log_mae":round(old_err,5),
             "multipliers":{**old,**learned}}
    MODEL.parent.mkdir(parents=True,exist_ok=True)
    MODEL.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print("[trainer] adopted new model")


if __name__=="__main__": main()
