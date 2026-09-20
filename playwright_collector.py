"""X collector using direct web GraphQL (tweetkit-x), no headless browser."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from pathlib import Path
import keyword_filter

def _env(name, default):
    v=os.environ.get(name)
    return v if v is not None and str(v).strip() else default

SEARCH_MIN_FAVES=int(_env("SEARCH_MIN_FAVES","30"))
BROAD_MIN_FAVES=int(_env("BROAD_MIN_FAVES","250"))
COMBO_MIN_FAVES=int(_env("COMBO_MIN_FAVES","20"))
COLLECT_MAX_AGE_MINUTES=float(_env("COLLECT_MAX_AGE_MINUTES","240"))
RESULTS_PER_QUERY=int(_env("RESULTS_PER_QUERY","40"))
BROAD_EXCLUDE=_env("BROAD_EXCLUDE","誕生日 生誕祭 発売記念 キャンペーン 先行配信").split()

class SessionExpiredError(Exception):
    pass

def _session_path():
    p=os.environ.get("X_SESSION_STATE_PATH","storage_state.json")
    if not os.path.exists(p):
        raise RuntimeError(f"セッションファイルが見つかりません: {p}")
    return p

def _cookie_header():
    data=json.loads(Path(_session_path()).read_text(encoding="utf-8"))
    pairs={}
    for c in data.get("cookies") or []:
        if c.get("name") and c.get("value"):
            pairs[c["name"]]=c["value"]
    missing=[x for x in ("auth_token","ct0") if not pairs.get(x)]
    if missing:
        raise SessionExpiredError("storage_state に "+", ".join(missing)+" がありません")
    # tweetkit-x requires auth_token + ct0; preserve any extra browser cookies if present.
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

def _iso(created):
    if not created:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.strptime(created,"%a %b %d %H:%M:%S +0000 %Y").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()

def _normalize(t):
    return {
        "post_id": str(t.get("id") or ""),
        "author_handle": t.get("author") or "",
        "url": t.get("url") or (f"https://x.com/i/status/{t.get('id')}" if t.get("id") else ""),
        "posted_at": _iso(t.get("created_at")),
        "text_snippet": (t.get("text") or "")[:280],
        "likes": int(t.get("likes") or 0),
        "retweets": int(t.get("retweets") or 0),
        "replies": int(t.get("replies") or 0),
        "quotes": int(t.get("quotes") or 0),
        "bookmarks": int(t.get("bookmarks") or 0),
        "impressions": int(t.get("views") or t.get("impressions") or 0),
    }

def fetch_posts():
    from twitter_cli.client import TwitterClient
    cookie=_cookie_header()
    pairs = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            pairs[k] = v
    print("収集方式: twitter-cli / X SearchTimeline POST + curl_cffi（Chromium不使用）")
    print("セッションCookie: auth_token=あり / ct0=あり")
    try:
        client=TwitterClient(
            pairs["auth_token"],
            pairs["ct0"],
            rate_limit_config={"requestDelay": 1.0, "maxRetries": 2, "retryBaseDelay": 3.0, "maxCount": RESULTS_PER_QUERY},
            cookie_string=cookie,
        )
        print("twitter-cli 初期化完了")
    except Exception as e:
        raise SessionExpiredError(f"twitter-cliセッション初期化失敗: {e}") from e

    all_posts={}
    queries=build_queries()
    print(f"検索クエリ数: {len(queries)}")
    failed=0
    auth_fail=0
    for query, product, limit in queries:
        print(f"  POST検索[{product}]: {query[:80]}{'...' if len(query)>80 else ''}")
        try:
            rows=client.fetch_search(query, count=limit, product=product)
            for raw in rows:
                p={
                    "post_id": str(raw.id or ""),
                    "author_handle": raw.author.screen_name if raw.author else "",
                    "url": f"https://x.com/{raw.author.screen_name}/status/{raw.id}" if raw.author and raw.id else "",
                    "posted_at": _iso(raw.created_at),
                    "text_snippet": (raw.text or "")[:280],
                    "likes": int(raw.metrics.likes or 0),
                    "retweets": int(raw.metrics.retweets or 0),
                    "replies": int(raw.metrics.replies or 0),
                    "quotes": int(raw.metrics.quotes or 0),
                    "bookmarks": int(raw.metrics.bookmarks or 0),
                    "impressions": int(raw.metrics.views or 0),
                }
                if p["post_id"]:
                    all_posts[p["post_id"]]=p
            print(f"  → {len(rows)}件(累計{len(all_posts)}件)")
        except Exception as e:
            failed+=1
            msg=str(e)
            if any(x in msg for x in ("HTTP 401","HTTP 403","Could not authenticate","Unauthorized")):
                auth_fail+=1
            print(f"  [ERROR] POST検索失敗: {msg[:300]}")

    if queries and failed==len(queries):
        if auth_fail:
            raise SessionExpiredError(f"X X認証に失敗しました ({auth_fail}/{len(queries)}クエリ)")
        raise RuntimeError(f"X SearchTimeline が全{len(queries)}クエリで失敗しました")
    result=list(all_posts.values())
    result.sort(key=lambda p:(p.get("likes",0),p.get("retweets",0)),reverse=True)
    print(f"POST収集完了: {len(result)}件 / 失敗クエリ {failed}/{len(queries)}")
    return result

# compatibility exports used by tests/older code
def _parse_count(text):
    s=str(text or "").replace(",","").strip()
    mult=1
    for suffix,m in (("万",10000),("億",100000000),("千",1000),("K",1000),("M",1000000)):
        if s.upper().endswith(suffix.upper()):
            mult=m;s=s[:-len(suffix)];break
    try:return int(float(s)*mult)
    except:return 0

def _parse_labeled_counts(label): return {}
def sanitize_counts(post): return post
