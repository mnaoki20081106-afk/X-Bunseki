"""
main.py
1回分の監視サイクルを実行する。GitHub Actions等から1時間おきに呼び出す想定。

流れ:
  1. Apifyで投稿収集
  2. 「爆発」判定(インプレッション閾値 × 経過時間)
  3. 未通知の候補だけをGroqでジャンル分類
  4. LINEに即時通知
  5. DBに記録(重複通知防止)
"""

import sys
import traceback
from datetime import datetime, timezone

import db
import detector
import genre_classifier
import line_notifier
from apify_collector import fetch_posts


def run_once():
    started_at = datetime.now(timezone.utc).isoformat()
    db.init_db()

    try:
        posts = fetch_posts()
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] データ収集に失敗しました: {e}")
        traceback.print_exc()
        db.log_run(started_at, datetime.now(timezone.utc).isoformat(), 0, 0, "error", str(e))
        sys.exit(1)

    print(f"収集した投稿数: {len(posts)}")

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
