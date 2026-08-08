"""
playwright_collector.py
ログイン済みセッション(storage_state.json)を使って、X検索結果から
投稿データ(いいね・引用・ブックマーク・返信数)を取得する。

★重要な注意(必ず読むこと):
  この実装はXの現在(2026年8月時点)の画面構造(DOM)を前提にして書かれています。
  Xは頻繁にHTML構造やクラス名を変更するため、実際に動かした時にセレクタが
  合わずデータが取れない可能性があります。その場合はエラーメッセージや
  「0件しか取れない」といった症状が出るので、教えてもらえれば調整します。
  これは一度作って終わりではなく、育てていくタイプのコードです。

事前準備:
  pip install playwright
  playwright install chromium
  環境変数 X_SESSION_STATE_PATH に storage_state.json のパスを設定
  (GitHub Actionsでは、Secretから復元したファイルのパスを渡す)
"""

import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

from playwright.sync_api import sync_playwright

import detector

# 監視対象の検索クエリ。ジャンルを横断して拾いたいので、
# 特定ジャンルに偏らない広めの条件にしてある。
# Xの検索演算子(min_faves, min_replies等)はログイン状態でも通常通り使える。
SEARCH_QUERIES = [
    "lang:ja min_faves:500 min_replies:20 -filter:retweets",
]

MAX_SCROLLS = 5           # 検索結果を何回スクロールして読み込むか(増やすほど件数は増えるが時間もかかる)
SCROLL_WAIT_SECONDS = 2.0  # スクロール後の読み込み待機時間


def _session_path() -> str:
    path = os.environ.get("X_SESSION_STATE_PATH", "storage_state.json")
    if not os.path.exists(path):
        raise RuntimeError(
            f"セッションファイルが見つかりません: {path}\n"
            "login_helper.py を実行してログイン状態を作成し、"
            "GitHub Secretsの X_SESSION_STATE に登録してください。"
        )
    return path


def _parse_count(text: str) -> int:
    """
    Xの表示上の数値("1.2万", "3,500", "12" など)を整数に変換する。
    Xの表示形式は変わることがあるので、複数パターンに対応。
    """
    if not text:
        return 0
    text = text.strip().replace(",", "")
    if not text:
        return 0

    multiplier = 1
    if text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith("億"):
        multiplier = 100_000_000
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    elif text.upper().endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]

    try:
        value = float(text)
        return int(value * multiplier)
    except ValueError:
        return 0


def _extract_tweet_id(url: str) -> str:
    match = re.search(r"/status/(\d+)", url or "")
    return match.group(1) if match else ""


def _extract_tweet_data(article) -> dict | None:
    """
    検索結果ページの1投稿分(<article>要素)から必要なデータを抜き出す。
    ★DOM構造はXの仕様変更で壊れやすい箇所。動かなければここを調整する。
    """
    try:
        # 投稿URL・IDの取得(時刻リンクの href から辿る)
        time_el = article.query_selector("time")
        if not time_el:
            return None
        link_el = time_el.evaluate_handle("el => el.closest('a')")
        href = link_el.as_element().get_attribute("href") if link_el else None
        if not href:
            return None
        post_id = _extract_tweet_id(href)
        if not post_id:
            return None
        url = f"https://x.com{href}" if href.startswith("/") else href

        posted_at_raw = time_el.get_attribute("datetime")  # ISO8601形式で取得できる

        # 投稿者ハンドル
        author_handle = ""
        user_link = article.query_selector('a[role="link"][href^="/"]')
        if user_link:
            href_user = user_link.get_attribute("href") or ""
            author_handle = href_user.strip("/").split("/")[0]

        # 本文
        text_el = article.query_selector('[data-testid="tweetText"]')
        text_snippet = text_el.inner_text()[:200] if text_el else ""

        # エンゲージメント数値(返信・RT・いいね)
        # 標準的なアクションバーのボタンには data-testid が振られており、
        # aria-label に「返信 12件」のような形式で件数が入っている
        def _count_by_testid(testid: str) -> int:
            el = article.query_selector(f'[data-testid="{testid}"]')
            if not el:
                return 0
            label = el.get_attribute("aria-label") or el.inner_text()
            m = re.search(r"[\d,\.]+[万億KkMm]?", label)
            return _parse_count(m.group(0)) if m else 0

        replies = _count_by_testid("reply")
        retweets = _count_by_testid("retweet")
        likes = _count_by_testid("like")

        # 引用・ブックマーク・表示回数(インプレッション)は、検索結果一覧の
        # カードには表示されないことが実際の運用で確認された。
        # これらは _fetch_detail_stats() で個別の投稿ページから別途取得し、
        # fetch_posts() 内で上書きする。ここではひとまず0を入れておく。
        quotes = 0
        bookmarks = 0

        return {
            "post_id": post_id,
            "author_handle": author_handle,
            "url": url,
            "posted_at": posted_at_raw or datetime.now(timezone.utc).isoformat(),
            "text_snippet": text_snippet,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "quotes": quotes,
            "bookmarks": bookmarks,
        }
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 投稿1件の解析に失敗: {e}")
        return None


class SessionExpiredError(Exception):
    """ログインセッションが切れて、ログイン画面に飛ばされた場合の専用エラー"""
    pass


def _fetch_detail_stats(page, url: str) -> dict:
    """
    投稿の個別ページ(詳細ページ)を開き、引用・ブックマーク・表示回数
    (インプレッション)を取得する。検索結果一覧には出てこない情報なので、
    候補に絞ってからここで1件ずつ取得する。
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)
        full_text = page.inner_text("body")
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 詳細ページの取得に失敗 {url}: {e}")
        return {"quotes": 0, "bookmarks": 0, "impressions": 0}

    def _find(patterns) -> int:
        for pattern in patterns:
            m = re.search(pattern, full_text, re.IGNORECASE)
            if m:
                return _parse_count(m.group(1))
        return 0

    quotes = _find([
        r"([\d,\.]+[万億KkMm]?)\s*件の引用",
        r"([\d,\.]+[万億KkMm]?)\s*Quotes?",
    ])
    bookmarks = _find([
        r"([\d,\.]+[万億KkMm]?)\s*件のブックマーク",
        r"([\d,\.]+[万億KkMm]?)\s*Bookmarks?",
    ])
    impressions = _find([
        r"([\d,\.]+[万億KkMm]?)\s*件の表示",
        r"([\d,\.]+[万億KkMm]?)\s*Views?",
    ])

    return {"quotes": quotes, "bookmarks": bookmarks, "impressions": impressions}


def _search_one_query(page, query: str) -> list[dict]:
    search_url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"
    print(f"  検索実行: {query}")
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    # セッション切れの検知: ログイン画面にリダイレクトされていないか確認
    current_url = page.url
    if "/login" in current_url or "/i/flow/login" in current_url:
        raise SessionExpiredError(
            "Xのログインセッションが切れているようです(ログイン画面にリダイレクトされました)。"
            "codespace_login.sh を再実行してセッションを更新してください。"
        )

    posts_by_id = {}
    for _ in range(MAX_SCROLLS):
        articles = page.query_selector_all('article[data-testid="tweet"]')
        for article in articles:
            data = _extract_tweet_data(article)
            if data:
                posts_by_id[data["post_id"]] = data

        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(int(SCROLL_WAIT_SECONDS * 1000))

    return list(posts_by_id.values())


def fetch_posts() -> list[dict]:
    """
    全ての検索クエリを実行し、正規化済みの投稿リストを返す。

    2段階方式:
      1段階目(検索): 広く投稿を集める。いいね・RT・返信は取れるが、
                     引用・ブックマーク・表示回数はここでは取れない。
      2段階目(詳細ページ): 1段階目のうち「いいねが閾値以上、かつ
                     投稿から時間内」の候補だけ、個別ページを開いて
                     引用・ブックマーク・表示回数を取得する。
    """
    session_path = _session_path()
    all_posts = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session_path)
        page = context.new_page()

        for query in SEARCH_QUERIES:
            try:
                posts = _search_one_query(page, query)
                for post in posts:
                    all_posts[post["post_id"]] = post
                print(f"  → {len(posts)}件取得(累計{len(all_posts)}件)")
            except SessionExpiredError:
                browser.close()
                raise  # セッション切れは main.py 側で専用処理するため、そのまま伝播させる
            except Exception as e:  # noqa: BLE001
                print(f"  [ERROR] 検索クエリ失敗 '{query}': {e}")

        # 2段階目: いいねが閾値以上、かつ投稿から時間内の候補だけ詳細ページを見る
        candidates = [
            p for p in all_posts.values()
            if p.get("likes", 0) >= detector.LIKE_THRESHOLD
            and detector.elapsed_hours(p["posted_at"]) <= detector.THRESHOLD_HOURS
        ]
        print(f"  [2段階目] 詳細ページ取得の対象: {len(candidates)}件")

        for post in candidates:
            stats = _fetch_detail_stats(page, post["url"])
            post["quotes"] = stats["quotes"]
            post["bookmarks"] = stats["bookmarks"]
            post["impressions"] = stats["impressions"]

        browser.close()

    result = list(all_posts.values())

    # ★診断用ログ: いいね数上位5件の全指標を出力する。
    # 「引用・ブックマークがいつも0」になっていないかをここで確認できる。
    # (Xの画面構造の変化でセレクタが合わなくなった時の切り分けに使う)
    top5 = sorted(result, key=lambda p: p.get("likes", 0), reverse=True)[:5]
    print("\n--- 診断ログ: いいね数上位5件の取得結果 ---")
    for p in top5:
        print(
            f"  [{p['post_id']}] {p['author_handle']}: "
            f"likes={p['likes']}, quotes={p['quotes']}, "
            f"bookmarks={p['bookmarks']}, retweets={p['retweets']}, "
            f"replies={p['replies']}, posted_at={p['posted_at']}"
        )
    print("--- 診断ログここまで ---\n")

    return result


if __name__ == "__main__":
    results = fetch_posts()
    print(f"\n合計 {len(results)} 件取得")
    for p in results[:5]:
        print(p)
