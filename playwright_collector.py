import json
import os
import subprocess
from pathlib import Path

import keyword_filter

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
    return q

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
    payload = [{"query": q, "product": product, "limit": limit} for q, product, limit in queries]
    Path(".x-agent-queries.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _ensure_x_agent()

    script = r'''
import fs from 'node:fs';
import { XClient } from 'x-agent-sdk';
const state=JSON.parse(fs.readFileSync(process.env.X_SESSION_STATE_PATH||'storage_state.json','utf8'));
const cookies=Object.fromEntries((state.cookies||[]).map(c=>[c.name,c.value]));
if(!cookies.auth_token || !cookies.ct0) throw new Error('SESSION_EXPIRED: auth_token/ct0 missing');
const queries=JSON.parse(fs.readFileSync('.x-agent-queries.json','utf8'));
const x=new XClient({authToken:cookies.auth_token,ct0:cookies.ct0,retries:2});
const seen=new Map();
for(const spec of queries){
  let cursor=undefined, fetched=0, pages=0;
  console.error('[x-agent] '+spec.product+' '+spec.query.slice(0,100));
  try {
    while(fetched < spec.limit && pages < 3){
      const r=await x.searchPage(spec.query, Math.min(20,spec.limit-fetched), spec.product, cursor);
      for(const t of (r.items||[])){
        if(!t.id) continue;
        seen.set(String(t.id), {
          post_id:String(t.id),
          author_handle:t.author||'',
          url:t.url||('https://x.com/i/status/'+t.id),
          posted_at:t.created_at||new Date().toISOString(),
          text_snippet:(t.text||'').slice(0,280),
          likes:Number(t.likes||0),
          retweets:Number(t.retweets||0),
          replies:Number(t.replies||0),
          quotes:0,
          bookmarks:0,
          impressions:0
        });
      }
      fetched += (r.items||[]).length;
      pages++;
      cursor=r.next_cursor||undefined;
      if(!cursor || !(r.items||[]).length) break;
    }
    console.error('[x-agent] -> '+fetched+'件 / 累計'+seen.size+'件');
  } catch(e) {
    console.error('[x-agent] query failed: '+(e?.message||e));
  }
}
process.stdout.write(JSON.stringify([...seen.values()]));
'''
    Path(".x-agent-collector.mjs").write_text(script, encoding="utf-8")
    try:
        r = subprocess.run(
            ["node", ".x-agent-collector.mjs"],
            text=True, capture_output=True, timeout=240,
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
        print(f"x-agent収集完了: {len(posts)}件")
        return posts
    finally:
        for p in (".x-agent-queries.json", ".x-agent-collector.mjs"):
            try: Path(p).unlink()
            except FileNotFoundError: pass
