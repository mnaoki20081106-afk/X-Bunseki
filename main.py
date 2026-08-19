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

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import detector
import genre_classifier
import keyword_filter
import line_notifier
import ntfy_notifier
import email_notifier
from playwright_collector import SessionExpiredError, fetch_posts

SESSION_ALERT_COOLDOWN_HOURS = 24  # セッション切れ通知は1日1回まで
ZERO_POSTS_ALERT_THRESHOLD = 3     # 何回連続で0件だったら「壊れてるかも」と通知するか
ZERO_POSTS_ALERT_COOLDOWN_HOURS = 24  # DOM構造変化アラートも1日1回まで

STATUS_FILE_PATH = Path(__file__).parent / "status.json"


def _write_status(**kwargs):
    """
    「動作確認」用に、直近の実行結果をstatus.jsonへ書き出す。
    このファイルはGitHub Actionsのワークフロー側でリポジトリにcommit&pushされ、
    Cloudflare Workerがそれを読みに行くことで「動作確認」に応答する仕組み。
    """
    status = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }
    try:
        STATUS_FILE_PATH.write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"status.json を書き出しました: {status}")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] status.json の書き出しに失敗: {e}")


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

    system_post = {
        "genre": "システム通知",
        "likes": 0, "quotes": 0, "bookmarks": 0, "elapsed_hours": "-",
        "url": "https://x.com/login",
        "author_handle": "⚠️ ログインセッションが切れました。codespace_login.sh を再実行してください",
    }
    try:
        line_notifier.send_notification(system_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] セッション切れ通知(LINE)の送信に失敗: {e}")
    try:
        ntfy_notifier.send_notification(system_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] セッション切れ通知(ntfy)の送信に失敗: {e}")
    try:
        email_notifier.send_notification(system_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] セッション切れ通知(メール)の送信に失敗: {e}")

    db.set_meta("session_expired_alert_at", now.isoformat())
    print("セッション切れ通知を送信しました")


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

    system_post = {
        "genre": "システム通知",
        "likes": 0, "quotes": 0, "bookmarks": 0, "elapsed_hours": "-",
        "url": "https://x.com",
        "author_handle": (
            f"⚠️ {streak}回連続で投稿を0件しか取得できていません。"
            "Xのページ構造が変わった可能性があります(要確認)"
        ),
    }
    try:
        line_notifier.send_notification(system_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 0件連続アラート(LINE)の送信に失敗: {e}")
    try:
        ntfy_notifier.send_notification(system_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 0件連続アラート(ntfy)の送信に失敗: {e}")
    try:
        email_notifier.send_notification(system_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 0件連続アラート(メール)の送信に失敗: {e}")

    db.set_meta("zero_posts_alert_at", now.isoformat())
    print("0件連続アラートを送信しました")


def run_once():
    started_at = datetime.now(timezone.utc).isoformat()
    db.init_db()

    try:
        posts = fetch_posts()
    except SessionExpiredError as e:
        print(f"[ERROR] {e}")
        db.log_run(started_at, datetime.now(timezone.utc).isoformat(), 0, 0, "session_expired", str(e))
        _notify_session_expired()
        _write_status(
            status="session_expired",
            started_at=started_at,
            posts_scanned=0,
            posts_flagged=0,
            notified_count=0,
            error_message=str(e),
        )
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] データ収集に失敗しました: {e}")
        traceback.print_exc()
        db.log_run(started_at, datetime.now(timezone.utc).isoformat(), 0, 0, "error", str(e))
        _write_status(
            status="error",
            started_at=started_at,
            posts_scanned=0,
            posts_flagged=0,
            notified_count=0,
            error_message=str(e),
        )
        sys.exit(1)

    print(f"収集した投稿数: {len(posts)}")
    _check_zero_posts_streak(len(posts))

    # ★キーワードフィルター(ホワイトリスト方式)をここで明示的に適用する。
    # playwright_collector.py側のキーワードフィルターは「詳細ページを
    # 取得する候補を絞る」ためだけのものであり、判定ロジック
    # (detector.filter_explosive)にはフィルタされていない生の投稿が
    # そのまま渡ってしまっていた。
    # 特に「瞬間バズ」判定は引用・ブックマーク・表示回数を必要とせず、
    # 検索段階のいいね・RT・返信だけで成立してしまうため、キーワードに
    # 一致しない投稿(広告・関係ないジャンルのファン投稿等)でも
    # 通知されてしまう不具合があった。ここで確実に絞り込む。
    #
    # ★診断用: 実際にマッチしたキーワードを post["matched_keyword"] に
    # 記録し、通知本文にも含める。これにより「本当にキーワードフィルターが
    # 機能しているか」を、届いた通知そのものを見るだけで検証できるようにする。
    posts_matching_keywords = []
    for p in posts:
        text = p.get("text_snippet", "")
        matched = keyword_filter.find_matching_keyword(text)
        if matched:
            p["matched_keyword"] = matched
            posts_matching_keywords.append(p)
        elif keyword_filter.matches_keyword(text):
            # キーワード自体が0件登録(フィルタ無効)の場合はここに来る
            p["matched_keyword"] = "(フィルタ無効・全件通過)"
            posts_matching_keywords.append(p)
    print(f"キーワードフィルター通過後: {len(posts_matching_keywords)}件")

    candidates = detector.filter_explosive(posts_matching_keywords)
    print(f"爆発条件を満たした投稿数: {len(candidates)}")

    # 既に通知済みのpost_idは除外
    new_candidates = [p for p in candidates if not db.is_known(p["post_id"])]
    print(f"未通知の新規候補: {len(new_candidates)}")

    # アカウント単位のクールダウン(同じ投稿者から立て続けに通知しない)
    filtered_candidates = []
    for post in new_candidates:
        author = post.get("author_handle", "")
        last_notified = db.get_last_notified_at_for_author(author)
        if detector.is_in_cooldown(author, last_notified):
            print(f"  (クールダウン中のためスキップ: @{author})")
            continue
        filtered_candidates.append(post)
    new_candidates = filtered_candidates
    print(f"クールダウン適用後の候補: {len(new_candidates)}")

    notified_count = 0
    for post in new_candidates:
        classification = genre_classifier.classify(post.get("text_snippet", ""))
        post["genre"] = classification["genre"]

        # ★診断ログ: 通知する直前に、マッチしたキーワードと本文の冒頭を必ず出力する。
        # 「キーワードに関係ない投稿が通知された」という報告があった際に、
        # このログを見れば実際にマッチしたキーワードが何だったのか、
        # 本文に本当に含まれていたのかがその場で検証できる。
        print(
            f"  [通知前チェック] post_id={post['post_id']}, "
            f"matched_keyword={post.get('matched_keyword')!r}, "
            f"text_snippet={post.get('text_snippet', '')[:100]!r}"
        )

        db.upsert_post(post)

        try:
            line_success = line_notifier.send_notification(post)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] LINE通知に失敗しました (post_id={post['post_id']}): {e}")
            line_success = False

        try:
            ntfy_success = ntfy_notifier.send_notification(post)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] ntfy通知に失敗しました (post_id={post['post_id']}): {e}")
            ntfy_success = False

        try:
            email_success = email_notifier.send_notification(post)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] メール通知に失敗しました (post_id={post['post_id']}): {e}")
            email_success = False

        success = line_success or ntfy_success or email_success
        if success:
            db.mark_notified(post["post_id"])
            db.set_last_notified_at_for_author(
                post.get("author_handle", ""), datetime.now(timezone.utc).isoformat()
            )
            notified_count += 1
            channels = []
            if line_success:
                channels.append("LINE")
            if ntfy_success:
                channels.append("ntfy")
            if email_success:
                channels.append("メール")
            print(f"  → 通知送信({'/'.join(channels)}): [{post['genre']}] {post['url']}")

    finished_at = datetime.now(timezone.utc).isoformat()
    db.log_run(started_at, finished_at, len(posts), len(new_candidates), "success")
    print(f"完了。通知送信数: {notified_count}")

    # 「動作確認」用に、2種類のランキングを記録しておく。
    # どちらも posts_matching_keywords(キーワードフィルター通過後)を
    # 対象にする。以前は top5_by_likes だけ全件(フィルタ前)を使っていたが、
    # キーワードに関係ない投稿が混ざって分かりにくいとの指摘を受けて統一した。
    # - top5_by_likes: いいね数が多い順(話題になっている投稿の全体像)
    # - top5_by_progress: 通知条件への「達成度」が高い順(早期発見の目安)
    # URLも添付し、実際の投稿をすぐ確認できるようにしている。
    top5_likes = sorted(posts_matching_keywords, key=lambda p: p.get("likes", 0), reverse=True)[:5]
    top5_by_likes = [
        {
            "author": p.get("author_handle"),
            "likes": p.get("likes", 0),
            "quotes": p.get("quotes", 0),
            "bookmarks": p.get("bookmarks", 0),
            "url": p.get("url", ""),
        }
        for p in top5_likes
    ]

    top5_progress = detector.rank_by_progress(posts_matching_keywords, limit=5)
    top5_by_progress = [
        {
            "author": p.get("author_handle"),
            "likes": p.get("likes", 0),
            "quotes": p.get("quotes", 0),
            "bookmarks": p.get("bookmarks", 0),
            "progress_percent": round(p.get("progress", 0) * 100),
            "url": p.get("url", ""),
        }
        for p in top5_progress
    ]

    _write_status(
        status="success",
        started_at=started_at,
        finished_at=finished_at,
        posts_scanned=len(posts),
        posts_flagged=len(new_candidates),
        notified_count=notified_count,
        top5_by_likes=top5_by_likes,
        top5_by_progress=top5_by_progress,
    )


def run_test_notification():
    """
    「通知確認」用のテストモード。実際にXを検索せず、テスト用の
    ダミー投稿データでLINE・ntfy・メールの3ルート全部に送信を試みる。
    「通知条件の判定ロジックは正しいが、通知の送信自体が失敗している」
    ケースを、実際にX検索を待たずすぐに確認できるようにするため。
    """
    print("=== テスト通知モード ===")

    test_post = {
        "genre": "テスト",
        "likes": 12345,
        "quotes": 100,
        "bookmarks": 300,
        "elapsed_hours": 0.5,
        "url": "https://x.com/example/status/000000000",
        "author_handle": "test_account",
        "is_gekiatsu": True,
        "quote_like_ratio": 0.15,
        "bookmark_like_ratio": 0.25,
    }

    results = {}

    try:
        results["LINE"] = line_notifier.send_notification(test_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] LINEテスト通知に失敗: {e}")
        results["LINE"] = False

    try:
        results["ntfy"] = ntfy_notifier.send_notification(test_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] ntfyテスト通知に失敗: {e}")
        results["ntfy"] = False

    try:
        results["メール"] = email_notifier.send_notification(test_post)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] メールテスト通知に失敗: {e}")
        results["メール"] = False

    print(f"テスト通知結果: {results}")

    # 結果自体もLINEに送っておく(どのルートが失敗したか一目で分かるように)
    summary_lines = ["📋 通知確認テスト結果"]
    for channel, success in results.items():
        mark = "✅" if success else "❌"
        summary_lines.append(f"{mark} {channel}")
    summary_text = "\n".join(summary_lines)

    try:
        line_notifier.send_notification({
            "genre": "システム通知",
            "likes": 0, "quotes": 0, "bookmarks": 0, "elapsed_hours": "-",
            "url": "",
            "author_handle": summary_text,
        })
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] テスト結果サマリーの送信に失敗: {e}")


if __name__ == "__main__":
    if os.environ.get("TEST_NOTIFICATION") == "true":
        run_test_notification()
    else:
        run_once()
