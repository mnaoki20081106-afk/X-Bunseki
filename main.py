"""
main.py (v4)
1回分の監視サイクルを実行する。GitHub Actionsから定期的に呼び出す想定。

════════════════════════════════════════════════════════════
 v4のパイプライン
════════════════════════════════════════════════════════════

  1. 収集    playwright_collector … キーワードから生成した検索クエリで広く集める
  2. 観測記録 db.record_observations … ★収集した「全件」を時系列DBに追記する
  3. 伸び率  growth.compute        … 前回観測との差分から実測の伸び率・加速度を出す
  4. 採点    detector.evaluate     … 0〜100点のスコア1本にまとめる
  5. 除外    NGワード・通知済み・クールダウン
  6. 話題まとめ clustering          … 同じ出来事の投稿を1件に集約する
  7. LLM評価 content_scorer        … 上位数件だけ「動画ネタとして使えるか」を評価
  8. 通知    上限を守って送信
  9. 記録    status.json / data/log/YYYY-MM-DD.jsonl / 古い観測の削除

v3との最大の違いは「2」。
v3は通知した投稿しかDBに入れていなかったため、伸び率を計算する材料が
そもそも存在しなかった。全件を記録して初めて「前回から何件増えたか」が
分かるようになる。
"""

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clustering
import content_scorer
import db
import detector
import keyword_filter
import line_notifier
import notification_text
import pushover_notifier
from playwright_collector import SessionExpiredError, fetch_posts

SESSION_ALERT_COOLDOWN_HOURS = 24
ZERO_POSTS_ALERT_THRESHOLD = 3
ZERO_POSTS_ALERT_COOLDOWN_HOURS = 24

# 観測データの保持期間。伸び率の計算に使うのは直近数時間なので、
# それ以上は消してDBを小さく保つ(GitHub Actionsのキャッシュに載せるため)。
OBSERVATION_KEEP_HOURS = int(os.environ.get("OBSERVATION_KEEP_HOURS") or 24)

# LLM評価にかける上限件数(Groq無料枠のレート制限を守るため)
LLM_MAX_POSTS = int(os.environ.get("LLM_MAX_POSTS") or 3)

BASE_DIR = Path(__file__).parent
STATUS_FILE_PATH = BASE_DIR / "status.json"
LOG_DIR = BASE_DIR / "data" / "log"


# ──────────────────────────────────────────────────────────
#  記録まわり
# ──────────────────────────────────────────────────────────

def _write_status(**kwargs):
    """直近の実行結果を status.json に書き出す(LINEの「動作確認」が読む)"""
    status = {"updated_at": datetime.now(timezone.utc).isoformat(), **kwargs}
    try:
        STATUS_FILE_PATH.write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"status.json を書き出しました (status={status.get('status')})")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] status.json の書き出しに失敗: {e}")


def _append_log(rows: list[dict]):
    """
    スコア上位の投稿を日付ごとのJSONLファイルに追記する。

    ★これは「あとで閾値を調整するための教師データ」になる。
    通知した/しなかったに関わらず上位候補を全部残しておけば、
      - 通知したのに伸びなかった(＝閾値が低すぎる)
      - 通知しなかったが実は伸びた(＝閾値が高すぎる)
    を後から検証できる。docs/grok_prompts.md の「閾値チューニング」で
    このファイルをそのままGrokに貼って分析させる想定。

    status.json と違って上書きではなく追記なので、履歴が消えない。
    """
    if not rows:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"分析用ログに{len(rows)}件追記しました: {path.name}")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 分析用ログの書き出しに失敗: {e}")


def _log_row(post: dict, notified: bool) -> dict:
    g = post.get("growth") or {}
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "post_id": post.get("post_id"),
        "url": post.get("url"),
        "author": post.get("author_handle"),
        "text": (post.get("text_snippet") or "")[:150],
        "age_minutes": g.get("age_minutes"),
        "likes": post.get("likes"),
        "retweets": post.get("retweets"),
        "replies": post.get("replies"),
        "bookmarks": post.get("bookmarks"),
        "impressions": post.get("impressions"),
        "likes_per_min": g.get("likes_per_min"),
        "acceleration": g.get("acceleration"),
        "is_measured": g.get("is_measured"),
        "samples": g.get("samples"),
        "buzz_score": post.get("buzz_score"),
        "score_breakdown": post.get("score_breakdown"),
        "genre": post.get("genre"),
        "tiktok_fit": post.get("tiktok_fit"),
        "scoop_score": post.get("scoop_score"),
        "notified": notified,
    }


# ──────────────────────────────────────────────────────────
#  システムアラート
# ──────────────────────────────────────────────────────────

def _send_system_alert(text: str):
    """セッション切れ等のシステム通知を両ルートに送る"""
    post = notification_text.build_system_message(text)
    for name, notifier in (("LINE", line_notifier), ("Pushover", pushover_notifier)):
        try:
            notifier.send_notification(post)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] システム通知({name})の送信に失敗: {e}")


def _alert_with_cooldown(meta_key: str, cooldown_hours: float, text: str):
    """同じ種類のアラートを連投しないためのクールダウン付き送信"""
    now = datetime.now(timezone.utc)
    last = db.get_meta(meta_key)
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=cooldown_hours):
                print(f"({meta_key} はクールダウン中のためスキップ)")
                return
        except ValueError:
            pass
    _send_system_alert(text)
    db.set_meta(meta_key, now.isoformat())


def _check_zero_posts_streak(posts_count: int):
    """0件が続いたら、Xの画面構造が変わった可能性が高いので知らせる"""
    if posts_count > 0:
        db.set_meta("zero_posts_streak", "0")
        return

    streak = int(db.get_meta("zero_posts_streak") or "0") + 1
    db.set_meta("zero_posts_streak", str(streak))
    print(f"(取得0件が{streak}回連続)")

    if streak < ZERO_POSTS_ALERT_THRESHOLD:
        return

    _alert_with_cooldown(
        "zero_posts_alert_at",
        ZERO_POSTS_ALERT_COOLDOWN_HOURS,
        f"⚠️ {streak}回連続で投稿を0件しか取得できていません。\n"
        "Xのページ構造が変わったか、検索クエリが厳しすぎる可能性があります。",
    )


# ──────────────────────────────────────────────────────────
#  メイン処理
# ──────────────────────────────────────────────────────────

def run_once():
    started_at = datetime.now(timezone.utc)
    started_iso = started_at.isoformat()
    db.init_db()

    print(f"=== 実行開始 {started_iso} ===")
    print(f"判定設定: {detector.config_summary()}")

    # ── 1. 収集 ──
    try:
        posts = fetch_posts()
    except SessionExpiredError as e:
        print(f"[ERROR] {e}")
        db.log_run(started_iso, datetime.now(timezone.utc).isoformat(), 0, 0, "session_expired", str(e))
        _alert_with_cooldown(
            "session_expired_alert_at",
            SESSION_ALERT_COOLDOWN_HOURS,
            "⚠️ Xのログインセッションが切れました。\n"
            "codespace_login.sh を再実行して X_SESSION_STATE を更新してください。\n"
            "https://x.com/login",
        )
        _write_status(status="session_expired", started_at=started_iso,
                      posts_scanned=0, posts_flagged=0, notified_count=0,
                      error_message=str(e))
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] データ収集に失敗しました: {e}")
        traceback.print_exc()
        db.log_run(started_iso, datetime.now(timezone.utc).isoformat(), 0, 0, "error", str(e))
        _write_status(status="error", started_at=started_iso,
                      posts_scanned=0, posts_flagged=0, notified_count=0,
                      error_message=str(e))
        sys.exit(1)

    print(f"収集した投稿数: {len(posts)}")
    _check_zero_posts_streak(len(posts))

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # ── 2. 伸び率の計算 → 3. 観測記録 ──
    # ★順番が重要: 先に過去の履歴を引いてから、今回の値を記録する。
    #   先に記録してしまうと「今回の値」が履歴に混ざり、差分が0になる。
    history_map = db.get_observation_history([p["post_id"] for p in posts])
    measured = sum(1 for h in history_map.values() if h)
    print(f"過去の観測がある投稿: {measured}/{len(posts)}件(この数だけ実測の伸び率が出せる)")

    # ── 4. 採点 ──
    evaluated = detector.evaluate_all(
        posts,
        history_map,
        relevance_fn=lambda p: keyword_filter.relevance_score(p.get("text_snippet", "")),
        now=now,
    )

    db.record_observations(posts, now_iso)

    # 足切りの内訳をログに出す。「なぜ何も鳴らないのか」を毎回追えるようにするため。
    reasons: dict[str, int] = {}
    for p in evaluated:
        if p["rejected_reason"]:
            key = p["rejected_reason"].split("(")[0]
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print(f"足切りの内訳: {reasons}")

    surviving = [p for p in evaluated if not p["rejected_reason"]]
    print(f"足切り通過: {len(surviving)}件")
    for p in surviving[:8]:
        g = p["growth"]
        print(
            f"  {p['buzz_score']:5.1f}点 @{p['author_handle'][:18]:18s} "
            f"いいね{p['likes']:6,} {g['likes_per_min']:6.1f}/分 "
            f"{'実測' if g['is_measured'] else '推定'} "
            f"加速{g['acceleration']} {p['score_breakdown']}"
        )

    candidates = [p for p in surviving if p["should_notify"]]
    print(f"通知スコア({detector.NOTIFY_SCORE:.0f}点)到達: {len(candidates)}件")

    # ── 5. 除外 ──
    filtered = []
    for post in candidates:
        ng = keyword_filter.find_ng_keyword(post.get("text_snippet", ""))
        if ng:
            print(f"  (NGワード'{ng}'のため除外: {post['url']})")
            continue
        if db.is_known(post["post_id"]):
            continue
        last = db.get_last_notified_at_for_author(post.get("author_handle", ""))
        if detector.is_in_cooldown(post.get("author_handle", ""), last, now=now):
            print(f"  (アカウントクールダウン中: @{post['author_handle']})")
            continue
        filtered.append(post)

    # ── 6. 話題ごとにまとめる ──
    # 同じ地震で8件鳴るような事態を防ぐ。1話題=1通知。
    representatives = clustering.pick_representatives(filtered)
    if len(filtered) != len(representatives):
        print(f"話題まとめ: {len(filtered)}件 → {len(representatives)}話題")

    # 直近に通知した話題と似ているものを除外する
    recent_texts = db.recent_notified_texts(hours=detector.TOPIC_COOLDOWN_HOURS)
    fresh_topics = []
    for post in representatives:
        similar = clustering.is_similar_to_any(post.get("text_snippet", ""), recent_texts)
        if similar:
            print(f"  (直近に同じ話題を通知済みのため除外: {post['url']})")
            continue
        fresh_topics.append(post)

    # ── 通知数の上限 ──
    day_start = (now - timedelta(hours=24)).isoformat()
    sent_today = db.count_notifications_since(day_start)
    remaining_today = max(detector.NOTIFY_MAX_PER_DAY - sent_today, 0)
    limit = min(detector.NOTIFY_MAX_PER_RUN, remaining_today)
    if remaining_today == 0 and fresh_topics:
        print(f"[上限] 直近24時間で既に{sent_today}件通知しているため、今回は送信しません")
    to_notify = fresh_topics[:limit]

    # ── 7. LLMで「動画ネタとして使えるか」を評価 ──
    to_notify = content_scorer.enrich(to_notify, limit=LLM_MAX_POSTS)

    # ── 8. 通知 ──
    notified_count = 0
    notified_ids: set[str] = set()
    for post in to_notify:
        db.upsert_post(post)

        results = {}
        for name, notifier in (("LINE", line_notifier), ("Pushover", pushover_notifier)):
            try:
                results[name] = notifier.send_notification(post)
            except Exception as e:  # noqa: BLE001
                print(f"[ERROR] {name}通知に失敗 (post_id={post['post_id']}): {e}")
                results[name] = False

        if any(results.values()):
            db.mark_notified(post["post_id"])
            db.record_notified_topic(post["post_id"], post.get("text_snippet", ""), now_iso)
            db.set_last_notified_at_for_author(post.get("author_handle", ""), now_iso)
            notified_count += 1
            notified_ids.add(post["post_id"])
            ok = "/".join(k for k, v in results.items() if v)
            print(f"  → 通知送信({ok}): {post['buzz_score']:.0f}点 {post['url']}")

    # ── 9. 記録 ──
    # 分析用ログには「スコア上位10件」と「実際に通知した投稿」の両方を残す。
    # 前者は閾値の当たり外れを検証するため、後者は必ず追跡できるようにするため。
    log_targets = list(surviving[:10])
    logged_ids = {p["post_id"] for p in log_targets}
    log_targets += [p for p in to_notify if p["post_id"] not in logged_ids]
    _append_log([_log_row(p, p["post_id"] in notified_ids) for p in log_targets])

    deleted = db.prune_observations(keep_hours=OBSERVATION_KEEP_HOURS)
    stats = db.observation_stats()
    print(f"観測DB: {stats.get('posts')}投稿 / {stats.get('total')}レコード(古い{deleted}件を削除)")

    finished_at = datetime.now(timezone.utc).isoformat()
    db.log_run(started_iso, finished_at, len(posts), len(candidates), "success")
    print(f"完了。通知送信数: {notified_count}")

    _write_status(
        status="success",
        started_at=started_iso,
        finished_at=finished_at,
        posts_scanned=len(posts),
        posts_with_history=measured,
        posts_flagged=len(candidates),
        notified_count=notified_count,
        notified_last_24h=sent_today + notified_count,
        reject_reasons=reasons,
        config=detector.config_summary(),
        observation_db=stats,
        top5=[
            {
                "author": p.get("author_handle"),
                "score": p.get("buzz_score"),
                "likes": p.get("likes"),
                "likes_per_min": (p.get("growth") or {}).get("likes_per_min"),
                "acceleration": (p.get("growth") or {}).get("acceleration"),
                "measured": (p.get("growth") or {}).get("is_measured"),
                "age_minutes": (p.get("growth") or {}).get("age_minutes"),
                "text": (p.get("text_snippet") or "")[:60],
                "url": p.get("url", ""),
            }
            for p in surviving[:5]
        ],
    )


def run_test_notification():
    """
    「通知確認」用のテストモード。
    実際にXを検索せず、ダミーデータで通知ルートの疎通だけを確認する。
    """
    print("=== テスト通知モード ===")

    test_post = {
        "post_id": "0",
        "author_handle": "test_account",
        "url": "https://x.com/example/status/000000000",
        "text_snippet": "【速報】これはテスト通知です。実際の投稿ではありません。",
        "likes": 3200, "retweets": 640, "replies": 310,
        "bookmarks": 420, "impressions": 180000,
        "elapsed_minutes": 34,
        "buzz_score": 78.4,
        "is_gekiatsu": True,
        "score_breakdown": {"伸び率": 31.2, "加速度": 12.0, "議論量": 15.0, "保存率": 8.7, "拡散率": 8.0, "関連度": 6.0},
        "growth": {
            "likes_per_min": 94.1, "acceleration": 1.6,
            "is_measured": True, "window_minutes": 15.0, "age_minutes": 34,
        },
        "genre": "事件・事故",
        "summary": "テスト用のダミー要約です",
        "hook": "これ、まだニュースになってません",
        "title": "【速報】テスト通知",
        "scoop_score": 8, "explain_score": 7, "tiktok_fit": 9, "risk": "低",
        "llm_ok": True,
    }

    print("--- 送信する本文プレビュー ---")
    print(notification_text.build_message(test_post))
    print("-----------------------------")

    results = {}
    for name, notifier in (("LINE", line_notifier), ("Pushover", pushover_notifier)):
        try:
            results[name] = notifier.send_notification(test_post)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] {name}テスト通知に失敗: {e}")
            results[name] = False

    print(f"テスト通知結果: {results}")

    summary = "📋 通知確認テスト結果\n" + "\n".join(
        f"{'✅' if ok else '❌'} {name}" for name, ok in results.items()
    )
    try:
        line_notifier.send_notification(notification_text.build_system_message(summary))
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] テスト結果サマリーの送信に失敗: {e}")


if __name__ == "__main__":
    if os.environ.get("TEST_NOTIFICATION") == "true":
        run_test_notification()
    else:
        run_once()
