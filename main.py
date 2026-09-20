"""
main.py (v4)
1回分の監視サイクルを実行する。GitHub Actionsから定期的に呼び出す想定。

2026-09: 通知無効。条件通過投稿は hits.md に一覧出力。
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
import notify_state
import pushover_notifier
from playwright_collector import SessionExpiredError, fetch_posts

SESSION_ALERT_COOLDOWN_HOURS = 24
ZERO_POSTS_ALERT_THRESHOLD = 3
ZERO_POSTS_ALERT_COOLDOWN_HOURS = 24
OBSERVATION_KEEP_HOURS = int(os.environ.get("OBSERVATION_KEEP_HOURS") or 24)
LLM_MAX_POSTS = int(os.environ.get("LLM_MAX_POSTS") or 3)

BASE_DIR = Path(__file__).parent
STATUS_FILE_PATH = BASE_DIR / "status.json"
HITS_FILE_PATH = BASE_DIR / "hits.md"
LOG_DIR = BASE_DIR / "data" / "log"


def _write_status(**kwargs):
    status = {"updated_at": datetime.now(timezone.utc).isoformat(), **kwargs}
    try:
        STATUS_FILE_PATH.write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"status.json を書き出しました (status={status.get('status')})")
    except Exception as e:
        print(f"[ERROR] status.json の書き出しに失敗: {e}")


def _write_hits(surviving: list[dict], candidates: list[dict], started_iso: str):
    """条件を満たした投稿を Markdown 一覧で見られるようにする。"""
    lines = [
        "# 条件通過ポスト一覧",
        "",
        f"更新: `{started_iso}`（UTC）",
        "",
        f"- 足切り通過: **{len(surviving)}** 件",
        f"- スコア {detector.NOTIFY_SCORE:.0f} 点以上: **{len(candidates)}** 件",
        "",
        "判定の主軸は数値（分速・返信・RT・引用・加速度）。キーワードは収集の参考のみ。",
        "",
        "## スコア順（上位30）",
        "",
        "| 点 | 経過 | いいね | /分 | 返信 | RT | 加速 | 投稿 |",
        "|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for p in surviving[:30]:
        g = p.get("growth") or {}
        age = g.get("age_minutes")
        age_s = f"{age:.0f}m" if age is not None else "-"
        lpm = g.get("likes_per_min")
        lpm_s = f"{lpm:.1f}" if lpm is not None else "-"
        accel = g.get("acceleration")
        accel_s = f"{accel:.2f}" if accel is not None else "-"
        text = (p.get("text_snippet") or "(文なし/画像など)").replace("|", "\|").replace("\n", " ")
        text = text[:80]
        url = p.get("url") or ""
        author = p.get("author_handle") or ""
        flag = " ★" if p.get("should_notify") else ""
        lines.append(
            f"| **{p.get('buzz_score', 0):.0f}**{flag} | {age_s} | "
            f"{p.get('likes') or 0:,} | {lpm_s} | {p.get('replies') or 0:,} | "
            f"{p.get('retweets') or 0:,} | {accel_s} | "
            f"[@{author}]({url}) {text} |"
        )

    if candidates:
        lines += ["", "## 候補（スコア到達）", ""]
        for p in candidates[:15]:
            g = p.get("growth") or {}
            lines.append(
                f"- **{p.get('buzz_score', 0):.0f}点** "
                f"[ @{p.get('author_handle')} ]({p.get('url')}) "
                f"いいね{p.get('likes') or 0:,} / "
                f"{g.get('likes_per_min', 0):.1f}/分 / "
                f"経過{g.get('age_minutes', 0):.0f}分  "
                f"{(p.get('text_snippet') or '')[:100]}"
            )
    else:
        lines += ["", "## 候補", "", "（今回スコア到達なし）", ""]

    lines += [
        "",
        "---",
        "★ = 通知スコア到達。通知送信は無効化中（記録のみ）。",
        "",
    ]
    try:
        HITS_FILE_PATH.write_text("\n".join(lines), encoding="utf-8")
        print(f"hits.md を書き出しました（通過{len(surviving)} / 候補{len(candidates)}）")
    except Exception as e:
        print(f"[ERROR] hits.md の書き出しに失敗: {e}")


def _append_log(rows: list[dict]):
    if not rows:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"分析用ログに{len(rows)}件追記しました: {path.name}")
    except Exception as e:
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


def _send_system_alert(text: str):
    print(f"[SYSTEM ALERT] {text}")


def _alert_with_cooldown(meta_key: str, cooldown_hours: float, text: str):
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


def run_once():
    started_at = datetime.now(timezone.utc)
    started_iso = started_at.isoformat()
    db.init_db()

    print(f"=== 実行開始 {started_iso} ===")
    print(f"判定設定: {detector.config_summary()}")

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
    except Exception as e:
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

    history_map = db.get_observation_history([p["post_id"] for p in posts])
    measured = sum(1 for h in history_map.values() if h)
    print(f"過去の観測がある投稿: {measured}/{len(posts)}件")

    evaluated = detector.evaluate_all(
        posts,
        history_map,
        relevance_fn=lambda p: keyword_filter.relevance_score(p.get("text_snippet", "")),
        now=now,
    )

    db.record_observations(posts, now_iso)

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

    # 条件通過一覧（GitHub上で開いて見られる）
    _write_hits(surviving, candidates, started_iso)

    state = notify_state.NotifyState.load()

    filtered = []
    for post in candidates:
        ng = keyword_filter.find_ng_keyword(post.get("text_snippet", ""))
        if ng:
            print(f"  (NGワード'{ng}'のため除外: {post['url']})")
            continue
        if state.is_notified(post["post_id"]):
            continue
        last = state.last_notified_at_for_author(post.get("author_handle", ""))
        if detector.is_in_cooldown(post.get("author_handle", ""), last, now=now):
            print(f"  (アカウントクールダウン中: @{post['author_handle']})")
            continue
        filtered.append(post)

    representatives = clustering.pick_representatives(filtered)
    if len(filtered) != len(representatives):
        print(f"話題まとめ: {len(filtered)}件 → {len(representatives)}話題")

    recent_texts = state.recent_texts(hours=detector.TOPIC_COOLDOWN_HOURS)
    fresh_topics = []
    for post in representatives:
        similar = clustering.is_similar_to_any(post.get("text_snippet", ""), recent_texts)
        if similar:
            print(f"  (直近に同じ話題を通知済みのため除外: {post['url']})")
            continue
        fresh_topics.append(post)

    sent_today = state.count_last_24h()
    remaining_today = max(detector.NOTIFY_MAX_PER_DAY - sent_today, 0)
    limit = min(detector.NOTIFY_MAX_PER_RUN, remaining_today)

    since_last = state.minutes_since_last()
    if remaining_today == 0 and fresh_topics:
        print(f"[上限] 直近24時間で既に{sent_today}件通知しているため、今回は送信しません")
        limit = 0

    to_notify = []
    for post in fresh_topics[:limit]:
        allowed, reason = detector.can_notify_now(post["buzz_score"], since_last)
        if not allowed:
            print(f"[間隔] {post['buzz_score']:.0f}点の投稿を見送ります: {reason}")
            continue
        to_notify.append(post)

    to_notify = content_scorer.enrich(to_notify, limit=LLM_MAX_POSTS)

    notified_count = 0
    notified_ids: set[str] = set()
    for post in to_notify:
        db.upsert_post(post)
        for name, notifier in (("LINE", line_notifier), ("Pushover", pushover_notifier)):
            try:
                notifier.send_notification(post)
            except Exception as e:
                print(f"[ERROR] {name}通知に失敗 (post_id={post['post_id']}): {e}")
        db.mark_notified(post["post_id"])
        state.record(post)
        notified_count += 1
        notified_ids.add(post["post_id"])
        print(f"  → 候補記録(通知無効): {post['buzz_score']:.0f}点 {post['url']}")

    log_targets = list(surviving[:10])
    logged_ids = {p["post_id"] for p in log_targets}
    log_targets += [p for p in to_notify if p["post_id"] not in logged_ids]
    _append_log([_log_row(p, p["post_id"] in notified_ids) for p in log_targets])

    if notified_count:
        state.save()

    deleted = db.prune_observations(keep_hours=OBSERVATION_KEEP_HOURS)
    stats = db.observation_stats()
    print(f"観測DB: {stats.get('posts')}投稿 / {stats.get('total')}レコード(古い{deleted}件を削除)")

    finished_at = datetime.now(timezone.utc).isoformat()
    db.log_run(started_iso, finished_at, len(posts), len(candidates), "success")

    print(
        f"\n[まとめ] 収集{len(posts)} → 足切り通過{len(surviving)} "
        f"→ スコア{detector.NOTIFY_SCORE:.0f}点到達{len(candidates)} "
        f"→ 話題まとめ{len(representatives)} → 候補{notified_count}"
        f"  (24時間で{sent_today + notified_count}/{detector.NOTIFY_MAX_PER_DAY}件)"
    )

    _write_status(
        status="success",
        started_at=started_iso,
        finished_at=finished_at,
        posts_scanned=len(posts),
        posts_with_history=measured,
        posts_flagged=len(candidates),
        notified_count=notified_count,
        notified_last_24h=sent_today + notified_count,
        minutes_since_last_notification=(
            None if since_last is None else round(since_last)
        ),
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
    print("=== テスト通知モード ===")
    print("通知は無効化されています(LINE / Pushover 送信しません)")


if __name__ == "__main__":
    if os.environ.get("TEST_NOTIFICATION") == "true":
        run_test_notification()
    else:
        run_once()
