"""
playwright_collector.py — 2026-09 diagnostic rebuild
目標: まず1件でも投稿を取る。失敗理由をログに出す。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import keyword_filter


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v.strip() != "" else default


SEARCH_MIN_FAVES = int(_env("SEARCH_MIN_FAVES", "20"))
COMBO_MIN_FAVES = int(_env("COMBO_MIN_FAVES", "15"))
BROAD_MIN_FAVES = int(_env("BROAD_MIN_FAVES", "500"))
SCROLLS = int(_env("SCROLLS_KEYWORD", "3"))
SCROLL_WAIT = float(_env("SCROLL_WAIT_SECONDS", "2.0"))
COLLECT_MAX_AGE_MINUTES = float(_env("COLLECT_MAX_AGE_MINUTES", "360"))
SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "off")
if SEARCH_WITHIN_TIME.lower() in ("off", "none", "no", "0", "-"):
    SEARCH_WITHIN_TIME = ""


class SessionExpiredError(Exception):
    pass


def _session_path() -> str:
    path = os.environ.get("X_SESSION_STATE_PATH", "storage_state.json")
    if not os.path.exists(path):
        raise RuntimeError(f"セッションファイルが見つかりません: {path}")
    return path


def _parse_count(text: str) -> int:
    if not text:
        return 0
    text = str(text).strip().replace(",", "").replace("，", "")
    mult = 1
    if text.endswith("万"):
        mult, text = 10_000, text[:-1]
    elif text.endswith("億"):
        mult, text = 100_000_000, text[:-1]
    elif text.upper().endswith("K"):
        mult, text = 1_000, text[:-1]
    elif text.upper().endswith("M"):
        mult, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return 0


_LABEL_PATTERNS = {
    "replies": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件の返信|返信|repl(?:y|ies))",
    "retweets": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件のリポスト|リポスト|件のリツイート|リツイート|repost|retweet)",
    "likes": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件のいいね|いいね|like)",
    "bookmarks": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件のブックマーク|ブックマーク|bookmark)",
    "impressions": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件の表示|表示|view)",
}


def _parse_labeled_counts(label: str) -> dict:
    result = {}
    if not label:
        return result
    for field, pattern in _LABEL_PATTERNS.items():
        m = re.search(pattern, label, re.IGNORECASE)
        if m:
            result[field] = _parse_count(m.group(1).replace(" ", ""))
    return result


def _extract_tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else ""


def _query_articles(page):
    for sel in (
        'article[data-testid="tweet"]',
        'article[role="article"]',
        '[data-testid="cellInnerDiv"] article',
    ):
        try:
            arts = page.query_selector_all(sel)
            if arts:
                return arts
        except Exception:
            continue
    return []


def _counts_from_card(article) -> dict:
    counts = {}
    try:
        group = article.query_selector('[role="group"][aria-label]')
        if group:
            counts.update(_parse_labeled_counts(group.get_attribute("aria-label") or ""))
    except Exception:
        pass
    for field, testid in (("replies", "reply"), ("retweets", "retweet"), ("likes", "like")):
        if counts.get(field):
            continue
        try:
            el = article.query_selector(f'[data-testid="{testid}"]')
            if not el:
                continue
            label = el.get_attribute("aria-label") or el.inner_text() or ""
            parsed = _parse_labeled_counts(label)
            if field in parsed:
                counts[field] = parsed[field]
            else:
                m = re.search(r"[\d,\.]+[万億千KkMm]?", label)
                if m:
                    counts[field] = _parse_count(m.group(0))
        except Exception:
            continue
    return counts


def _extract_tweet_data(article) -> dict | None:
    try:
        time_el = article.query_selector("time")
        if not time_el:
            return None
        href = time_el.evaluate(
            "el => el.closest('a') ? el.closest('a').getAttribute('href') : ''"
        )
        if not href:
            return None
        post_id = _extract_tweet_id(href)
        if not post_id:
            return None
        url = f"https://x.com{href}" if href.startswith("/") else href
        posted_at = time_el.get_attribute("datetime") or datetime.now(timezone.utc).isoformat()

        author = ""
        user_link = article.query_selector('[data-testid="User-Name"] a[href^="/"]')
        if user_link:
            author = (user_link.get_attribute("href") or "").strip("/").split("/")[0]
        if not author:
            author = href.strip("/").split("/")[0]

        text_el = article.query_selector('[data-testid="tweetText"]')
        text_snippet = text_el.inner_text()[:280] if text_el else ""

        post = {
            "post_id": post_id,
            "author_handle": author,
            "url": url,
            "posted_at": posted_at,
            "text_snippet": text_snippet,
            "likes": 0,
            "retweets": 0,
            "replies": 0,
            "quotes": 0,
            "bookmarks": 0,
            "impressions": 0,
        }
        post.update(_counts_from_card(article))
        return post
    except Exception as e:
        print(f"  [警告] 投稿解析失敗: {e}")
        return None


def _check_session(page) -> None:
    print("  [セッション確認] https://x.com/home を開きます…")
    page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(4500)
    url = page.url
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    print(f"  [セッション確認] URL={url} title={title[:50]!r}")

    if "/login" in url or "/i/flow/login" in url:
        raise SessionExpiredError(
            "ホームがログイン画面にリダイレクト → セッション切れ。"
            "codespace_login.sh で X_SESSION_STATE を更新してください。"
        )

    body = ""
    try:
        body = (page.inner_text("body") or "")[:400]
    except Exception:
        pass
    if "パスワード" in body and ("ログイン" in body or "Log in" in body):
        raise SessionExpiredError("ログイン画面の文言を検出 → セッション切れの可能性大。")


def _search_one(page, query: str, mode: str, scrolls: int) -> list[dict]:
    f_param = "&f=live" if mode == "live" else ""
    search_url = f"https://x.com/search?q={quote(query)}&src=typed_query{f_param}"
    print(f"  検索[{mode}]: {query[:80]}{'...' if len(query) > 80 else ''}")
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    url = page.url
    print(f"    到達URL={url[:100]}")
    if "/login" in url or "/i/flow/login" in url:
        raise SessionExpiredError("検索がログイン画面にリダイレクト → セッション切れ。")

    try:
        page.wait_for_selector(
            'article[data-testid="tweet"], article[role="article"]',
            timeout=12000,
        )
    except Exception:
        arts0 = _query_articles(page)
        print(f"    (カード待ちタイムアウト / 検出={len(arts0)})")
        for sel in (
            'article[data-testid="tweet"]',
            'article[role="article"]',
            '[data-testid="cellInnerDiv"]',
            "article",
        ):
            try:
                print(f"      {sel}: {len(page.query_selector_all(sel))}")
            except Exception:
                pass
        try:
            snip = (page.content() or "")[:400]
            print(f"    [HTML断片] {snip[:200]!r}")
        except Exception:
            pass

    posts = {}
    for _ in range(scrolls):
        for article in _query_articles(page):
            data = _extract_tweet_data(article)
            if data and data["post_id"] not in posts:
                posts[data["post_id"]] = data
        page.mouse.wheel(0, 3500)
        page.wait_for_timeout(int(SCROLL_WAIT * 1000))

    print(f"    → {len(posts)}件")
    return list(posts.values())


def build_queries() -> list[tuple[str, str, int]]:
    queries: list[tuple[str, str, int]] = []
    smoke = "lang:ja -filter:retweets min_faves:50"
    if SEARCH_WITHIN_TIME:
        smoke = f"{smoke} within_time:{SEARCH_WITHIN_TIME}"
    queries.append((smoke, "live", 2))

    for group in keyword_filter.query_groups():
        q = f"({group}) lang:ja -filter:retweets min_faves:{SEARCH_MIN_FAVES}"
        if SEARCH_WITHIN_TIME:
            q = f"{q} within_time:{SEARCH_WITHIN_TIME}"
        queries.append((q, "live", SCROLLS))

    for expression in keyword_filter.combo_queries():
        q = f"{expression} lang:ja -filter:retweets min_faves:{COMBO_MIN_FAVES}"
        if SEARCH_WITHIN_TIME:
            q = f"{q} within_time:{SEARCH_WITHIN_TIME}"
        queries.append((q, "live", 2))

    broad = f"lang:ja -filter:retweets -filter:replies min_faves:{BROAD_MIN_FAVES}"
    if SEARCH_WITHIN_TIME:
        broad = f"{broad} within_time:{SEARCH_WITHIN_TIME}"
    queries.append((broad, "live", 3))
    queries.append((broad, "top", 2))
    return queries


def fetch_posts() -> list[dict]:
    from playwright.sync_api import sync_playwright

    session_path = _session_path()
    all_posts: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=session_path,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        _check_session(page)

        queries = build_queries()
        print(f"検索クエリ数: {len(queries)} (SEARCH_WITHIN_TIME={SEARCH_WITHIN_TIME or 'off'})")

        for query, mode, scrolls in queries:
            try:
                posts = _search_one(page, query, mode, scrolls)
                for post in posts:
                    if post["post_id"] not in all_posts:
                        all_posts[post["post_id"]] = post
                    else:
                        for f in ("likes", "retweets", "replies", "bookmarks", "impressions"):
                            if not all_posts[post["post_id"]].get(f) and post.get(f):
                                all_posts[post["post_id"]][f] = post[f]
            except SessionExpiredError:
                browser.close()
                raise
            except Exception as e:
                print(f"  [ERROR] 検索失敗 [{mode}]: {e}")

        browser.close()

    result = list(all_posts.values())
    top = sorted(result, key=lambda p: p.get("likes") or 0, reverse=True)[:5]
    print("\n--- 診断: いいね上位 ---")
    for p in top:
        print(
            f"  [{p['post_id']}] @{p['author_handle']}: "
            f"likes={p.get('likes')} rt={p.get('retweets')} reply={p.get('replies')}"
        )
    if not result:
        print("  [重要] 0件。想定原因:")
        print("    1. X_SESSION_STATE が無効 → codespace_login.sh で再ログイン")
        print("    2. Xがヘッドレスを弾いている / レート制限")
        print("    3. セレクタ変更（ログのHTML断片・article数を確認）")
    print(f"--- 合計 {len(result)} 件 ---\n")
    return result


if __name__ == "__main__":
    for q, m, s in build_queries():
        print(f"[{m}] scrolls={s} :: {q[:100]}")
