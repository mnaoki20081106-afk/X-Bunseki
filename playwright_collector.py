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


class CollectionError(Exception):
    def __init__(self, health):
        self.health = health
        super().__init__(health.get("error_code") or health.get("status") or "collection_failed")


class CollectedPosts(list):
    """Preserve the list API while attaching non-sensitive collection health."""
    def __init__(self, posts, health):
        super().__init__(posts)
        self.health = health


def decode_collection(output):
    try:
        result = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        raise CollectionError({"status": "error", "error_code": "invalid_collector_output"}) from None
    if not isinstance(result, dict) or not isinstance(result.get("posts"), list) or not isinstance(result.get("health"), dict):
        raise CollectionError({"status": "error", "error_code": "invalid_collector_output"})
    health = result["health"]
    if health.get("status") not in {"success", "degraded"}:
        if health.get("status") == "session_expired":
            raise SessionExpiredError("Xの認証が失効しました。X_SESSION_STATEを更新してください")
        raise CollectionError(health)
    return CollectedPosts(result["posts"], health)


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
    subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=Path(__file__).parent, check=True)

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

    try:
        r = subprocess.run(
            ["node", str(Path(__file__).with_name("x_collector.mjs"))],
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
        posts = decode_collection(r.stdout)
        skipped_detail_ids = posts.health.pop("_detail_skipped_ids", [])
        if skipped_detail_ids:
            try:
                watchlist.mark_detail_skipped(skipped_detail_ids)
                print(f"watchlist詳細欠損を次のチェックポイントへ進めました: {len(skipped_detail_ids)}件")
            except Exception as e:
                print(f"[WARN] watchlist詳細欠損の再スケジュールに失敗: {e}")
        for post in posts:
            post["posted_at"] = _normalize_created_at(post.get("posted_at"))
        print(f"x-agent収集完了: {len(posts)}件")
        return posts
    finally:
        for p in (".x-agent-queries.json", ".x-agent-watchlist.json"):
            try: Path(p).unlink()
            except FileNotFoundError: pass
