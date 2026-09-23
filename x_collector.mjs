import fs from 'node:fs';
import { XClient, QID } from 'x-agent-sdk';
import { CollectorError, createRequestGuard, parseTimeline, responseError } from './x_response.mjs';
import { discoverQueryIds, queryIdOverrides } from './x_query_ids.mjs';

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
// Resolve only the two read operations. Keep the pinned SDK IDs if discovery
// cannot run; an explicit configuration override wins over live discovery.
try {
  if(process.env.X_QUERY_ID_DISCOVERY !== 'off') Object.assign(QID,await discoverQueryIds());
} catch { console.error('[compat] live operation IDs unavailable; using pinned IDs'); }
Object.assign(QID,queryIdOverrides(process.env.X_GRAPHQL_QUERY_IDS));
const started=Date.now();
const guard=createRequestGuard();
const searchPages=new Map();
let searchSuccess=0, searchFailed=0, detailFailed=0, detailSkipped=0, invalidTweets=0;
function pageKey(query,product,cursor){ return JSON.stringify([query,product,cursor||'']); }
function safeError(e){ return e instanceof CollectorError ? e.code : 'request_failed'; }
let searchResponses=0, searchTweets=0, searchTweetsWithViews=0;
let rateLimited=0, forbidden=0;
const searchRateLimit={limit:null,remaining:null,reset:null};

function responseHeaderInt(res,name){
  const value=res.headers.get(name);
  if(value===null || value==='') return null;
  const parsed=Number(value);
  return Number.isFinite(parsed) && parsed>=0 ? Math.floor(parsed) : null;
}
function captureSearchRateLimit(operation,res){
  if(operation!=='SearchTimeline') return;
  const limit=responseHeaderInt(res,'x-rate-limit-limit');
  const remaining=responseHeaderInt(res,'x-rate-limit-remaining');
  const reset=responseHeaderInt(res,'x-rate-limit-reset');
  if(limit!==null) searchRateLimit.limit=limit;
  if(remaining!==null) searchRateLimit.remaining=remaining;
  if(reset!==null) searchRateLimit.reset=reset;
}

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
const trackedFetch=async (url,init={})=>{
  const parsedUrl=new URL(String(url));
  const operation=parsedUrl.pathname.endsWith('/SearchTimeline')?'SearchTimeline':'TweetDetail';
  guard.check(operation);
  const timeout=operation==='SearchTimeline'?SEARCH_TIMEOUT_MS:DETAIL_TIMEOUT_MS;
  const signal=init.signal ? AbortSignal.any([init.signal,AbortSignal.timeout(timeout)]) : AbortSignal.timeout(timeout);
  let res, body;
  try {
    res=await fetch(url,{...init,signal});
    try { body=await res.clone().json(); } catch {}
  } catch {
    guard.fail(operation,'network_error');
    throw new CollectorError('network_error');
  }
  captureSearchRateLimit(operation,res);
  if(res.status===429) rateLimited++;
  if(res.status===403) forbidden++;
  const error=responseError(res.status,body);
  if(error){ guard.fail(operation,error); throw new CollectorError(error); }
  let parsed;
  try { parsed=parseTimeline(body,operation); }
  catch { guard.fail(operation,'schema_changed'); throw new CollectorError('schema_changed'); }
  invalidTweets+=parsed.invalid;
  if(operation==='SearchTimeline' && parsed.candidates>0 && parsed.tweets.length===0){
    guard.fail(operation,'schema_changed'); throw new CollectorError('schema_changed');
  }
  if(operation==='SearchTimeline'){
    searchResponses++;
    searchTweets+=parsed.tweets.length;
    searchTweetsWithViews+=parsed.tweets.filter(t=>t.impressions>0).length;
    const vars=JSON.parse(parsedUrl.searchParams.get('variables')||'{}');
    searchPages.set(pageKey(vars.rawQuery,vars.product,vars.cursor),parsed.tweets);
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
function mergeTweet(tweet,spec){
  const id=tweet.post_id;
  const old=seen.get(id);
  const sources=new Set(old?.discovery_sources||[]);
  sources.add(spec.source||'keyword');
  seen.set(id,{
    ...tweet,
    discovery_sources:[...sources],
    discovery_source:sources.has('viral_search')?'viral_search':(old?.discovery_source||spec.source||'keyword'),
    discovery_query_hits:(old?.discovery_query_hits||0)+1
  });
}
async function searchOne(spec,cursor){
  guard.check('SearchTimeline');
  await sleep(100+Math.floor(Math.random()*301));
  guard.check('SearchTimeline');
  const r=await x.searchPage(spec.query,Math.min(20,spec.limit),spec.product,cursor);
  const key=pageKey(spec.query,spec.product,cursor);
  const tweets=searchPages.get(key);
  if(!tweets) throw new CollectorError('schema_changed');
  searchPages.delete(key);
  for(const tweet of tweets) mergeTweet(tweet,spec);
  searchSuccess++;
  return {spec,cursor:r.next_cursor||undefined,count:tweets.length};
}

// Phase 1: breadth-first. Every query gets page 1 before any deep paging.
console.error('[search] phase1 queries='+queries.length+' concurrency='+SEARCH_CONCURRENCY);
const first=await mapLimit(queries,SEARCH_CONCURRENCY,async spec=>{
  if(Date.now()-started>SEARCH_BUDGET_MS){ searchFailed++; throw new CollectorError('budget_exhausted'); }
  try {
    const r=await searchOne(spec,undefined);
    console.error('[search] p1 '+spec.product+' '+spec.query.slice(0,75)+' -> '+r.count);
    return r;
  } catch(e){
    searchFailed++;
    console.error('[search] p1 failed '+spec.product+' '+spec.query.slice(0,60)+': '+safeError(e));
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
// Deep paging has diminishing returns and the first live run hit one 429 at
// 51 SearchTimeline responses. Keep all viral Latest page-2 searches first,
// then use only a few spare slots for keyword searches.
page2.sort((a,b)=>(b.spec.source==='viral_search'?1:0)-(a.spec.source==='viral_search'?1:0));
page2.splice(8);
if(Date.now()-started < SEARCH_BUDGET_MS*0.70 && rateLimited<2 && forbidden<2){
  console.error('[search] phase2 latest-page2='+page2.length);
  await mapLimit(page2,SEARCH_CONCURRENCY,async v=>{
    if(Date.now()-started>SEARCH_BUDGET_MS){ searchFailed++; return null; }
    try {
      const r=await searchOne(v.spec,v.cursor);
      console.error('[search] p2 '+v.spec.query.slice(0,75)+' -> '+r.count);
      return r;
    } catch(e){
      searchFailed++;
      console.error('[search] p2 failed: '+safeError(e));
      return null;
    }
  });
} else {
  console.error('[search] phase2 skipped (budget/rate-limit guard)');
}

// Due watchlist entries found by SearchTimeline already have fresh raw metrics.
// Only posts missing from search need TweetDetail.
const missingDue=[];
for(const w of watch){
  if(!w.post_id) continue;
  const id=String(w.post_id);
  if(seen.has(id)) continue;
  const p={
    post_id:id,author_handle:w.author_handle||'',
    author_name:w.author_name||w.author_handle||'',
    url:w.url||('https://x.com/i/status/'+id),posted_at:w.posted_at,
    text_snippet:w.text_snippet||'',likes:0,retweets:0,replies:0,
    quotes:0,bookmarks:0,impressions:Number(w.last_impressions||0),
    discovery_source:'watchlist',discovery_sources:['watchlist'],discovery_query_hits:0
  };
  missingDue.push(p);
}

function applyDetail(p,d){
  const result=parseTimeline(d,'TweetDetail').tweets.find(t=>t.post_id===p.post_id);
  if(!result) return false;
  // Only confirmed fresh measurements can enter observations and learning.
  seen.set(p.post_id,{...p,...result});
  return true;
}
const detailStart=Date.now();
let detailOk=0;
console.error('[detail] missing due watchlist='+missingDue.length+' concurrency='+DETAIL_CONCURRENCY);
await mapLimit(missingDue,DETAIL_CONCURRENCY,async p=>{
  if(Date.now()-detailStart>DETAIL_BUDGET_MS){ detailFailed++; return null; }
  try {
    guard.check('TweetDetail');
    const d=await x.getTweet(p.post_id);
    if(applyDetail(p,d)) detailOk++;
    else detailSkipped++;
  } catch(e){
    detailFailed++;
    console.error('[detail] '+p.post_id+' failed: '+safeError(e));
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
  ' skipped='+detailSkipped+' failed='+detailFailed+
  ' 429='+rateLimited+' 403='+forbidden+
  ' elapsed_ms='+(Date.now()-started));
// A small number of individual posts can legitimately omit views/bookmarks or
// become unavailable. Skip those observations without declaring the whole
// collector unhealthy. Escalate only when the loss is material.
const detailSkipRatio=missingDue.length ? detailSkipped/missingDue.length : 0;
const materialDetailLoss=detailSkipped>=5 && detailSkipRatio>=0.25;
const hasFailure=searchFailed>0 || detailFailed>0 || materialDetailLoss;
const error=guard.primaryError ||
  (searchSuccess===0 ? 'collection_failed' : (materialDetailLoss?'incomplete_observations':null));
const health={
  status:all.length===0 && hasFailure ? (error||'collection_failed') : (hasFailure?'degraded':'success'),
  error_code:error, search_succeeded:searchSuccess, search_failed:searchFailed,
  detail_succeeded:detailOk, detail_failed:detailFailed, detail_skipped:detailSkipped,
  invalid_tweets:invalidTweets,
  search_rate_limit:searchRateLimit,
  failures:guard.failures
};
process.stdout.write(JSON.stringify({posts:all,health}));
