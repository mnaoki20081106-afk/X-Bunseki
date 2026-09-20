"""X collector using twscrape with browser-exported auth_token + ct0."""
from __future__ import annotations
import asyncio, json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
import keyword_filter

def _env(name, default):
    v=os.environ.get(name)
    return v if v is not None and str(v).strip() else default

SEARCH_MIN_FAVES=int(_env("SEARCH_MIN_FAVES","30"))
BROAD_MIN_FAVES=int(_env("BROAD_MIN_FAVES","250"))
COMBO_MIN_FAVES=int(_env("COMBO_MIN_FAVES","20"))
RESULTS_PER_QUERY=int(_env("RESULTS_PER_QUERY","40"))
BROAD_EXCLUDE=_env("BROAD_EXCLUDE","誕生日 生誕祭 発売記念 キャンペーン 先行配信").split()

class SessionExpiredError(Exception): pass

def _session_path():
    p=os.environ.get("X_SESSION_STATE_PATH","storage_state.json")
    if not os.path.exists(p): raise RuntimeError(f"セッションファイルが見つかりません: {p}")
    return p

def _cookie_header():
    data=json.loads(Path(_session_path()).read_text(encoding="utf-8"))
    pairs={c.get("name"):c.get("value") for c in (data.get("cookies") or []) if c.get("name") and c.get("value")}
    missing=[x for x in ("auth_token","ct0") if not pairs.get(x)]
    if missing: raise SessionExpiredError("storage_state に "+", ".join(missing)+" がありません")
    return "; ".join(f"{k}={v}" for k,v in pairs.items())

def build_queries():
    q=[]
    for group in keyword_filter.query_groups():
        q.append((f"({group}) lang:ja -filter:retweets min_faves:{SEARCH_MIN_FAVES}","Latest",RESULTS_PER_QUERY))
    for expr in keyword_filter.combo_queries():
        q.append((f"{expr} lang:ja -filter:retweets min_faves:{COMBO_MIN_FAVES}","Latest",RESULTS_PER_QUERY))
    exclusions=" ".join(f"-{w}" for w in BROAD_EXCLUDE)
    broad=f"lang:ja -filter:retweets -filter:replies min_faves:{BROAD_MIN_FAVES} {exclusions}".strip()
    q += [(broad,"Latest",RESULTS_PER_QUERY),(broad,"Top",RESULTS_PER_QUERY)]
    return q

def _iso(v):
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    return datetime.now(timezone.utc).isoformat()

def _normalize(t):
    u=getattr(t,"user",None)
    handle=getattr(u,"username","") if u else ""
    tid=str(getattr(t,"id","") or "")
    return {
      "post_id":tid, "author_handle":handle,
      "url":f"https://x.com/{handle}/status/{tid}" if handle and tid else f"https://x.com/i/status/{tid}",
      "posted_at":_iso(getattr(t,"date",None)),
      "text_snippet":str(getattr(t,"rawContent","") or "")[:280],
      "likes":int(getattr(t,"likeCount",0) or 0),
      "retweets":int(getattr(t,"retweetCount",0) or 0),
      "replies":int(getattr(t,"replyCount",0) or 0),
      "quotes":int(getattr(t,"quoteCount",0) or 0),
      "bookmarks":int(getattr(t,"bookmarkCount",0) or 0),
      "impressions":int(getattr(t,"viewCount",0) or 0),
    }

async def _collect():
    from twscrape import API, gather
    cookie=_cookie_header()
    db=os.path.join(tempfile.gettempdir(),"x_bunseki_twscrape.db")
    try: os.remove(db)
    except FileNotFoundError: pass
    api=API(db, raise_when_no_account=True, wait_timeout=10, wait_interval=1)
    await api.pool.add_account_cookies("x_bunseki", cookie)
    print("収集方式: twscrape / X SearchTimeline（Chromium不使用）")
    print("セッションCookie: auth_token=あり / ct0=あり")

    queries=build_queries()
    # Cheap canary first. If transport/auth is broken, don't hammer X 20 times.
    probe="lang:ja -filter:retweets"
    print(f"  [疎通確認] SearchTimeline: {probe}")
    try:
        probe_rows=await gather(api.search(probe, limit=1))
        print(f"  [疎通確認] 成功 ({len(probe_rows)}件)")
    except Exception as e:
        msg=str(e)
        if any(x in msg.lower() for x in ("401","403","auth","cookie","inactive")):
            raise SessionExpiredError(f"X SearchTimeline認証/アクセス失敗: {msg}") from e
        raise RuntimeError(f"X SearchTimeline疎通確認失敗: {msg}") from e

    all_posts={}
    failed=0
    print(f"検索クエリ数: {len(queries)}")
    for query,product,limit in queries:
        print(f"  検索[{product}]: {query[:90]}{'...' if len(query)>90 else ''}")
        try:
            kv={"product":product}
            rows=await gather(api.search(query,limit=limit,kv=kv))
            for t in rows:
                p=_normalize(t)
                if p["post_id"]: all_posts[p["post_id"]]=p
            print(f"  → {len(rows)}件(累計{len(all_posts)}件)")
        except Exception as e:
            failed+=1
            print(f"  [ERROR] {type(e).__name__}: {str(e)[:350]}")
    if queries and failed==len(queries):
        raise RuntimeError(f"X SearchTimeline が全{len(queries)}クエリで失敗しました")
    out=list(all_posts.values())
    out.sort(key=lambda p:(p["likes"],p["retweets"]),reverse=True)
    print(f"twscrape収集完了: {len(out)}件 / 失敗クエリ {failed}/{len(queries)}")
    return out

def fetch_posts():
    return asyncio.run(_collect())

def _parse_count(text):
    s=str(text or "").replace(",","").strip(); mult=1
    for suffix,m in (("万",10000),("億",100000000),("千",1000),("K",1000),("M",1000000)):
        if s.upper().endswith(suffix.upper()): mult=m;s=s[:-len(suffix)];break
    try:return int(float(s)*mult)
    except:return 0
def _parse_labeled_counts(label): return {}
def sanitize_counts(post): return post
