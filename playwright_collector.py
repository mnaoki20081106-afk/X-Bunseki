"""X collector using Playwright with browser-exported storage state."""
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

async def _probe_engine(pw, engine_name, state):
    engine=getattr(pw, engine_name)
    browser=await engine.launch(headless=True)
    context=await browser.new_context(
        storage_state=state,
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        viewport={"width":1280,"height":800},
    )
    page=await context.new_page()
    try:
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)
        title=await page.title()
        url=page.url
        waiting="しばらくお待ちください" in title
        login="/login" in url or "/i/flow/login" in url
        home_dom=await page.locator('a[href="/home"], [data-testid="primaryColumn"]').count()
        print(f"[browser-probe] {engine_name}: URL={url} title={title!r} waiting={waiting} login={login} home_dom={home_dom}")
        if not waiting and not login and home_dom:
            return browser,context,page
    except Exception as e:
        print(f"[browser-probe] {engine_name}: ERROR {type(e).__name__}: {str(e)[:300]}")
    await context.close()
    await browser.close()
    return None

async def _collect():
    from playwright.async_api import async_playwright
    state=_session_path()
    queries=build_queries()
    all_posts={}

    async with async_playwright() as pw:
        selected=None
        print("収集方式: Playwright 3ブラウザ比較 / Chromium → Firefox → WebKit")
        print("セッションCookie: auth_token=あり / ct0=あり")
        for engine_name in ("chromium","firefox","webkit"):
            result=await _probe_engine(pw,engine_name,state)
            if result:
                selected=(engine_name,*result)
                print(f"[browser-probe] 採用: {engine_name}")
                break
        if not selected:
            raise RuntimeError("Chromium / Firefox / WebKit の全てでX Home通常DOMへ到達できませんでした")

        engine_name,browser,context,page=selected
        try:
            print(f"検索クエリ数: {len(queries)} / browser={engine_name}")
            for query,product,limit in queries:
                from urllib.parse import quote
                search_url=f"https://x.com/search?q={quote(query)}&src=typed_query&f={'live' if product == 'Latest' else 'top'}"
                print(f"  検索[{product}]: {query[:90]}{'...' if len(query)>90 else ''}")
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3500)
                    articles=page.locator('article[data-testid="tweet"]')
                    n=min(await articles.count(),limit)
                    added=0
                    for i in range(n):
                        a=articles.nth(i)
                        text=(await a.inner_text()).strip()
                        links=await a.locator('a[href*="/status/"]').evaluate_all("(els)=>els.map(e=>e.getAttribute('href'))")
                        href=next((x for x in links if x and "/status/" in x),None)
                        if not href: continue
                        parts=href.split("/status/")
                        handle=parts[0].strip("/").split("/")[-1]
                        tid=parts[1].split("?")[0].split("/")[0]
                        if not tid or tid in all_posts: continue
                        all_posts[tid]={
                          "post_id":tid,"author_handle":handle,"url":"https://x.com"+href.split("?")[0],
                          "posted_at":datetime.now(timezone.utc).isoformat(),"text_snippet":text[:280],
                          "likes":0,"retweets":0,"replies":0,"quotes":0,"bookmarks":0,"impressions":0,
                        }
                        added+=1
                    print(f"  → DOM {n}件 / 新規{added}件 (累計{len(all_posts)}件)")
                except Exception as e:
                    print(f"  [ERROR] {type(e).__name__}: {str(e)[:350]}")
        finally:
            await context.close()
            await browser.close()

    out=list(all_posts.values())
    print(f"Playwright収集完了: {len(out)}件")
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
