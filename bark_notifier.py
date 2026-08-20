"""
bark_notifier.py
Bark(無料・オープンソースのiOSプッシュ通知アプリ)経由で通知を送る。
LINE・ntfy・メールに加えて、4つ目の通知ルートとして使う。

Barkの利点:
  ntfyは優先度(priority)に応じたプリセット音しか選べないが、Barkは
  通知1件ごとに「sound」パラメータでiOS標準の通知音を直接指定できる。
  さらに「level: critical」を指定すると、iPhoneがマナーモード・
  おやすみモード中でも音が鳴る(重要な通知)扱いになる。

事前準備:
  1. App Storeで「Bark」をインストール(アカウント登録不要)
  2. アプリを開くと画面上部に「デバイスキー」を含むURLが表示される
     (例: https://api.day.app/xxxxxxxxxxxxxxxxxxxxxxxx/)
     この xxxxxxxxxxxxxxxxxxxxxxxx の部分が BARK_DEVICE_KEY
  3. 環境変数に以下を設定:
     BARK_DEVICE_KEY : 上記で取得したデバイスキー
     BARK_SERVER_URL : 省略可。自前でBarkサーバーを立てている場合のみ指定
                        (未設定なら公式の https://api.day.app を使う)

音について:
  BARK_SOUND環境変数で明示的に音を変えない限り、デフォルトでは
  "alarm"(アラーム音)を使う。他の通知音の候補はBarkアプリ内の
  「サウンド一覧」から確認できる。
"""

import os

import requests

DEFAULT_BARK_SERVER_URL = "https://api.day.app"
DEFAULT_SOUND = "alarm"


def send_notification(post: dict) -> bool:
    """
    1件の投稿についてBark通知を送る。成功したら True。
    """
    device_key = os.environ.get("BARK_DEVICE_KEY")
    if not device_key:
        raise RuntimeError("環境変数 BARK_DEVICE_KEY が設定されていません")

    server_url = (os.environ.get("BARK_SERVER_URL") or DEFAULT_BARK_SERVER_URL).rstrip("/")
    sound = os.environ.get("BARK_SOUND") or DEFAULT_SOUND

    genre = post.get("genre", "その他")
    likes = post.get("likes", 0)
    quotes = post.get("quotes", 0)
    bookmarks = post.get("bookmarks", 0)
    hours = post.get("elapsed_hours", "?")
    url = post.get("url", "")
    author = post.get("author_handle", "")

    body = (
        f"{author} の投稿が{hours}時間で"
        f"いいね{likes:,}・引用{quotes:,}・ブックマーク{bookmarks:,}"
    )

    matched_keyword = post.get("matched_keyword")
    if matched_keyword:
        body += f"\n🔑 マッチしたワード: {matched_keyword}"

    is_gekiatsu = post.get("is_gekiatsu", False)
    type_label = "瞬間" if post.get("explosive_type") == "instant" else "持続"
    title = f"🔥🔥🔥 激アツ投稿 [{type_label}/{genre}]" if is_gekiatsu else f"🔥 急上昇検知 [{type_label}/{genre}]"

    payload = {
        "device_key": device_key,
        "title": title,
        "body": body,
        "sound": sound,
        "level": "critical",  # マナーモード・おやすみモード中でも鳴らす
        "group": "sns-monitor",
    }
    if url:
        payload["url"] = url

    resp = requests.post(f"{server_url}/push", json=payload, timeout=10)
    if resp.status_code != 200:
        print(f"[Bark通知失敗] status={resp.status_code} body={resp.text}")
        return False

    try:
        data = resp.json()
    except ValueError:
        print(f"[Bark通知失敗] JSON以外の応答: {resp.text}")
        return False

    if data.get("code") != 200:
        print(f"[Bark通知失敗] {data}")
        return False

    return True
