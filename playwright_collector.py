import json
import os
import subprocess
import re
from pathlib import Path
from datetime import datetime, timezone

import keyword_filter
import watchlist

def _env(name, default):
    v = os.environ.get(name)
    return v if v is not None and str(v).strip() else default

SEARCH_MIN_FAVES = int(_env("SEARCH_MIN_FAVES", "30"))
BROAD_MIN_FAVES = int(_env("BROAD_MIN_FAVES", "250"))
COMBO_MIN_FAVES = int(_env("COMBO_MIN_FAVES", "20"))
RESULTS_PER_QUERY = int(_env("RESULTS_PER_QUERY", "40"))
BROAD_EXCLUDE = _env("BROAD_EXCLUDE", "誕生日 生誕祭 発売記念 キャンペーン 先行配信").split()

class SessionExpiredError(Exception):
    pass


def _parse_count(value):
    """Compatibility parser for X's localized compact counters."""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    if not s or s in {"なし", "-", "—"}:
        return 0
    mult = 1
    if s[-1:].lower() == "k":
        mult, s = 1_000, s[:-1]
    elif s[-1:].lower() == "m":
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("万"):
        mult, s = 10_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _parse_labeled_counts(label):
    out = {"replies": 0, "retweets": 0, "likes": 0, "bookmarks": 0, "impressions": 0}
    patterns = {
        "replies": [r"([\d,.]+(?:万|[KkMm])?)件の返信", r"([\d,.]+(?:[KkMm])?)\s+replies?"],
        "retweets": [r"([\d,.]+(?:万|[KkMm])?)件のリポスト", r"([\d,.]+(?:[KkMm])?)\s+reposts?"],
        "likes": [r"([\d,.]+(?:万|[KkMm])?)件のいいね", r"([\d,.]+(?:[KkMm])?)\s+likes?"],
        "bookmarks": [r"([\d,.]+(?:万|[KkMm])?)件のブックマーク", r"([\d,.]+(?:[KkMm])?)\s+bookmarks?"],
        "impressions": [r"([\d,.]+(?:万|[KkMm])?)件の表示", r"([\d,.]+(?:[KkMm])?)\s+views?"],
    }
    for key, pats in patterns.items():
        for pat in pats:
            m = re.search(pat, str(label), re.I)
            if m:
                out[key] = _parse_count(m.group(1))
                break
    return out


def sanitize_counts(post):
    """Legacy API kept for tests/callers; x-agent already returns raw counts."""
    return dict(post)

def _session_path():
    p = os.environ.get("X_SESSION_STATE_PATH", "storage_state.json")
    if not os.path.exists(p):
        raise RuntimeError(f"セッションファイルが見つかりません: {p}")
    return p

def build_queries():
    q = []
    for group in keyword_filter.query_groups():
        q.append((f"({group}) lang:ja -filter:retweets min_faves:{SEARCH_MIN_FAVES}", "Latest", RESULTS_PER_QUERY))
    for expr in keyword_filter.combo_queries():
        q.append((f"{expr} lang:ja -filter:retweets min_faves:{COMBO_MIN_FAVES}", "Latest", RESULTS_PER_QUERY))
    exclusions = " ".join(f"-{w}" for w in BROAD_EXCLUDE)
    broad = f"lang:ja -filter:retweets -filter:replies min_faves:{BROAD_MIN_FAVES} {exclusions}".strip()
    q += [(broad, "Latest", RESULTS_PER_QUERY), (broad, "Top", RESULTS_PER_QUERY)]

    # Keyword-independent Viral Discovery. All four signals run every 15-minute
    # cycle in both Latest and Top. Search is breadth-first/concurrent below, so
    # this no longer needs the old 30-minute rotation.
    viral = [
        "lang:ja -filter:retweets min_faves:2000",
        "lang:ja -filter:retweets min_faves:5000",
        "lang:ja -filter:retweets min_retweets:400",
        "lang:ja -filter:retweets min_faves:2000 min_retweets:200",
    ]
    for query in viral:
        q.append((query, "Latest", min(RESULTS_PER_QUERY, 40)))
        q.append((query, "Top", min(RESULTS_PER_QUERY, 40)))
    return q

def _normalize_created_at(value):
    if not value:
        return datetime.now(timezone.utc).isoformat()
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S +0000 %Y"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    print(f"[WARN] 投稿日時を解釈できません: {s!r}")
    return datetime.now(timezone.utc).isoformat()

def _ensure_x_agent():
    pkg = Path("node_modules/x-agent-sdk/package.json")
    if pkg.exists():
        return
    print("x-agent-sdk をインストールします")
    subprocess.run(["npm", "install", "--no-save", "x-agent-sdk@0.2.3"], check=True)

def fetch_posts():
    state = json.loads(Path(_session_path()).read_text(encoding="utf-8"))
    cookies = {c.get("name"): c.get("value") for c in state.get("cookies", [])}
    if not cookies.get("auth_token") or not cookies.get("ct0"):
        raise SessionExpiredError("storage_state に auth_token または ct0 がありません")

    queries = build_queries()
    viral_query_count = 8
    keyword_query_count = len(queries) - viral_query_count
    payload = [
        {"query": q, "product": product, "limit": limit,
         "source": "keyword" if i < keyword_query_count else "viral_search"}
        for i, (q, product, limit) in enumerate(queries)
    ]
    Path(".x-agent-queries.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    due = watchlist.due_posts(limit=int(_env("WATCHLIST_DETAIL_LIMIT", "40")))
    Path(".x-agent-watchlist.json").write_text(json.dumps(due, ensure_ascii=False), encoding="utf-8")
    print(f"watchlist追跡対象: {len(due)}件")
    _ensure_x_agent()

    script = r'''
import fs from 'node:fs';
import { XClient } from 'x-agent-sdk';

const state=JSON.parse(fs.readFileSync(process.env.X_SESSION_STATE_PATH||'storage_state.json','utf8'));
const cookies=Object.fromEntries((state.cookies||[]).map(c=>[c.name,c.value]));
if(!cookies.auth_token || !cookies.ct0) throw new Error('SESSION_EXPIRED: auth_token/ct0 missing');
const queries=JSON.parse(fs.readFileSync('.x-agent-queries.json','utf8'));
const watch=JSON.parse(fs.readFileSync('.x-agent-watchlist.json','utf8'));

const SEARCH_CONCURRENCY=Math.max(1,Number(process.env.SEARCH_CONCURRENCY||2));
const DETAIL_CONCURRENCY=Math.max(1,Number(process.env.DETAIL_CONCURRENCY||2));
const SEARCH_TIMEOUT_MS=Math.max(5000,Number(process.env.SEARCH_TIMEOUT_MS||18000));
const DETAIL_TIMEOUT_MS=Math.max(5000,Number(process.env.DETAIL_TIMEOUT_MS||12000));
const SEARCH_BUDGET_MS=Math.max(30000,Number(process.env.SEARCH_BUDGET_MS||100000));
const DETAIL_BUDGET_MS=Math.max(15000,Number(process.env.DETAIL_BUDGET_MS||90000));
const started=Date.now();
const rawById=new Map();
let searchResponses=0, searchTweets=0, searchTweetsWithViews=0;
let rateLimited=0, forbidden=0;

function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
async function mapLimit(items, limit, fn){
  const out=new Array(items.length); let next=0;
  async function worker(){
    while(true){ const i=next++; if(i>=items.length) return;
      try { out[i]={status:'fulfilled',value:await fn(items[i],i)}; }
      catch(e){ out[i]={status:'rejected',reason:e}; }
    }
  }
  await Promise.all(Array.from({length:Math.min(limit,items.length)},()=>worker()));
  return out;
}
function unwrapResult(r){
  let cur=r;
  for(let i=0;i<4;i++){
    if(!cur) break;
    if(cur.legacy) return cur;
    if(cur.tweet) cur=cur.tweet;
    else if(cur.result) cur=cur.result;
    else break;
  }
  return cur;
}
function harvestSearch(data){
  const instr=data?.data?.search_by_raw_query?.search_timeline?.timeline?.instructions||[];
  let n=0, v=0;
  for(const ins of instr) for(const e of ins.entries||[]){
    const candidates=[
      e?.content?.itemContent?.tweet_results?.result,
      ...(e?.content?.items||[]).map(x=>x?.item?.itemContent?.tweet_results?.result)
    ];
    for(const candidate of candidates){
      const r=unwrapResult(candidate), lg=r?.legacy;
      if(!lg?.id_str) continue;
      n++;
      const views=Number(r?.views?.count||0)||0;
      if(views>0) v++;
      rawById.set(String(lg.id_str),{
        impressions:views,
        likes:Number(lg.favorite_count||0)||0,
        retweets:Number(lg.retweet_count||0)||0,
        replies:Number(lg.reply_count||0)||0,
        quotes:Number(lg.quote_count||0)||0,
        bookmarks:Number(lg.bookmark_count||0)||0
      });
    }
  }
  searchTweets+=n; searchTweetsWithViews+=v;
}
const trackedFetch=async (url,init={})=>{
  const timeout=String(url).includes('/SearchTimeline')?SEARCH_TIMEOUT_MS:DETAIL_TIMEOUT_MS;
  const signal=init.signal ? AbortSignal.any([init.signal,AbortSignal.timeout(timeout)]) : AbortSignal.timeout(timeout);
  const res=await fetch(url,{...init,signal});
  if(res.status===429) rateLimited++;
  if(res.status===403) forbidden++;
  if(String(url).includes('/SearchTimeline')){
    searchResponses++;
    try { harvestSearch(await res.clone().json()); } catch {}
  }
  return res;
};
const x=new XClient({
  authToken:cookies.auth_token,ct0:cookies.ct0,retries:1,fetch:trackedFetch,
  // x-agent's default 429 sleep can wait until the whole rate-limit window.
  // With retries=1 that only stalls this cycle, so cap it and let the next cycle retry.
  sleep:ms=>sleep(Math.min(ms,2000))
});

const seen=new Map();
function mergeTweet(t,spec){
  if(!t?.id) return;
  const id=String(t.id), raw=rawById.get(id)||{};
  const old=seen.get(id);
  const sources=new Set(old?.discovery_sources||[]);
  sources.add(spec.source||'keyword');
  const base={
    post_id:id,
    author_handle:t.author||old?.author_handle||'',
    url:t.url||old?.url||('https://x.com/i/status/'+id),
    posted_at:t.created_at||old?.posted_at||new Date().toISOString(),
    text_snippet:(t.text||old?.text_snippet||'').slice(0,280),
    likes:Number(raw.likes??t.likes??old?.likes??0)||0,
    retweets:Number(raw.retweets??t.retweets??old?.retweets??0)||0,
    replies:Number(raw.replies??t.replies??old?.replies??0)||0,
    quotes:Number(raw.quotes??old?.quotes??0)||0,
    bookmarks:Number(raw.bookmarks??old?.bookmarks??0)||0,
    impressions:Number(raw.impressions??old?.impressions??0)||0,
    discovery_sources:[...sources],
    discovery_source:sources.has('viral_search')?'viral_search':(old?.discovery_source||spec.source||'keyword'),
    discovery_query_hits:(old?.discovery_query_hits||0)+1
  };
  seen.set(id,base);
}
async function searchOne(spec,cursor){
  // Small jitter avoids perfectly synchronized request bursts.
  await sleep(100+Math.floor(Math.random()*301));
  const r=await x.searchPage(spec.query,Math.min(20,spec.limit),spec.product,cursor);
  for(const t of (r.items||[])) mergeTweet(t,spec);
  return {spec,cursor:r.next_cursor||undefined,count:(r.items||[]).length};
}

// Phase 1: breadth-first. Every query gets page 1 before any deep paging.
console.error('[search] phase1 queries='+queries.length+' concurrency='+SEARCH_CONCURRENCY);
const first=await mapLimit(queries,SEARCH_CONCURRENCY,async spec=>{
  if(Date.now()-started>SEARCH_BUDGET_MS) throw new Error('search budget exhausted');
  try {
    const r=await searchOne(spec,undefined);
    console.error('[search] p1 '+spec.product+' '+spec.query.slice(0,75)+' -> '+r.count);
    return r;
  } catch(e){
    console.error('[search] p1 failed '+spec.product+' '+spec.query.slice(0,60)+': '+(e?.message||e));
    throw e;
  }
});

// Phase 2: only Latest page 2, only while discovery still has budget.
const page2=[];
for(const r of first){
  if(r.status!=='fulfilled') continue;
  const v=r.value;
  if(v?.cursor && v.spec.product==='Latest' && v.spec.limit>20) page2.push(v);
}
if(Date.now()-started < SEARCH_BUDGET_MS*0.70 && rateLimited<2 && forbidden<2){
  console.error('[search] phase2 latest-page2='+page2.length);
  await mapLimit(page2,SEARCH_CONCURRENCY,async v=>{
    if(Date.now()-started>SEARCH_BUDGET_MS) return null;
    try {
      const r=await searchOne(v.spec,v.cursor);
      console.error('[search] p2 '+v.spec.query.slice(0,75)+' -> '+r.count);
      return r;
    } catch(e){
      console.error('[search] p2 failed: '+(e?.message||e));
      return null;
    }
  });
} else {
  console.error('[search] phase2 skipped (budget/rate-limit guard)');
}

// Due watchlist entries found by SearchTimeline already have fresh raw metrics.
// Only posts missing from search need TweetDetail.
const dueIds=new Set(watch.map(w=>String(w.post_id)));
const missingDue=[];
for(const w of watch){
  if(!w.post_id) continue;
  const id=String(w.post_id);
  if(seen.has(id)) continue;
  const p={
    post_id:id,author_handle:w.author_handle||'',
    url:w.url||('https://x.com/i/status/'+id),posted_at:w.posted_at,
    text_snippet:w.text_snippet||'',likes:0,retweets:0,replies:0,
    quotes:0,bookmarks:0,impressions:Number(w.last_impressions||0),
    discovery_source:'watchlist',discovery_sources:['watchlist'],discovery_query_hits:0
  };
  seen.set(id,p); missingDue.push(p);
}

function applyDetail(p,d){
  const ins=d?.data?.threaded_conversation_with_injections_v2?.instructions||[];
  for(const inst of ins) for(const e of inst.entries||[]){
    const res=unwrapResult(e?.content?.itemContent?.tweet_results?.result);
    if(String(res?.rest_id||res?.legacy?.id_str||'')!==String(p.post_id)) continue;
    const lg=res?.legacy||{};
    p.impressions=Number(res?.views?.count||p.impressions||0)||0;
    p.likes=Number(lg.favorite_count??p.likes??0)||0;
    p.retweets=Number(lg.retweet_count??p.retweets??0)||0;
    p.replies=Number(lg.reply_count??p.replies??0)||0;
    p.quotes=Number(lg.quote_count??p.quotes??0)||0;
    p.bookmarks=Number(lg.bookmark_count??p.bookmarks??0)||0;
    return true;
  }
  return false;
}
const detailStart=Date.now();
let detailOk=0;
console.error('[detail] missing due watchlist='+missingDue.length+' concurrency='+DETAIL_CONCURRENCY);
await mapLimit(missingDue,DETAIL_CONCURRENCY,async p=>{
  if(Date.now()-detailStart>DETAIL_BUDGET_MS) return null;
  try {
    const d=await x.getTweet(p.post_id);
    if(applyDetail(p,d)) detailOk++;
  } catch(e){
    console.error('[detail] '+p.post_id+' failed: '+(e?.message||e));
  }
  return null;
});

const all=[...seen.values()];
const withViews=all.filter(p=>(p.impressions||0)>0).length;
console.error('[collector] posts='+all.length+
  ' with_views='+withViews+
  ' raw_search_views='+searchTweetsWithViews+'/'+searchTweets+
  ' search_responses='+searchResponses+
  ' detail='+detailOk+'/'+missingDue.length+
  ' 429='+rateLimited+' 403='+forbidden+
  ' elapsed_ms='+(Date.now()-started));
process.stdout.write(JSON.stringify(all));
'''
    Path(".x-agent-collector.mjs").write_text(script, encoding="utf-8")
    try:
        r = subprocess.run(
            ["node", ".x-agent-collector.mjs"],
            text=True, capture_output=True, timeout=660,
            env={**os.environ, "X_SESSION_STATE_PATH": _session_path()},
        )
        if r.stderr:
            print(r.stderr, end="")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "x-agent collector failed")[-2000:]
            if "SESSION_EXPIRED" in msg:
                raise SessionExpiredError(msg)
            raise RuntimeError(msg)
        posts = json.loads(r.stdout or "[]")
        for post in posts:
            post["posted_at"] = _normalize_created_at(post.get("posted_at"))
        print(f"x-agent収集完了: {len(posts)}件")
        return posts
    finally:
        for p in (".x-agent-queries.json", ".x-agent-watchlist.json", ".x-agent-collector.mjs"):
            try: Path(p).unlink()
            except FileNotFoundError: pass
