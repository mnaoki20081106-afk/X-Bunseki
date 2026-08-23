"""
playwright_collector.py (v4)
ログイン済みセッション(storage_state.json)を使って、X検索結果から
投稿データを取得する。

════════════════════════════════════════════════════════════
 v4での主な変更
════════════════════════════════════════════════════════════

【1】検索クエリをキーワードから自動生成し、min_faves を大幅に下げた

  v3: "lang:ja min_faves:500 min_replies:20 -filter:retweets" の1本だけ。
      → 毎回55件前後しか集まらず、キーワードに合致するのは1〜2件(39%の実行で0件)。
        しかも「すでに500いいね付いた投稿」しか入口を通れないため、
        早期発見が原理的に不可能だった。

  v4: keywords.txt を "(地震 OR 台風 OR 速報 OR ...)" というOR句に変換し、
      Xのサーバー側でジャンルを絞らせる。絞れているぶん min_faves を
      50まで下げられるので、「まだ小さいが伸びている投稿」が入口を通れる。

【2】検索結果カードから、ブックマーク・表示回数も取れるようにした

  v3は詳細ページを1件ずつ開いて取っていた(1件あたり約8秒)。
  現在のXは、投稿カードのアクションバー([role="group"])の aria-label に
  「12件の返信、34件のリポスト、56件のいいね、78件のブックマーク、9千件の表示」
  という形式で全指標をまとめて持っている。まずこれを読む。
  読めなかったものだけ、従来どおり詳細ページにフォールバックする。

【3】引用数のスクレイピングを既定で停止した

  v3の _count_quote_tweets() は「引用一覧ページを開いて3回スクロールして
  表示された件数を数える」という実装で、1件あたり約8秒かかるうえ、
  得られるのは画面に載った分だけの下限値でしかなかった。
  費用対効果が見合わないので既定オフ(FETCH_QUOTES=true で復活可能)。

【4】明らかにおかしい数値を弾くサニティチェックを入れた

  実行履歴を見ると likes=8208 / bookmarks=8800、likes=5048 / bookmarks=5000
  のように「ブックマークがいいねを超える」データが記録されていた。
  実際にはまず起きない事象で、v3の「Views以降の4番目の数字＝ブックマーク」
  という位置頼みのパースが崩れていたことを示している。
  誤った大きな値は判定を歪めるので、破棄して0扱いにする。

★注意: この実装はXの画面構造(DOM)に依存します。Xの仕様変更で
  取得できなくなることがあります。0件が続いたら通知が飛ぶようにしてあります。
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import keyword_filter

# playwright は実際にブラウザを起動する fetch_posts() の中でのみ import する。
# こうしておくと、検索クエリの確認やパーサの単体テストを
# playwright 未インストールの環境でも実行できる。


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value.strip() != "" else default


# ── 検索の設定(環境変数で調整可能) ────────────────────────
# ジャンル特化クエリの最低いいね数。v3の500から大幅に下げた。
# 「バズる前」を捉えるには、入口の閾値を低くするしかない。
SEARCH_MIN_FAVES = int(_env("SEARCH_MIN_FAVES", "30"))

# キーワードに引っかからない話題も一応拾うための広域クエリ用。
# こちらはノイズが多いので閾値を高めに保つ。
BROAD_MIN_FAVES = int(_env("BROAD_MIN_FAVES", "800"))

# 組み合わせ検索(keywords_combo.txt)の最低いいね数。
# AND条件で既に強く絞れているので、OR検索よりさらに低くできる。
COMBO_MIN_FAVES = int(_env("COMBO_MIN_FAVES", "20"))

# 各クエリのスクロール回数(多いほど件数は増えるが実行時間も伸びる)
SCROLLS_KEYWORD = int(_env("SCROLLS_KEYWORD", "2"))
SCROLLS_COMBO = int(_env("SCROLLS_COMBO", "2"))
SCROLLS_BROAD = int(_env("SCROLLS_BROAD", "5"))
SCROLL_WAIT_SECONDS = float(_env("SCROLL_WAIT_SECONDS", "1.8"))

# 詳細ページを開く上限件数(1件あたり2〜3秒かかるため上限を設ける)
MAX_DETAIL_FETCH = int(_env("MAX_DETAIL_FETCH", "12"))

# 引用数のスクレイピング(コストに見合わないため既定オフ)
FETCH_QUOTES = _env("FETCH_QUOTES", "false").lower() == "true"

# 収集対象とする投稿の最大経過分(これより古い投稿は詳細を取りに行かない)
COLLECT_MAX_AGE_MINUTES = float(_env("COLLECT_MAX_AGE_MINUTES", "240"))

# ★X検索側で「新しい投稿だけ」に絞り込む指定(2026-08-24に追加)。
#
# これまでは全件取得してから投稿時刻で後から捨てていたため、
# スクロールして読み込んだ投稿の大半が「古すぎて対象外」で無駄になっていた。
# within_time を付けるとXのサーバー側で絞ってくれるので、
# 同じスクロール回数でも取れる「新しい投稿」の数が大きく増える。
#
# detector.MAX_AGE_MINUTES(180分)に合わせて3時間にしてある。
# 空文字にすればこの絞り込みを無効化できる。
# 無効化したいときは "off" を設定する。
# (GitHubのVariablesは空文字を設定しても未設定と区別できないため、
#  無効化の意思をはっきり示せる合言葉を用意している)
SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "3h")
if SEARCH_WITHIN_TIME.lower() in ("off", "none", "no", "0", "-"):
    SEARCH_WITHIN_TIME = ""

# 広域クエリから除外するワード。
# 実測すると、広域クエリの上位はアイドル・ゲーム公式アカウントの
# 誕生日祝い・新譜告知・キャンペーンでほぼ埋まっており、
# 「これから伸びる一般投稿」がまったく拾えていなかった。
# エンゲージメントは非常に高いが、動画のネタにはならない類型なので除外する。
BROAD_EXCLUDE = _env("BROAD_EXCLUDE", "誕生日 生誕祭 発売記念 キャンペーン 先行配信").split()


class SessionExpiredError(Exception):
    """ログインセッションが切れて、ログイン画面に飛ばされた場合の専用エラー"""
    pass


def _session_path() -> str:
    path = os.environ.get("X_SESSION_STATE_PATH", "storage_state.json")
    if not os.path.exists(path):
        raise RuntimeError(
            f"セッションファイルが見つかりません: {path}\n"
            "login_helper.py を実行してログイン状態を作成し、"
            "GitHub Secretsの X_SESSION_STATE に登録してください。"
        )
    return path


# ──────────────────────────────────────────────────────────
#  数値パース
# ──────────────────────────────────────────────────────────

def _parse_count(text: str) -> int:
    """Xの表示上の数値("1.2万", "3,500", "12K" など)を整数に変換する"""
    if not text:
        return 0
    text = str(text).strip().replace(",", "").replace("，", "")
    if not text:
        return 0

    multiplier = 1
    if text.endswith("万"):
        multiplier, text = 10_000, text[:-1]
    elif text.endswith("億"):
        multiplier, text = 100_000_000, text[:-1]
    elif text.endswith("千"):
        multiplier, text = 1_000, text[:-1]
    elif text.upper().endswith("K"):
        multiplier, text = 1_000, text[:-1]
    elif text.upper().endswith("M"):
        multiplier, text = 1_000_000, text[:-1]

    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


# aria-label から「ラベル付きの数字」を拾うためのパターン。
# 日本語UI・英語UIのどちらでも動くように両方書いておく。
_LABEL_PATTERNS = {
    "replies": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件の返信|返信|repl(?:y|ies))",
    "retweets": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件のリポスト|リポスト|件のリツイート|リツイート|repost|retweet)",
    "likes": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件のいいね|いいね|like)",
    "bookmarks": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件のブックマーク|ブックマーク|bookmark)",
    "impressions": r"([\d,\.]+\s*[万億千KkMm]?)\s*(?:件の表示|表示|view)",
}


def _parse_labeled_counts(label: str) -> dict:
    """
    "12件の返信、34件のリポスト、56件のいいね、78件のブックマーク、9千件の表示"
    のような文字列から、ラベルごとに数値を取り出す。

    ★位置(何番目の数字か)ではなくラベルで取るのが重要。
    v3は位置で取っていたため、UIの要素が1つ増減しただけで
    「ブックマーク数」に全く別の値が入り込んでいた。
    """
    result = {}
    if not label:
        return result
    for field, pattern in _LABEL_PATTERNS.items():
        m = re.search(pattern, label, re.IGNORECASE)
        if m:
            result[field] = _parse_count(m.group(1).replace(" ", ""))
    return result


def sanitize_counts(post: dict) -> dict:
    """
    明らかにありえない数値を捨てる。パース失敗による巨大な誤値が
    スコアを壊すのを防ぐのが目的(0なら単に加点されないだけで済む)。

    ★2026-08-24の修正: リポストの判定基準が厳しすぎて、正常な値まで
      捨てていた。実運用ログで次のような誤検知が出ていた。

        [サニティ] retweets=1237 が likes=330 に対して不自然なため0に補正
        [サニティ] retweets=515166 が likes=109922 に対して不自然なため0に補正

      「リポストがいいねより多い」のは異常ではない。災害情報・注意喚起・
      公式の拡散依頼・キャンペーンなどでは、いいねの数倍リポストされるのが
      普通である(上の515166件はローソン公式のRTキャンペーン投稿で、実在の値)。
      判定基準を3倍から20倍に緩め、パース事故レベルの値だけを弾くようにした。

      ブックマークは事情が違う。「保存した人が、いいねした人より多い」は
      現実にはまず起きないので、1.0倍のままにしてある。
    """
    likes = post.get("likes") or 0
    if likes > 0:
        for field, max_ratio in (("bookmarks", 1.0), ("retweets", 20.0), ("quotes", 1.0)):
            value = post.get(field) or 0
            if value > likes * max_ratio:
                print(
                    f"    [サニティ] {post.get('post_id')}: {field}={value} が "
                    f"likes={likes} に対して不自然なため0に補正しました"
                )
                post[field] = 0

    impressions = post.get("impressions") or 0
    if impressions and likes and impressions < likes:
        # 表示回数がいいね数を下回ることはない
        print(f"    [サニティ] {post.get('post_id')}: impressions={impressions} < likes={likes} のため0に補正")
        post["impressions"] = 0

    return post


def _extract_tweet_id(url: str) -> str:
    match = re.search(r"/status/(\d+)", url or "")
    return match.group(1) if match else ""


# ──────────────────────────────────────────────────────────
#  検索結果カードの解析
# ──────────────────────────────────────────────────────────

def _counts_from_card(article) -> dict:
    """
    投稿カードから各指標を取り出す。

    優先順位:
      1. アクションバー([role="group"])の aria-label をまとめて解析
         → 返信/RT/いいね/ブックマーク/表示 が一度に取れる
      2. 取れなかった項目だけ、個別ボタンの aria-label から取る
    """
    counts = {}

    try:
        group = article.query_selector('[role="group"][aria-label]')
        if group:
            counts.update(_parse_labeled_counts(group.get_attribute("aria-label") or ""))
    except Exception:  # noqa: BLE001
        pass

    # 個別ボタンでの補完
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
        except Exception:  # noqa: BLE001
            continue

    return counts


def _extract_tweet_data(article) -> dict | None:
    """検索結果ページの1投稿分(<article>要素)から必要なデータを抜き出す"""
    try:
        time_el = article.query_selector("time")
        if not time_el:
            return None
        link_handle = time_el.evaluate_handle("el => el.closest('a')")
        link_el = link_handle.as_element() if link_handle else None
        href = link_el.get_attribute("href") if link_el else None
        if not href:
            return None
        post_id = _extract_tweet_id(href)
        if not post_id:
            return None
        url = f"https://x.com{href}" if href.startswith("/") else href

        posted_at_raw = time_el.get_attribute("datetime")

        # 投稿者ハンドル。
        # v3は article 内の最初のリンクを使っていたが、それだと
        # 「○○さんがリポストしました」ヘッダのアカウントを拾ってしまう。
        # User-Name 内のリンクを優先し、無ければ時刻リンクのhrefから取る。
        author_handle = ""
        user_link = article.query_selector('[data-testid="User-Name"] a[href^="/"]')
        if user_link:
            author_handle = (user_link.get_attribute("href") or "").strip("/").split("/")[0]
        if not author_handle:
            author_handle = href.strip("/").split("/")[0]

        text_el = article.query_selector('[data-testid="tweetText"]')
        text_snippet = text_el.inner_text()[:280] if text_el else ""

        post = {
            "post_id": post_id,
            "author_handle": author_handle,
            "url": url,
            "posted_at": posted_at_raw or datetime.now(timezone.utc).isoformat(),
            "text_snippet": text_snippet,
            "likes": 0,
            "retweets": 0,
            "replies": 0,
            "quotes": 0,
            "bookmarks": 0,
            "impressions": 0,
        }
        post.update(_counts_from_card(article))
        return sanitize_counts(post)
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 投稿1件の解析に失敗: {e}")
        return None


# ──────────────────────────────────────────────────────────
#  詳細ページ(カードから取れなかった項目の補完)
# ──────────────────────────────────────────────────────────

def _article_post_id(article) -> str:
    """投稿カードからステータスIDだけを安く取り出す(重複解析を避けるため)"""
    try:
        time_el = article.query_selector("time")
        if not time_el:
            return ""
        href = time_el.evaluate(
            "el => el.closest('a') ? el.closest('a').getAttribute('href') : ''"
        )
        return _extract_tweet_id(href)
    except Exception:  # noqa: BLE001
        return ""


def _article_matches(article, expected_id: str) -> bool:
    """
    表示されている投稿が、開こうとした投稿かどうかを確認する。
    時刻要素のリンク先に含まれるステータスIDで判定する。
    """
    if not expected_id:
        return True
    try:
        time_el = article.query_selector("time")
        if not time_el:
            return False
        href = time_el.evaluate(
            "el => el.closest('a') ? el.closest('a').getAttribute('href') : ''"
        )
        return _extract_tweet_id(href) == expected_id
    except Exception:  # noqa: BLE001
        return False


def _fetch_detail_stats(page, url: str) -> dict:
    """
    投稿の個別ページを開き、ブックマーク・表示回数を取得する。
    検索結果カードの aria-label から取れなかった場合のフォールバック。
    """
    stats = {}
    expected_id = _extract_tweet_id(url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1800)

        # ★読み込みが間に合わないと、前に開いていた投稿の数値を
        #   そのまま読んでしまう。実運用ログで、別々の3投稿に
        #   likes=1038/1039/1045, bookmarks=60/60/61, impressions=38395(同値)
        #   という、明らかに使い回された数値が記録されていた。
        #   開いたページが目的の投稿かどうかを確認し、違えば待ち直す。
        main_article = None
        for attempt in range(3):
            main_article = page.query_selector('article[data-testid="tweet"]')
            if main_article and _article_matches(main_article, expected_id):
                break
            main_article = None
            page.wait_for_timeout(1200)

        if not main_article:
            print(f"  [警告] 詳細ページで目的の投稿を確認できませんでした {url}")
            return stats

        # (1) アクションバーの aria-label(最も信頼できる)
        group = main_article.query_selector('[role="group"][aria-label]')
        if group:
            stats.update(_parse_labeled_counts(group.get_attribute("aria-label") or ""))

        # (2) 本文テキスト中のラベル付き数値
        #     詳細ページは "1,234 件の表示" のようにテキストでも出る
        text = main_article.inner_text()
        for field, value in _parse_labeled_counts(text).items():
            stats.setdefault(field, value)

    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 詳細ページの取得に失敗 {url}: {e}")

    return stats


def _count_quote_tweets(page, url: str) -> int:
    """
    引用一覧ページを開いて件数を数える(FETCH_QUOTES=true のときだけ使う)。

    ★Xは他人の投稿の引用数を正確な数字として公開していないため、
      これは「画面に読み込まれた分を数えた下限値」でしかない。
      1件あたり8秒前後かかるわりに精度が低いので、既定では使わない。
    """
    try:
        quote_url = f"{url}/quotes"
        page.goto(quote_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1800)
        seen = set()
        for _ in range(3):
            for article in page.query_selector_all('article[data-testid="tweet"]'):
                time_el = article.query_selector("time")
                if not time_el:
                    continue
                href = time_el.evaluate(
                    "el => el.closest('a') ? el.closest('a').getAttribute('href') : ''"
                )
                tid = _extract_tweet_id(href)
                if tid:
                    seen.add(tid)
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
        return len(seen)
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 引用一覧の取得に失敗 {url}: {e}")
        return 0


# ──────────────────────────────────────────────────────────
#  検索クエリの組み立てと実行
# ──────────────────────────────────────────────────────────

def _with_recency(query: str) -> str:
    """新しい投稿だけに絞る指定を付け足す"""
    return f"{query} within_time:{SEARCH_WITHIN_TIME}" if SEARCH_WITHIN_TIME else query


def build_queries() -> list[tuple[str, str, int]]:
    """
    実行する検索クエリの一覧を (クエリ文字列, モード, スクロール回数) で返す。
    モード: "live"=時系列順(新着) / "top"=話題性順(Xのアルゴリズムが選んだもの)

    ★演算子の個数に注意。X検索は演算子が22〜23個を超えると、
      超えた分をエラーも出さずに無視する。組み立てたクエリは
      keyword_filter.count_operators() で必ず確認すること。
    """
    queries: list[tuple[str, str, int]] = []

    # (1) キーワード特化クエリ … 本命。ジャンルが絞れているので閾値を低くできる。
    for group in keyword_filter.query_groups():
        q = _with_recency(f"({group}) lang:ja -filter:retweets min_faves:{SEARCH_MIN_FAVES}")
        queries.append((q, "live", SCROLLS_KEYWORD))

    # (2) 組み合わせ検索 … keywords_combo.txt の各行がそのまま1本の検索になる。
    #     「逮捕」のような単独では一般ニュースに埋もれるワードを、
    #     文脈のAND条件で絞って拾うためのもの。
    #     絞り込みが効いている分、閾値をさらに下げる。
    for expression in keyword_filter.combo_queries():
        q = _with_recency(f"{expression} lang:ja -filter:retweets min_faves:{COMBO_MIN_FAVES}")
        queries.append((q, "live", SCROLLS_COMBO))

    # (3) 広域クエリ … キーワードに無い話題を取りこぼさないための保険。
    #     実測すると公式アカウントの誕生日祝い・新譜告知で埋まっていたため、
    #     それらを除外したうえで、閾値を高めに保つ。
    exclusions = " ".join(f"-{w}" for w in BROAD_EXCLUDE)
    broad = _with_recency(
        f"lang:ja -filter:retweets -filter:replies "
        f"min_faves:{BROAD_MIN_FAVES} {exclusions}".strip()
    )
    queries.append((broad, "live", SCROLLS_BROAD))
    queries.append((broad, "top", SCROLLS_BROAD))

    # 組み立てたクエリが演算子上限に収まっているか確認する
    for query, mode, _ in queries:
        ops = keyword_filter.count_operators(query)
        if ops > keyword_filter.OPERATOR_WARN_THRESHOLD:
            print(
                f"  [警告] 演算子が{ops}個あります(上限は22〜23個)。"
                f"超過分は無視される可能性があります: {query[:60]}..."
            )

    return queries


def _search_one_query(page, query: str, mode: str, scrolls: int) -> list[dict]:
    f_param = "&f=live" if mode == "live" else ""
    search_url = f"https://x.com/search?q={quote(query)}&src=typed_query{f_param}"
    print(f"  検索[{mode}]: {query[:70]}{'...' if len(query) > 70 else ''}")
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    current_url = page.url
    if "/login" in current_url or "/i/flow/login" in current_url:
        raise SessionExpiredError(
            "Xのログインセッションが切れているようです(ログイン画面にリダイレクトされました)。"
            "codespace_login.sh を再実行してセッションを更新してください。"
        )

    posts_by_id = {}
    for _ in range(scrolls):
        for article in page.query_selector_all('article[data-testid="tweet"]'):
            # スクロールしても既読の投稿は画面に残り続けるため、
            # 毎回すべてを解析し直すと同じ投稿を何度も処理することになる。
            # 先に安いID取得だけ行い、既に読んだものは飛ばす。
            post_id = _article_post_id(article)
            if post_id and post_id in posts_by_id:
                continue
            data = _extract_tweet_data(article)
            if data:
                posts_by_id[data["post_id"]] = data
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(int(SCROLL_WAIT_SECONDS * 1000))

    return list(posts_by_id.values())


def _age_minutes(posted_at: str) -> float:
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def fetch_posts() -> list[dict]:
    """全ての検索クエリを実行し、正規化済みの投稿リストを返す"""
    from playwright.sync_api import sync_playwright

    session_path = _session_path()
    all_posts: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session_path)
        page = context.new_page()

        queries = build_queries()
        print(f"検索クエリ数: {len(queries)}")
        within_time_failures = 0
        within_time_queries = 0

        for query, mode, scrolls in queries:
            try:
                posts = _search_one_query(page, query, mode, scrolls)

                # ★within_time はXの非公式な検索演算子で、公式ドキュメントに
                #   記載がない。将来Xが対応をやめると、この指定を含むクエリが
                #   常に0件を返し、システムが静かに無音になってしまう。
                #   0件だったら指定を外して1回だけやり直し、どちらが原因か
                #   切り分けられるようにする。
                if not posts and "within_time:" in query:
                    within_time_queries += 1
                    fallback = re.sub(r"\s*within_time:\S+", "", query)
                    print("    (0件のため within_time を外して再試行します)")
                    posts = _search_one_query(page, fallback, mode, scrolls)
                    if posts:
                        within_time_failures += 1
                elif "within_time:" in query:
                    within_time_queries += 1

                for post in posts:
                    existing = all_posts.get(post["post_id"])
                    if existing:
                        # 同じ投稿を複数クエリで拾った場合は、値が入っている方を残す
                        for field in ("likes", "retweets", "replies", "bookmarks", "impressions"):
                            if not existing.get(field) and post.get(field):
                                existing[field] = post[field]
                    else:
                        all_posts[post["post_id"]] = post
                print(f"  → {len(posts)}件(累計{len(all_posts)}件)")
            except SessionExpiredError:
                browser.close()
                raise
            except Exception as e:  # noqa: BLE001
                print(f"  [ERROR] 検索クエリ失敗 [{mode}]: {e}")

        if within_time_failures and within_time_failures >= within_time_queries:
            print(
                "\n  [警告] within_time 付きのクエリが全て0件で、外すと取得できました。\n"
                "         Xが within_time 演算子に対応しなくなった可能性が高いです。\n"
                "         Variables に SEARCH_WITHIN_TIME=off を設定して無効化してください。\n"
            )

        # ── 詳細ページで補完 ──
        # カードからブックマーク/表示回数が取れなかった投稿のうち、
        # 「新しくて、いいねが多い」ものを優先して補完する。
        needs_detail = [
            p for p in all_posts.values()
            if (not p.get("bookmarks") or not p.get("impressions"))
            and _age_minutes(p["posted_at"]) <= COLLECT_MAX_AGE_MINUTES
            and not keyword_filter.is_ng(p.get("text_snippet", ""))
        ]
        needs_detail.sort(key=lambda p: p.get("likes", 0), reverse=True)
        needs_detail = needs_detail[:MAX_DETAIL_FETCH]
        print(f"  [詳細補完] 対象{len(needs_detail)}件(上限{MAX_DETAIL_FETCH})")

        for post in needs_detail:
            stats = _fetch_detail_stats(page, post["url"])
            for field, value in stats.items():
                if value:
                    post[field] = value
            if FETCH_QUOTES:
                post["quotes"] = _count_quote_tweets(page, post["url"])
            sanitize_counts(post)
            print(
                f"    [詳細] {post['post_id']}: likes={post.get('likes')}, "
                f"bookmarks={post.get('bookmarks')}, impressions={post.get('impressions')}"
            )

        browser.close()

    result = list(all_posts.values())

    # ── 診断ログ ──
    # 「取れているはずの指標がいつも0」になっていないかをここで確認できる。
    top5 = sorted(result, key=lambda p: p.get("likes", 0), reverse=True)[:5]
    print("\n--- 診断ログ: いいね数上位5件 ---")
    for p in top5:
        print(
            f"  [{p['post_id']}] @{p['author_handle']}: "
            f"likes={p['likes']}, rt={p['retweets']}, reply={p['replies']}, "
            f"bm={p['bookmarks']}, imp={p['impressions']}, "
            f"age={_age_minutes(p['posted_at']):.0f}分"
        )
    filled = sum(1 for p in result if p.get("bookmarks"))
    print(f"  ブックマークが取得できた投稿: {filled}/{len(result)}件")
    print("--- 診断ログここまで ---\n")

    return result


if __name__ == "__main__":
    for q, m, s in build_queries():
        print(f"[{m}] scrolls={s} :: {q}")
