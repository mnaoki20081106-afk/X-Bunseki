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

        # 投稿全体のテキスト(ボタンのaria-labelを含む)を1回だけ取得し、
        # ブックマーク・引用の両方をここから探す。
        # 理由: ブックマーク数は data-testid="bookmark" ボタンに乗っている
        # ことが多いが、引用数は専用ボタンが無く「142件の引用」のような
        # テキストリンクとして、アクションバーの外(投稿の別の場所)に
        # 表示されることがある。ボタン列(role="group")の中だけを見ていると
        # これを取りこぼすため、記事全体を対象に正規表現で探す。
        full_text = article.inner_text()

        # ブックマーク: 専用ボタンを優先し、無ければ全体テキストからも探す
        bookmarks = _count_by_testid("bookmark")
        if bookmarks == 0:
            m = re.search(
                r"([\d,\.]+[万億KkMm]?)\s*(?:件のブックマーク|ブックマーク|bookmarks?)",
                full_text,
                re.IGNORECASE,
            )
            if m:
                bookmarks = _parse_count(m.group(1))

        # 引用: 専用ボタンが無いことが多いので、全体テキストから
        # 「◯件の引用」「◯ Quotes」のようなパターンを探す
        quotes = 0
        for pattern in (
            r"([\d,\.]+[万億KkMm]?)\s*件の引用",
            r"([\d,\.]+[万億KkMm]?)\s*Quotes?",
            r"引用[\s\u3000]*([\d,\.]+[万億KkMm]?)",
        ):
            m = re.search(pattern, full_text, re.IGNORECASE)
            if m:
                quotes = _parse_count(m.group(1))
                break

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
