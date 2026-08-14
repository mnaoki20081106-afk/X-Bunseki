"""
line_notifier.py
LINE Messaging API (push message) でLOのiPhoneに即時通知する。

事前準備:
  1. https://developers.line.biz/ja/ で「Messaging API」チャネルを作成(無料)
  2. チャネルアクセストークン(長期)を発行
  3. 作成した公式アカウントを自分のLINEで友だち追加
  4. 自分のuserIdを取得(友だち追加後、Webhookで一度受信するか、
     LINE公式アカウントの「友だちリスト」機能等で確認)
  5. 環境変数に以下を設定:
     LINE_CHANNEL_ACCESS_TOKEN
     LINE_USER_ID
"""

import os

import requests

LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"


def _headers() -> dict:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定されていません")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def build_message(post: dict) -> str:
    genre = post.get("genre", "その他")
    likes = post.get("likes", 0)
    quotes = post.get("quotes", 0)
    bookmarks = post.get("bookmarks", 0)
    hours = post.get("elapsed_hours", "?")
    url = post.get("url", "")
    author = post.get("author_handle", "")

    type_label = "⚡瞬間" if post.get("explosive_type") == "instant" else "📈持続"

    if post.get("is_gekiatsu"):
        header = f"🔥🔥🔥 激アツ投稿 [{type_label}/{genre}]"
        quote_ratio = post.get("quote_like_ratio", 0) * 100
        bookmark_ratio = post.get("bookmark_like_ratio", 0) * 100
        detail = (
            f"{author} の投稿が{hours}時間で"
            f"いいね{likes:,}・引用{quotes:,}({quote_ratio:.0f}%)"
            f"・ブックマーク{bookmarks:,}({bookmark_ratio:.0f}%)"
        )
    else:
        header = f"🔥 急上昇検知 [{type_label}/{genre}]"
        detail = (
            f"{author} の投稿が{hours}時間で"
            f"いいね{likes:,}・引用{quotes:,}・ブックマーク{bookmarks:,}"
        )

    return f"{header}\n{detail}\n{url}"


def send_notification(post: dict) -> bool:
    """
    1件の投稿について通知を送る。成功したら True。
    """
    user_id = os.environ.get("LINE_USER_ID")
    if not user_id:
        raise RuntimeError("環境変数 LINE_USER_ID が設定されていません")

    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": build_message(post)}],
    }

    resp = requests.post(
        LINE_PUSH_ENDPOINT, headers=_headers(), json=payload, timeout=10
    )
    if resp.status_code != 200:
        print(f"[LINE通知失敗] status={resp.status_code} body={resp.text}")
        return False
    return True
