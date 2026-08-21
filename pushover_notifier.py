"""
pushover_notifier.py
Pushover(iOSアプリは買い切り課金、送信APIは無料)経由で通知を送る。
LINE(動作確認用にも使うので残す)に加えて、こちらが「はっきりした音で
確実に気づく」ための通知ルートになる。

Pushoverの利点(ntfy/Barkとの違い):
  優先度2(Emergency)を指定すると、通知を確認してタップする(または
  スワイプで消す)までPUSHOVER_RETRY秒おきに音が鳴り続ける。マナー
  モード・おやすみモードも貫通する。ntfy/Barkの「1回鳴って終わり」
  より確実に気づける。

事前準備:
  1. App Storeで「Pushover」をインストール(買い切り。30日間は無料お試し)
  2. https://pushover.net でアカウント作成し、アプリにログイン
  3. アカウント画面右上に表示されている「Your User Key」をメモ
     → 環境変数 PUSHOVER_USER_KEY に設定
  4. 「Create an Application/API Token」から新しいアプリケーションを
     作成し(名前は何でもいい)、発行される「API Token/Key」をメモ
     → 環境変数 PUSHOVER_TOKEN に設定

音・優先度のカスタマイズ(任意):
  PUSHOVER_SOUND    : 通知音。省略時は "siren"(アプリ内の「サウンド一覧」で試聴可)
  PUSHOVER_PRIORITY : 通常時の優先度。省略時は "2"(Emergency = マナーモード・
                       おやすみモードを貫通して、確認するまで鳴り続ける)
  PUSHOVER_RETRY     : Emergency時の再通知間隔(秒)。省略時は60。最小30
  PUSHOVER_EXPIRE     : Emergency時に鳴り続ける最大時間(秒)。省略時は3600。最大10800

学校にいる間だけ優先度を下げる仕組み(任意):
  Critical Alert(優先度2)はiOSの仕様上、おやすみモード・集中モードを
  アプリ単位で除外することができない(常に貫通してしまう)。そのため、
  「学校にいる間はおやすみモードに従わせたい(＝鳴らしたくない)」を
  実現するには、iPhone側のショートカット(位置情報オートメーション)から
  Cloudflare Worker(cloudflare_worker.js の /school-status)に「今学校に
  いるか」を伝えてもらい、ここでその状態を見て優先度を切り替える。

  SCHOOL_STATUS_URL    : cloudflare_worker.js の /school-status のURL
                          (例: https://xxxx.workers.dev/school-status)
                          未設定なら、この機能自体を使わない(常にPUSHOVER_PRIORITY)
  SCHOOL_STATUS_SECRET : cloudflare_worker.js 側と共有する合言葉
  PUSHOVER_SCHOOL_PRIORITY : 学校にいる間に使う優先度。省略時は "0"
                              (通常優先度。おやすみモード中は鳴らない)

  学校の位置情報判定や通信の失敗時は「学校にいない」扱い(＝通常通り
  Criticalで確実に鳴らす)にフェイルオープンする。通知が届かなくなる
  よりは、余計に鳴ってしまう方を安全側として選んでいる。
"""

import os

import requests

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
DEFAULT_SOUND = "siren"
DEFAULT_PRIORITY = "2"
DEFAULT_SCHOOL_PRIORITY = "0"
DEFAULT_RETRY = "60"
DEFAULT_EXPIRE = "3600"


def _is_at_school() -> bool:
    """
    Cloudflare Workerに「今学校にいるか」を問い合わせる。
    SCHOOL_STATUS_URLが未設定、または通信に失敗した場合はFalse
    (学校にいない扱い=通常通りCriticalで鳴らす)を返す。
    """
    status_url = os.environ.get("SCHOOL_STATUS_URL")
    secret = os.environ.get("SCHOOL_STATUS_SECRET")
    if not status_url or not secret:
        return False

    try:
        resp = requests.get(
            status_url,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=5,
        )
        if resp.status_code != 200:
            print(f"[学校判定] school-status取得に失敗: status={resp.status_code}")
            return False
        return bool(resp.json().get("at_school", False))
    except Exception as e:  # noqa: BLE001
        print(f"[学校判定] 取得に失敗、通常優先度で送信します: {e}")
        return False


def send_notification(post: dict) -> bool:
    """
    1件の投稿についてPushover通知を送る。成功したら True。
    """
    token = os.environ.get("PUSHOVER_TOKEN")
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    if not token or not user_key:
        raise RuntimeError(
            "環境変数 PUSHOVER_TOKEN / PUSHOVER_USER_KEY が設定されていません"
        )

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

    matched_keyword = post.get("matched_keyword")
    if matched_keyword:
        message += f"\n🔑 マッチしたワード: {matched_keyword}"

    is_gekiatsu = post.get("is_gekiatsu", False)
    type_label = "瞬間" if post.get("explosive_type") == "instant" else "持続"
    title = f"🔥🔥🔥 激アツ投稿 [{type_label}/{genre}]" if is_gekiatsu else f"🔥 急上昇検知 [{type_label}/{genre}]"

    if _is_at_school():
        priority = os.environ.get("PUSHOVER_SCHOOL_PRIORITY") or DEFAULT_SCHOOL_PRIORITY
        print(f"[学校判定] 学校にいるため優先度を{priority}に下げます")
    else:
        priority = os.environ.get("PUSHOVER_PRIORITY") or DEFAULT_PRIORITY

    payload = {
        "token": token,
        "user": user_key,
        "title": title,
        "message": message,
        "sound": os.environ.get("PUSHOVER_SOUND") or DEFAULT_SOUND,
        "priority": priority,
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "投稿を開く"

    # Emergency(優先度2)は retry・expire が必須
    if priority == "2":
        payload["retry"] = os.environ.get("PUSHOVER_RETRY") or DEFAULT_RETRY
        payload["expire"] = os.environ.get("PUSHOVER_EXPIRE") or DEFAULT_EXPIRE

    resp = requests.post(PUSHOVER_API_URL, data=payload, timeout=10)
    if resp.status_code != 200:
        print(f"[Pushover通知失敗] status={resp.status_code} body={resp.text}")
        return False

    data = resp.json()
    if data.get("status") != 1:
        print(f"[Pushover通知失敗] {data}")
        return False

    return True
