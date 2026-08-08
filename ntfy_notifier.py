"""
ntfy_notifier.py
ntfy(無料・オープンソースのプッシュ通知サービス)経由で通知を送る。
LINE通知(line_notifier.py)に加えて、こちらも併用する。

事前準備:
  1. App Storeで「ntfy」をインストール(アカウント登録不要)
  2. アプリ内で好きなトピック名(他人と被らない複雑なもの)を作成して購読
  3. そのトピック名を環境変数 NTFY_TOPIC に設定

注意:
  ntfy.shは公開サーバーなので、トピック名を知っていれば誰でも購読・投稿できる。
  推測されにくい複雑なトピック名にすること(例: sns-monitor-8f3k2x9)。

音について:
  iOSの制約上、mp3などの独自音声ファイルは使えない。優先度(priority)に応じて
  ntfyアプリ側が数種類のプリセット音・バイブレーションパターンを自動的に
  使い分ける仕組みになっている。ここではpriorityを5(urgent、最大値)に
  固定し、LINEとは違う目立つ通知になるようにしている。
"""

import os

import requests

NTFY_BASE_URL = "https://ntfy.sh"


def send_notification(post: dict) -> bool:
    """
    1件の投稿についてntfy通知を送る。成功したら True。
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        raise RuntimeError("環境変数 NTFY_TOPIC が設定されていません")

    genre = post.get("genre", "その他")
    likes = post.get("likes", 0)
    quotes = post.get("quotes", 0)
    bookmarks = post.get("bookmarks", 0)
    hours = post.get("elapsed_hours", "?")
    url = post.get("url", "")
    author = post.get("author_handle", "")

    message = (
        f"{author} の投稿が{hours}時間で"
        f"いいね{likes:,}・引用{quotes:,}・ブックマーク{bookmarks:,}"
    )

    headers = {
        "Title": f"急上昇検知 [{genre}]".encode("utf-8"),
        "Priority": "5",  # urgent(最大値)。他の通知より目立つ音・バイブレーションになる
        "Tags": "fire",   # 🔥 絵文字がタイトルに付く
        "Click": url,      # 通知をタップすると投稿ページが開く
    }

    resp = requests.post(
        f"{NTFY_BASE_URL}/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[ntfy通知失敗] status={resp.status_code} body={resp.text}")
        return False
    return True
