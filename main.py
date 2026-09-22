"""
main.py (v4)
1回分の監視サイクルを実行する。GitHub Actionsから定期的に呼び出す想定。

2026-09: 通知無効。条件通過投稿は hits.md / hits.json に一覧出力。
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
import watchlist
from playwright_collector import SessionExpiredError, fetch_posts

SESSION_ALERT_COOLDOWN_HOURS = 24
ZERO_POSTS_ALERT_THRESHOLD = 3
ZERO_POSTS_ALERT_COOLDOWN_HOURS = 24
OBSERVATION_KEEP_HOURS = int(os.environ.get("OBSERVATION_KEEP_HOURS") or 24)
LLM_MAX_POSTS = int(os.environ.get("LLM_MAX_POSTS") or 3)

BASE_DIR = Path(__file__).parent
STATUS_FILE_PATH = BASE_DIR / "status.json"
HITS_FILE_PATH = BASE_DIR / "hits.md"
HITS_JSON_PATH = BASE_DIR / "hits.json"
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


def _validate_session_file() -> str | None:
    """storage_state が壊れていればエラーメッセージを返す。"""
    path = Path(os.environ.get("X_SESSION_STATE_PATH") or "storage_state.json")
    if not path.exists():
        return f"セッションファイルがありません: {path}"
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return f"セッションファイルがUTF-8ではありません（壊れた base64 の可能性）: {e}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"セッションファイルがJSONではありません: {e}"
    if not isinstance(data, dict):
        return "セッションファイルのルートがオブジェクトではありません"
    cookies = data.get("cookies")
    if cookies is not None and not isinstance(cookies, list):
        return "cookies が配列ではありません"
    if isinstance(cookies, list) and len(cookies) == 0 and not data.get("origins"):
        return "セッションに cookies が空です。X_SESSION_STATE を再発行してください"
    return None


def _write_hits(surviving: list[dict], candidates: list[dict], started_iso: str, watch_state: dict | None = None):
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
        text = (p.get("text_snippet") or "(文なし/画像など)").replace("|", "\\|").replace("\n", " ")
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

    lines += ["", "---", "★ = 通知スコア到達。通知送信は無効化中（記録のみ）。", ""]
    try:
        HITS_FILE_PATH.write_text("\n".join(lines), encoding="utf-8")
        print(f"hits.md を書き出しました（通過{len(surviving)} / 候補{len(candidates)}）")
    except Exception as e:
        print(f"[ERROR] hits.md の書き出しに失敗: {e}")

    # Two views: early discovery (0-4h) and currently viral (4-24h).
    # Trending is backed by the persistent watchlist so a post does not disappear
    # merely because it aged past the early-discovery window.
    early = [p for p in surviving if float((p.get("growth") or {}).get("age_minutes") or 0) <= 240]
    early.sort(key=lambda p: (p.get("predicted_final_impressions") or 0, p.get("buzz_score") or 0), reverse=True)

    trending = []
    for row in (watch_state or {}).values():
        age = float(row.get("last_age_minutes") or 0)
        imp = int(row.get("last_impressions") or 0)
        ipm = float(row.get("last_impressions_per_min") or 0)
        mega = age <= 960 and imp >= 8_500_000
        if not (240 < age <= 1440 and (imp >= 1_000_000 or ipm >= 300 or mega)):
            continue
        trending.append({
            "author": row.get("author_handle"),
            "score": row.get("last_buzz_score"),
            "likes": row.get("last_likes"),
            "impressions": imp,
            "predicted_final_impressions": row.get("last_predicted_final_impressions"),
            "prediction_confidence": row.get("last_prediction_confidence"),
            "impressions_per_min": row.get("last_impressions_per_min"),
            "impressions_acceleration": row.get("last_impressions_acceleration"),
            "replies": row.get("last_replies"),
            "retweets": row.get("last_retweets"),
            "bookmarks": row.get("last_bookmarks"),
            "likes_per_min": row.get("last_likes_per_min"),
            "age_minutes": age,
            "candidate": True,
            "discovery_source": row.get("discovery_source") or "watchlist",
            "million_imp_bypass": bool(row.get("million_imp_bypass")),
            "million_imp_bypass_detail": row.get("million_imp_bypass_detail") or "",
            "mega_viral": mega,
            "text": (row.get("text_snippet") or "")[:120],
            "url": row.get("url") or "",
        })
    trending.sort(key=lambda p: (p.get("impressions_per_min") or 0, p.get("impressions") or 0), reverse=True)

    def _public_post(p):
        return {
            "author": p.get("author_handle"),
            "score": p.get("buzz_score"),
            "likes": p.get("likes"),
            "impressions": p.get("impressions"),
            "predicted_final_impressions": p.get("predicted_final_impressions"),
            "prediction_confidence": p.get("prediction_confidence"),
            "impressions_per_min": (p.get("growth") or {}).get("impressions_per_min"),
            "impressions_acceleration": (p.get("growth") or {}).get("impressions_acceleration"),
            "replies": p.get("replies"),
            "retweets": p.get("retweets"),
            "bookmarks": p.get("bookmarks"),
            "likes_per_min": (p.get("growth") or {}).get("likes_per_min"),
            "age_minutes": (p.get("growth") or {}).get("age_minutes"),
            "candidate": bool(p.get("should_notify")),
            "discovery_source": p.get("discovery_source") or "keyword",
            "million_imp_bypass": bool(p.get("million_imp_bypass")),
            "million_imp_bypass_detail": p.get("million_imp_bypass_detail") or "",
            "mega_viral": float((p.get("growth") or {}).get("age_minutes") or 9999) <= 960 and int(p.get("impressions") or 0) >= 8_500_000,
            "text": (p.get("text_snippet") or "")[:120],
            "url": p.get("url") or "",
        }

    payload = {
        "updated_at": started_iso,
        "surviving": len(surviving),
        "candidates": len(candidates),
        "notify_score": detector.NOTIFY_SCORE,
        "early_posts": [_public_post(p) for p in early[:30]],
        "trending_posts": trending[:50],
        # Backward compatibility for older clients.
        "posts": [_public_post(p) for p in early[:30]],
    }
    try: