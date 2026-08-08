"""
main.py
1回分の監視サイクルを実行する。GitHub Actions等から1時間おきに呼び出す想定。

流れ:
  1. Playwrightで投稿収集(ログインセッションが切れていたら専用エラー)
  2. 「爆発」判定(引用・ブックマーク・いいねの閾値 × 経過時間)
  3. 未通知の候補だけをGroqでジャンル分類
  4. LINEに即時通知
  5. DBに記録(重複通知防止)
"""

import sys
import traceback
from datetime import datetime, timedelta, timezone

import db
import detector
import genre_classifier
import line_notifier
from playwright_collector import SessionExpiredError, fetch_posts

SESSION_ALERT_COOLDOWN_HOURS = 24  # セッション切れ通知は1日1回まで
ZERO_POSTS_ALERT_THRESHOLD = 3     # 何回連続で0件だったら「壊れてるかも」と通知するか
ZERO_POSTS_ALERT_COOLDOWN_HOURS = 24  # DOM構造変化アラートも1日1回まで


def _notify_session_expired():
    """
    セッション切れをLINEで知らせる。ただし1日1回まで(毎時失敗し続けて
    通知が埋まるのを防ぐため)。
    """
    last_alert = db.get_meta("session_expired_alert_at")
    now = datetime.now(timezone.utc)

    if last_alert:
        last_alert_dt = datetime.fromisoformat(last_alert)
        if now - last_alert_dt < timedelta(hours=SESSION_ALERT_COOLDOWN_HOURS):
            print("(セッション切れ通知はクールダウン中のためスキップ)")
            return

    try:
        line_notifier.send_notification({
            "genre": "システム通知",
            "likes": 0, "quotes": 0, "bookmarks": 0, "elapsed_hours": "-",
            "url": "https://x.com/login",
            "author_handle": "⚠️ ログインセッションが切れました。codespace_login.sh を再実行してください",
        })
        db.set_meta("session_expired_alert_at", now.isoformat())
        print("セッション切れ通知をLINEに送信しました")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] セッション切れ通知の送信にも失敗: {e}")


def _check_zero_posts_streak(posts_count: int):
    """
    取得件数が0件の実行が連続していないかチェックする。
    連続していたら、Xのページ構造が変わってデータが取れなくなっている
    可能性が高いのでLINEで知らせる(セッション切れとは別原因の壊れ方)。
    """
    now = datetime.now(timezone.utc)

    if posts_count > 0:
        # 正常に取れているなら連続カウントをリセット
        db.set_meta("zero_posts_streak", "0")
        return

    streak = int(db.get_meta("zero_posts_streak") or "0") + 1
    db.set_meta("zero_posts_streak", str(streak))
    print(f"(取得0件が{streak}回連続)")

    if streak < ZERO_POSTS_ALERT_THRESHOLD:
        return

    last_alert = db.get_meta("zero_posts_alert_at")
    if last_alert:
        last_alert_dt = datetime.fromisoformat(last_alert)
        if now - last_alert_dt < timedelta(hours=ZERO_POSTS_ALERT_COOLDOWN_HOURS):
            print("(0件連続アラートはクールダウン中のためスキップ)")
            return

    try:
        line_notifier.send_notification({
            "genre": "システム通知",
            "likes": 0, "quotes": 0, "bookmarks": 0, "elapsed_hours": "-",
            "url": "https://x.com",
            "author_handle": (
                f"⚠️ {streak}回連続で投稿を0件しか取得できていません。"
                "Xのページ構造が変わった可能性があります(要確認)"
            ),
        })
        db.set_meta("zero_posts_alert_at", now.isoformat())
        print("0件連続アラートをLINEに送信しました")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 0件連続アラートの送信にも失敗: {e}")


def run_once():
    started_at = datetime.now(timezone.utc).isoformat()
    db.init_db()

    try:
        posts = fetch_posts()
    except SessionExpiredError as e:
        print(f"[ERROR] {e}")
        db.log_run(started_at, datetime.now(timezone.utc).isoformat(), 0, 0, "session_expired", str(e))
        _notify_session_expired()
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] データ収集に失敗しました: {e}")
        traceback.print_exc()
        db.log_run(started_at, datetime.now(timezone.utc).isoformat(), 0, 0, "error", str(e))
        sys.exit(1)

    print(f"収集した投稿数: {len(posts)}")
    _check_zero_posts_streak(len(posts))

    candidates = detector.filter_explosive(posts)
    print(f"爆発条件を満たした投稿数: {len(candidates)}")

    # 既に通知済みのpost_idは除外
    new_candidates = [p for p in candidates if not db.is_known(p["post_id"])]
    print(f"未通知の新規候補: {len(new_candidates)}")

    notified_count = 0
    for post in new_candidates:
        classification = genre_classifier.classify(post.get("text_snippet", ""))
        post["genre"] = classification["genre"]

        db.upsert_post(post)

        try:
            success = line_notifier.send_notification(post)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] LINE通知に失敗しました (post_id={post['post_id']}): {e}")
            success = False

        if success:
            db.mark_notified(post["post_id"])
            notified_count += 1
            print(f"  → 通知送信: [{post['genre']}] {post['url']}")

    finished_at = datetime.now(timezone.utc).isoformat()
    db.log_run(started_at, finished_at, len(posts), len(new_candidates), "success")
    print(f"完了。通知送信数: {notified_count}")


if __name__ == "__main__":
    run_once()
