"""
apify_collector.py
2段階方式でX(Twitter)の投稿データを収集する。

★背景(重要): X関連のApify Actorは検索(search)や単一ツイート取得(tweet detail)の
  エンドポイントではインプレッション数(viewCount)を返さないことが判明した。
  viewCountが確認できるのは「プロフィールのタイムライン取得」エンドポイントのみ。
  そのため以下の2段階方式を採用する。

  1段階目(発見・広く安く): apidojo/tweet-scraper で検索し、いいね数を「仮の
     急上昇シグナル」として使い、候補を絞り込む(本物のインプレッション数はまだ無い)
  2段階目(確定・絞ってから): 1段階目で候補になった投稿の投稿者アカウントだけ、
     apidojo/twitter-profile-scraper でプロフィールを引き直し、
     該当ツイートの本物のviewCountを取得する

事前準備:
  pip install apify-client
  環境変数 APIFY_TOKEN を設定 (https://console.apify.com/account/integrations)
"""

import os

from apify_client import ApifyClient

SEARCH_ACTOR_ID = os.environ.get("APIFY_SEARCH_ACTOR_ID", "apidojo/tweet-scraper")
PROFILE_ACTOR_ID = os.environ.get("APIFY_PROFILE_ACTOR_ID", "apidojo/twitter-profile-scraper")

# 監視対象の検索クエリ。ジャンルを横断して拾いたいので、
# 特定ジャンルに偏らない広めの条件にしてある。
SEARCH_QUERIES = [
    "lang:ja min_faves:5000",  # 日本語・いいね5000以上(要調整)
]

MAX_ITEMS_SEARCH = 200  # 1段階目: 検索での取得上限

# 1段階目で「2段階目に進める価値がある候補」と判定するための仮の閾値。
# 本物のインプレッション数ではなく、いいね数ベースの粗いフィルタ。
# 500万インプレッションを狙うなら、経験則としていいね数はその1/50〜1/100程度になることが多い
# (投稿の性質によって大きく変動するため、運用しながら調整すること)
PRELIMINARY_LIKE_THRESHOLD = 30_000


def _client() -> ApifyClient:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("環境変数 APIFY_TOKEN が設定されていません")
    return ApifyClient(token)


def _parse_twitter_date(raw: str) -> str:
    """
    apidojo系Actorの createdAt は "Wed Sep 24 18:06:27 +0000 2025" 形式(Twitter独自形式)。
    detector.py はISO8601を期待するため、ここで変換する。
    """
    from datetime import datetime

    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        return dt.isoformat()
    except (ValueError, TypeError):
        return raw  # 変換失敗時はそのまま返す(detector側でエラーになるので要調査)


def _normalize_search_item(item: dict) -> dict | None:
    """
    1段階目(apidojo/tweet-scraper の検索結果)を正規化する。
    この段階では impressions はまだ不明(None)。2段階目で埋める。
    """
    try:
        return {
            "post_id": str(item["id"]),
            "author_handle": item.get("author", {}).get("userName", ""),
            "url": item.get("url") or item.get("twitterUrl", ""),
            "posted_at": _parse_twitter_date(item.get("createdAt", "")),
            "text_snippet": (item.get("text") or "")[:200],
            "impressions": None,  # 2段階目で埋める
            "likes": int(item.get("likeCount") or 0),
            "retweets": int(item.get("retweetCount") or 0),
            "replies": int(item.get("replyCount") or 0),
        }
    except (KeyError, TypeError, ValueError):
        return None


def fetch_posts() -> list[dict]:
    """
    2段階方式のエントリポイント。
    1段階目(検索)→ 仮フィルタ → 2段階目(プロフィール取得で本物のimpressions付与)
    という流れをまとめて実行し、最終的な post 辞書のリストを返す。
    """
    stage1_posts = _search_candidates()
    print(f"  [1段階目] 検索で取得: {len(stage1_posts)}件")

    preliminary = [p for p in stage1_posts if p["likes"] >= PRELIMINARY_LIKE_THRESHOLD]
    print(f"  [1段階目] 仮フィルタ通過(いいね{PRELIMINARY_LIKE_THRESHOLD}以上): {len(preliminary)}件")

    final_posts = _enrich_with_impressions(preliminary)
    print(f"  [2段階目] インプレッション数取得完了: {len(final_posts)}件")

    return final_posts


def _search_candidates() -> list[dict]:
    """1段階目: 検索でいいね数などの基本エンゲージメント情報を取得(impressionsはまだNone)"""
    client = _client()

    run_input = {
        "searchTerms": SEARCH_QUERIES,
        "maxItems": MAX_ITEMS_SEARCH,
        "sort": "Latest",
    }

    run = client.actor(SEARCH_ACTOR_ID).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"]

    posts = []
    for item in client.dataset(dataset_id).iterate_items():
        normalized = _normalize_search_item(item)
        if normalized:
            posts.append(normalized)
    return posts


def _enrich_with_impressions(candidates: list[dict]) -> list[dict]:
    """
    2段階目: 候補の投稿者だけプロフィールを取得し直し、該当ツイートの
    viewCount(インプレッション数)を突き合わせて埋める。
    """
    if not candidates:
        return []

    client = _client()

    # 同じ投稿者が複数候補に混ざっている場合、プロフィール取得は1回にまとめる
    handles = sorted({c["author_handle"] for c in candidates if c["author_handle"]})
    if not handles:
        return []

    run_input = {
        "twitterHandles": handles,
        "maxItems": max(200, len(handles) * 40),  # 各プロフィール40件は無料枠に収まる
    }

    run = client.actor(PROFILE_ACTOR_ID).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"]

    # post_id -> viewCount の対応表を作る
    impressions_by_id = {}
    for item in client.dataset(dataset_id).iterate_items():
        post_id = str(item.get("id", ""))
        if post_id:
            impressions_by_id[post_id] = int(item.get("viewCount") or 0)

    enriched = []
    for c in candidates:
        impressions = impressions_by_id.get(c["post_id"])
        if impressions is None:
            # プロフィール再取得で見つからなかった(取得範囲外など)投稿はスキップ
            continue
        c = dict(c)
        c["impressions"] = impressions
        enriched.append(c)

    return enriched


if __name__ == "__main__":
    for p in fetch_posts():
        print(p)
