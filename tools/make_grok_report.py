"""
tools/make_grok_report.py
data/log/*.jsonl を、Grokにそのまま貼れる要約レポートに変換する。

★なぜ必要か:
  15分間隔で動かすと、分析用ログは1日あたり約1,000行(500KB前後)になる。
  これをiPadでコピーしてGrokに貼るのは現実的ではないし、
  生ログをそのまま渡してもGrokは各行が何を意味するのか分からない。

  このスクリプトは、
    - 同じ投稿の複数回の観測を1本の「軌跡」にまとめ
    - 「最終的に伸びたか」という答え合わせの結果を付け
    - フィールドの意味とスコアの計算式を添えた
  レポートを作る。これならA4数枚分に収まり、貼るだけで分析が始められる。

★このレポートが答える問い:
  「スコアが高かった投稿は、実際にその後伸びたのか?」
  これが分からない限り、閾値をいくら動かしても改善したか判定できない。

実行方法:
  ローカル : python3 tools/make_grok_report.py
  iPadのみ : GitHubのActionsタブ →「Grok用レポート作成」→ Run workflow
             → 完了後 data/grok_report.md を開いて全文コピー
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "data" / "log"
OUTPUT_PATH = BASE_DIR / "data" / "grok_report.md"

# レポートに載せる投稿の上限(多すぎると貼れなくなるため)
MAX_POSTS_IN_REPORT = 60

# 「結局バズった」とみなすいいね数の目安。ジャンルによって適正値は違うので
# --viral-threshold で変更できる。
DEFAULT_VIRAL_LIKES = 5000


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def load_rows(days: int) -> list[dict]:
    """直近days日分のログを読み込む"""
    if not LOG_DIR.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for path in sorted(LOG_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            observed = _parse_dt(row.get("observed_at"))
            if observed and observed < cutoff:
                continue
            rows.append(row)
    return rows


def build_tracks(rows: list[dict]) -> list[dict]:
    """
    同じ post_id の観測をまとめて、1投稿1本の「軌跡」にする。
    ここが分析の肝で、点の集まりを線に変えることで
    「スコアを付けた時点」と「その後どうなったか」を対応させられる。
    """
    grouped = defaultdict(list)
    for row in rows:
        if row.get("post_id"):
            grouped[row["post_id"]].append(row)

    tracks = []
    for post_id, observations in grouped.items():
        observations.sort(key=lambda r: r.get("age_minutes") or 0)
        first, last = observations[0], observations[-1]

        likes_values = [o.get("likes") or 0 for o in observations]
        scores = [o.get("buzz_score") or 0 for o in observations]

        tracks.append({
            "post_id": post_id,
            "url": last.get("url", ""),
            "author": last.get("author", ""),
            "text": (last.get("text") or "").replace("\n", " ")[:70],
            "genre": last.get("genre") or "-",
            "observations": observations,
            "samples": len(observations),
            "first_age": first.get("age_minutes"),
            "first_likes": first.get("likes") or 0,
            "first_score": first.get("buzz_score") or 0,
            "max_score": max(scores),
            "final_likes": max(likes_values),
            "final_retweets": max((o.get("retweets") or 0) for o in observations),
            "final_replies": max((o.get("replies") or 0) for o in observations),
            "final_bookmarks": max((o.get("bookmarks") or 0) for o in observations),
            "notified": any(o.get("notified") for o in observations),
            "measured": any(o.get("is_measured") for o in observations),
        })

    tracks.sort(key=lambda t: t["final_likes"], reverse=True)
    return tracks


def format_trajectory(observations: list[dict]) -> str:
    """「22分:900いいね → 37分:4,200 → 52分:9,800」という形の軌跡文字列"""
    parts = []
    for o in observations[:8]:
        age = o.get("age_minutes")
        age_text = f"{age:.0f}分" if isinstance(age, (int, float)) else "?分"
        parts.append(f"{age_text}:{(o.get('likes') or 0):,}")
    if len(observations) > 8:
        parts.append("…")
    return " → ".join(parts)


def score_buckets(tracks: list[dict], viral_likes: int) -> list[dict]:
    """
    スコア帯ごとに「その後どれだけ伸びたか」を集計する。
    ★これが閾値決定の直接の根拠になる表。
    スコアが予測力を持っているなら、上の帯ほど最終いいね数の中央値が高くなるはず。
    """
    bounds = [(0, 40), (40, 55), (55, 70), (70, 101)]
    result = []
    for low, high in bounds:
        group = [t for t in tracks if low <= t["max_score"] < high]
        if not group:
            result.append({"range": f"{low}〜{high - 1}点", "count": 0})
            continue
        finals = sorted(t["final_likes"] for t in group)
        result.append({
            "range": f"{low}〜{high - 1}点",
            "count": len(group),
            "median_final_likes": int(statistics.median(finals)),
            "max_final_likes": finals[-1],
            "viral_count": sum(1 for f in finals if f >= viral_likes),
            "viral_rate": round(sum(1 for f in finals if f >= viral_likes) / len(group) * 100),
            "notified_count": sum(1 for t in group if t["notified"]),
        })
    return result


FIELD_GUIDE = """\
### このレポートの各項目の意味

- **最終いいね**: 観測できた範囲での最大いいね数。「結局どれだけ伸びたか」の答え合わせに使う値
- **初回スコア**: システムが最初にその投稿を見たときに付けた点数
- **最高スコア**: 観測期間中の最高点。通知するかどうかはこの値で決まった
- **軌跡**: 「投稿からの経過分:その時点のいいね数」の並び。15分おきに観測している
- **通知**: 実際にスマホに通知を鳴らしたか
- **実測**: 2回以上観測できて、伸び率を差分で計算できたか(falseは初回観測のみで推定値)
"""

SCORE_FORMULA = """\
### スコアの計算式(0〜100点)

```
伸び率  = 40 × log1p(いいね/分) / log1p(120)      # 初回観測(推定値)の場合は ×0.7
加速度  = 15 × min(加速度 / 2.0, 1)               # 加速度が計算不能なら 7.5点
議論量  = 15 × min((返信 ÷ いいね) / 0.10, 1)
保存率  = 10 × min((ブックマーク ÷ いいね) / 0.15, 1)
拡散率  = 10 × min((リポスト ÷ いいね) / 0.25, 1)
関連度  = 10 × 関連度(キーワード1個で0.6 / 2個で0.8 / 3個以上で1.0)

合計 × 鮮度係数
  鮮度係数 = 投稿から60分以内なら 1.0
             それ以降は180分で 0.6 まで直線的に減衰

いいね/分 = (今回のいいね − 前回のいいね) ÷ 経過分   ← 平均速度ではなく実測の差分
加速度    = 直近区間の速度 ÷ その前の区間の速度      ← 1.0超なら加速中
```

### 通知の条件

```
足切り(これに当たると採点せず即除外):
  - 投稿から180分より古い
  - いいねが150未満
  - 初回観測 かつ 平均速度が8いいね/分未満

通知:
  - 上の足切りを通過し、かつスコアが55点以上
  - さらに、同じ話題は1件にまとめる(1回の実行で最大2件、24時間で最大12件)
```
"""


def render(tracks: list[dict], rows: list[dict], days: int, viral_likes: int) -> str:
    lines = []
    add = lines.append

    add("# 監視システムの実測ログ要約")
    add("")
    add(f"- 集計期間: 直近{days}日")
    add(f"- 観測レコード数: {len(rows):,}件")
    add(f"- 対象になった投稿数: {len(tracks):,}件")
    add(f"- うち通知を鳴らした投稿: {sum(1 for t in tracks if t['notified'])}件")
    add(f"- 「バズった」の判定基準: 最終いいね{viral_likes:,}以上")
    add("")
    add(FIELD_GUIDE)
    add(SCORE_FORMULA)

    # ── 1. スコア帯ごとの答え合わせ ──
    add("---")
    add("")
    add("## 1. スコア帯ごとの実績(★閾値決定の直接の根拠)")
    add("")
    add("スコアに予測力があるなら、上の帯ほど最終いいね数の中央値とバズ率が高くなるはずです。")
    add("")
    add("| スコア帯 | 件数 | 最終いいね中央値 | 最大 | バズ率 | 通知した数 |")
    add("|---|---|---|---|---|---|")
    for bucket in score_buckets(tracks, viral_likes):
        if not bucket["count"]:
            add(f"| {bucket['range']} | 0 | - | - | - | - |")
            continue
        add(
            f"| {bucket['range']} | {bucket['count']} | "
            f"{bucket['median_final_likes']:,} | {bucket['max_final_likes']:,} | "
            f"{bucket['viral_rate']}% | {bucket['notified_count']} |"
        )
    add("")

    # ── 2. 見逃し ──
    misses = [t for t in tracks if not t["notified"] and t["final_likes"] >= viral_likes]
    add("## 2. 見逃し(通知しなかったが、結果的に大きく伸びた投稿)")
    add("")
    if not misses:
        add("該当なし。")
    else:
        add(f"{len(misses)}件ありました。**閾値が高すぎる可能性を示します。**")
        add("")
        for t in misses[:15]:
            add(f"- **最終{t['final_likes']:,}いいね** / 最高スコア{t['max_score']:.0f}点 "
                f"/ 初回は投稿{t['first_age']}分後に{t['first_likes']:,}いいねで{t['first_score']:.0f}点")
            add(f"  - 「{t['text']}」")
            add(f"  - 軌跡: {format_trajectory(t['observations'])}")
    add("")

    # ── 3. 空振り ──
    false_alarms = [t for t in tracks if t["notified"] and t["final_likes"] < viral_likes]
    add("## 3. 空振り(通知したのに、それほど伸びなかった投稿)")
    add("")
    if not false_alarms:
        add("該当なし。")
    else:
        add(f"{len(false_alarms)}件ありました。**閾値が低すぎる、または配点が不適切な可能性を示します。**")
        add("")
        for t in false_alarms[:15]:
            add(f"- **最終{t['final_likes']:,}いいねどまり** / 最高スコア{t['max_score']:.0f}点")
            add(f"  - 「{t['text']}」")
            add(f"  - 軌跡: {format_trajectory(t['observations'])}")
    add("")

    # ── 4. 全投稿の軌跡 ──
    add(f"## 4. 上位{min(len(tracks), MAX_POSTS_IN_REPORT)}件の軌跡")
    add("")
    for t in tracks[:MAX_POSTS_IN_REPORT]:
        flag = "🔔通知" if t["notified"] else "　　　"
        add(f"- {flag} 最終{t['final_likes']:,}いいね / RT{t['final_retweets']:,} "
            f"/ 返信{t['final_replies']:,} / BM{t['final_bookmarks']:,} "
            f"| 最高{t['max_score']:.0f}点(初回{t['first_score']:.0f}点) "
            f"| 観測{t['samples']}回 {'実測' if t['measured'] else '推定のみ'} | {t['genre']}")
        add(f"  - 「{t['text']}」")
        add(f"  - 軌跡: {format_trajectory(t['observations'])}")
    add("")

    # ── 5. 足切りの内訳 ──
    add("## 5. 補足")
    add("")
    only_once = sum(1 for t in tracks if t["samples"] == 1)
    add(f"- 1回しか観測できなかった投稿: {only_once}/{len(tracks)}件")
    add("  (この投稿は伸び率を実測できず、推定値で判定しています。多い場合は実行間隔か収集範囲に問題があります)")
    if tracks:
        add(f"- 通知した投稿の最終いいね中央値: "
            f"{int(statistics.median([t['final_likes'] for t in tracks if t['notified']])) if any(t['notified'] for t in tracks) else '-'}")
        add(f"- 通知しなかった投稿の最終いいね中央値: "
            f"{int(statistics.median([t['final_likes'] for t in tracks if not t['notified']])) if any(not t['notified'] for t in tracks) else '-'}")
    add("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Grokに貼るためのログ要約レポートを作る")
    parser.add_argument("--days", type=int, default=3, help="集計する日数 (既定: 3)")
    parser.add_argument("--viral-threshold", type=int, default=DEFAULT_VIRAL_LIKES,
                        help=f"「バズった」とみなすいいね数 (既定: {DEFAULT_VIRAL_LIKES})")
    args = parser.parse_args()

    rows = load_rows(args.days)
    if not rows:
        print(
            "data/log/ にログがまだありません。\n"
            "監視ワークフローが数回実行されてから、もう一度試してください。"
        )
        return 1

    tracks = build_tracks(rows)
    report = render(tracks, rows, args.days, args.viral_threshold)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report, encoding="utf-8")

    print(f"レポートを書き出しました: {OUTPUT_PATH}")
    print(f"  観測レコード {len(rows):,}件 → 投稿 {len(tracks):,}件")
    print(f"  文字数 約{len(report):,}字(このままGrokに貼れます)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
