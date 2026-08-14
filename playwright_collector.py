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


def _count_quote_tweets(page, main_article, base_url: str) -> int:
    """
    「View quotes」リンクを実際に開き、引用投稿の一覧ページを表示して、
    そこに並んでいる投稿の件数を数える。

    ★注意: Xは他人の投稿の「引用数」を正確な数字として公開していない
    (投稿者本人のアナリティクス機能でしか正確な数は見れない)。
    そのため、この関数は「実際に一覧に表示されている引用投稿を
    数える」という近似的な方法を取っている。スクロールした範囲でしか
    数えられないため、本当の総数より少なく出ることがある(下限値に近い)。
    それでも「0のまま」より遥かに有用な情報になる。
    """
    try:
        quote_link = main_article.query_selector('a[href*="quotes" i], a[href*="Quotes" i]')
        if not quote_link:
            all_links = main_article.query_selector_all("a")
            for link in all_links:
                text = (link.inner_text() or "").lower()
                aria = (link.get_attribute("aria-label") or "").lower()
                if "quote" in text or "quote" in aria or "引用" in text:
                    quote_link = link
                    break

        if not quote_link:
            return 0

        href = quote_link.get_attribute("href")
        if not href:
            return 0
        quote_url = f"https://x.com{href}" if href.startswith("/") else href

        page.goto(quote_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        seen_ids = set()
        for _ in range(3):
            articles = page.query_selector_all('article[data-testid="tweet"]')
            for a in articles:
                tid = a.get_attribute("aria-labelledby") or id(a)
                seen_ids.add(tid)
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        return len(seen_ids)
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 引用一覧の取得に失敗 {base_url}: {e}")
        return 0


def _fetch_detail_stats(page, url: str) -> dict:
    """
    投稿の個別ページ(詳細ページ)を開き、引用・ブックマーク・表示回数
    (インプレッション)を取得する。検索結果一覧には出てこない情報なので、
    候補に絞ってからここで1件ずつ取得する。
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        main_article = page.query_selector('article[data-testid="tweet"]')
        if not main_article:
            print(f"  [警告] 詳細ページで投稿本体が見つからない {url}")
            return {"quotes": 0, "bookmarks": 0, "impressions": 0}

        scoped_text = main_article.inner_text()
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 詳細ページの取得に失敗 {url}: {e}")
        return {"quotes": 0, "bookmarks": 0, "impressions": 0}

    views_match = re.search(r"([\d,\.]+[万億KkMm]?)\s*Views?", scoped_text, re.IGNORECASE)
    impressions = _parse_count(views_match.group(1)) if views_match else 0

    bookmarks = 0
    if views_match:
        after = scoped_text[views_match.end():]
        cutoff = after.find("Relevant")
        if cutoff != -1:
            after = after[:cutoff]

        numbers = re.findall(r"[\d,\.]+[万億KkMm]?", after)
        parsed = [_parse_count(n) for n in numbers]

        if len(parsed) >= 4:
            bookmarks = parsed[3]
        else:
            print(f"      [デバッグ] Views以降の数字が4つ未満: {parsed} (url={url})")

    quotes = _count_quote_tweets(page, main_article, url)

    if bookmarks == 0:
        tail = scoped_text[-300:].replace("\n", " | ")
        print(f"      [デバッグ] ブックマーク0。投稿本体テキスト末尾300字: {tail}")

    return {"quotes": quotes, "bookmarks": bookmarks, "impressions": impressions}


def _search_one_query(page, query: str) -> list[dict]:
    search_url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"
    print(f"  検索実行: {query}")
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

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
      1段階目(検索): 広く投稿を集める。「時系列順(Latest)」と「話題性順(Top)」の
                     両方で検索することで、新着の投稿だけでなく、じわじわ伸びて
                     いる投稿(ニュース・災害など)も拾えるようにしている。
                     いいね・RT・返信は取れるが、引用・ブックマーク・表示回数は
                     ここでは取れない。
      2段階目(詳細ページ): 1段階目のうち「瞬間バズ候補または持続バズ候補」に
                     該当する投稿だけ、個別ページを開いて
                     引用・ブックマーク・表示回数を取得する。
    """
    session_path = _session_path()
    all_posts = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session_path)
        page = context.new_page()

        for query in SEARCH_QUERIES:
            for mode in ("live", "top"):
                try:
                    posts = _search_one_query(page, query, mode=mode)
                    for post in posts:
                        all_posts[post["post_id"]] = post
                    print(f"  → {len(posts)}件取得(累計{len(all_posts)}件)")
                except SessionExpiredError:
                    browser.close()
                    raise  # セッション切れは main.py 側で専用処理するため、そのまま伝播させる
                except Exception as e:  # noqa: BLE001
                    print(f"  [ERROR] 検索クエリ失敗 [{mode}] '{query}': {e}")

        # 2段階目: 「瞬間バズ候補(いいね多め・2時間以内)」または
        # 「持続バズ候補(一定のいいねがあり・8時間以内)」のどちらかに
        # 該当する投稿だけ詳細ページを見る。
        # 事前フィルタなので、v3の絶対閾値のうち緩い方(瞬間バズのいいね
        # 閾値の半分程度)を目安に広めに候補を拾い、正確な判定は
        # detector.filter_explosive() 側(quotes/bookmarks/impressions
        # 取得後)で行う。
        PRELIMINARY_LIKE_THRESHOLD = detector.INSTANT_LIKE_THRESHOLD // 2  # 400
        candidates = [
            p for p in all_posts.values()
            if p.get("likes", 0) >= PRELIMINARY_LIKE_THRESHOLD
            and detector.elapsed_hours(p["posted_at"]) <= detector.SUSTAINED_MAX_HOURS
        ]
        print(f"  [2段階目] 詳細ページ取得の対象: {len(candidates)}件")

        for post in candidates:
            stats = _fetch_detail_stats(page, post["url"])
            post["quotes"] = stats["quotes"]
            post["bookmarks"] = stats["bookmarks"]
            post["impressions"] = stats["impressions"]
            print(
                f"    [詳細] {post['post_id']}: "
                f"quotes={stats['quotes']}, bookmarks={stats['bookmarks']}, "
                f"impressions={stats['impressions']}"
            )

        browser.close()

    result = list(all_posts.values())



if __name__ == "__main__":
    results = fetch_posts()
    print(f"\n合計 {len(results)} 件取得")
    for p in results[:5]:
        print(p)
