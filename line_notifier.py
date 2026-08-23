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

import notification_text

LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"

# ★LINEの無料プランはプッシュ通知が月200通まで(2026-08-24に上限到達を確認)。
#
#     [LINE通知失敗] status=429 {"message":"You have reached your monthly limit."}
#
# 15分間隔で1日最大12件通知すると、月360通で確実に超過する。
# そのため既定では、LINEには**システム通知(セッション切れ・不具合アラート)
# だけ**を送り、投稿の急上昇通知はPushoverに任せる。
# Pushoverは送信数の制限が実質無いので、こちらが主経路として適している。
#
#   LINE_MODE=alerts_only : システム通知のみ送る(既定)
#   LINE_MODE=all         : 急上昇通知もLINEに送る(月200通に注意)
#   LINE_MODE=off         : LINEには一切送らない
LINE_MODE = (os.environ.get("LINE_MODE") or "alerts_only").strip().lower()

_quota_warned = False


def _headers() -> dict:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定されていません")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def build_message(post: dict) -> str:
    """
    通知本文の組み立ては notification_text.py に一本化した。
    v3ではLINEとPushoverで別々に本文を作っていて内容がズレていたため。
    """
    if post.get("system_message"):
        return post["system_message"]
    return notification_text.build_message(post)


def should_send(post: dict) -> bool:
    """LINE_MODE の設定にもとづいて、この通知をLINEに送るかどうか判定する"""
    if LINE_MODE == "off":
        return False
    if LINE_MODE == "alerts_only":
        # システム通知(セッション切れ等)と、通知確認テストだけ送る
        return bool(post.get("system_message"))
    return True


def send_notification(post: dict) -> bool:
    """
    1件の投稿について通知を送る。成功したら True。
    """
    if not should_send(post):
        print("  (LINEは LINE_MODE=alerts_only のため送信しません)")
        return False

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
    if resp.status_code == 429:
        # 月間上限。毎回同じ内容を出しても仕方がないので1度だけ案内する。
        global _quota_warned
        if not _quota_warned:
            print(
                "[LINE] 月間送信上限に達しました。通知はPushoverのみで届きます。\n"
                "       LINEの無料プランはプッシュ通知が月200通までです。\n"
                "       翌月にリセットされます。"
            )
            _quota_warned = True
        return False

    if resp.status_code != 200:
        print(f"[LINE通知失敗] status={resp.status_code} body={resp.text}")
        return False
    return True
