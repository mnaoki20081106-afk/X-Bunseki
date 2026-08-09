"""
email_notifier.py
Gmail(SMTP)経由でバズ投稿検知時にメール通知を送る。
LINE・ntfyに加えて、3つ目の通知ルートとして使う。

事前準備:
  1. Gmailアカウントを作成(通知送信専用でOK)
  2. Googleアカウントの「2段階認証」を有効にする
     (https://myaccount.google.com/security)
  3. 「アプリパスワード」を発行する
     (https://myaccount.google.com/apppasswords)
     - アプリ: 「メール」、デバイス: 「その他」などを選び、名前は何でもいい
     - 発行された16桁のパスワード(スペース無視)をメモする
       ★これは普段Gmailにログインする時のパスワードとは別物
  4. 環境変数に以下を設定:
     GMAIL_ADDRESS      : 送信元のGmailアドレス
     GMAIL_APP_PASSWORD : 上記で発行したアプリパスワード
     NOTIFY_EMAIL_TO    : 通知を受け取りたいメールアドレス(未設定ならGMAIL_ADDRESS宛)
"""

import os
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def build_subject(post: dict) -> str:
    genre = post.get("genre", "その他")
    if post.get("is_gekiatsu"):
        return f"🔥🔥🔥 激アツ投稿検知 [{genre}]"
    return f"🔥 急上昇検知 [{genre}]"


def build_body(post: dict) -> str:
    likes = post.get("likes", 0)
    quotes = post.get("quotes", 0)
    bookmarks = post.get("bookmarks", 0)
    hours = post.get("elapsed_hours", "?")
    url = post.get("url", "")
    author = post.get("author_handle", "")

    return (
        f"{author} の投稿が{hours}時間で"
        f"いいね{likes:,}・引用{quotes:,}・ブックマーク{bookmarks:,}\n\n"
        f"{url}"
    )


def send_notification(post: dict) -> bool:
    """
    1件の投稿についてメール通知を送る。成功したら True。
    """
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_address = os.environ.get("NOTIFY_EMAIL_TO") or gmail_address

    if not gmail_address or not app_password:
        raise RuntimeError(
            "環境変数 GMAIL_ADDRESS / GMAIL_APP_PASSWORD が設定されていません"
        )

    msg = MIMEText(build_body(post))
    msg["Subject"] = build_subject(post)
    msg["From"] = gmail_address
    msg["To"] = to_address

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, [to_address], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[メール通知失敗] {e}")
        return False
