"""
growth.py
「伸び率」を“実測”で計算するモジュール。

★これがv4で最も重要な追加。

旧実装(v3.x)の致命的な問題:
    velocity = いいね総数 ÷ 投稿からの経過時間

  これは「伸び率」ではなく「投稿からの平均速度」でしかない。
  平均速度は、投稿直後に跳ねて既に失速した投稿でも高いまま出るし、
  逆に「今まさに加速している」投稿を検出できない。
  このプロジェクトの目的(=バズる前の早期発見)には原理的に使えない。

v4の方式:
    1回の実行ごとに、収集した全投稿の指標を observations テーブルへ記録する。
    次の実行で同じ投稿を再び見つけたら、前回との差分を取る。

        いいね/分 = (今回のいいね - 前回のいいね) ÷ 経過分

    さらに、その1つ前の観測区間の速度と比べて「加速度」を出す。

        加速度 = 今回の区間速度 ÷ 前回の区間速度

    加速度 > 1.0 なら「まだ伸びている(これから伸びる)」、
    < 1.0 なら「もう失速している(今から動画にしても遅い)」。

  これで初めて「早期発見」が成立する。
"""

from datetime import datetime, timezone

# 観測間隔がこれより短い場合は、差分がノイズに埋もれるので信用しない
MIN_WINDOW_MINUTES = 3.0

# 初回観測(＝差分が取れない)時に、投稿からの平均速度で代用する際の下限経過時間。
# 投稿1分後に500いいねだと「500/分」という非現実的な数字になるのを防ぐ。
MIN_AGE_MINUTES_FOR_LIFETIME = 5.0

# 前回速度がこれ未満の場合、加速度の分母として使わない(0除算・過大評価の防止)
MIN_PREV_RATE_FOR_ACCEL = 1.0

_TRACKED_FIELDS = ("likes", "retweets", "replies", "bookmarks", "impressions")


def _to_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def minutes_between(earlier, later) -> float:
    return (_to_dt(later) - _to_dt(earlier)).total_seconds() / 60.0


def age_minutes(posted_at, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    return max(minutes_between(posted_at, now), 0.1)


def _rate(current: int, previous: int, window_min: float) -> float:
    """
    区間速度(1分あたりの増加数)。

    Xの表示値は概算のため、たまに前回より減ることがある。
    その場合は0扱いにする(マイナスの速度は意味を持たないため)。
    """
    delta = max((current or 0) - (previous or 0), 0)
    return delta / max(window_min, 1.0)


def compute(post: dict, history: list[dict], now=None) -> dict:
    """
    post    : 今回収集した投稿(likes等の現在値を持つ)
    history : 同じ post_id の過去の観測レコード。observed_at 昇順。今回分は含めない。

    戻り値(主要なもの):
      is_measured        : True なら実測差分ベース、False なら初回観測の推定値
      likes_per_min      : 直近区間のいいね増加速度
      acceleration       : 直近区間 ÷ その前の区間 (Noneなら判定不能)
      window_minutes     : 差分を取った区間の長さ
      samples            : 過去の観測回数
      age_minutes        : 投稿からの経過分
      *_per_min          : 各指標の速度
    """
    now = now or datetime.now(timezone.utc)
    age_min = age_minutes(post.get("posted_at"), now=now)

    result = {
        "age_minutes": round(age_min, 1),
        "samples": len(history),
        "is_measured": False,
        "window_minutes": None,
        "acceleration": None,
        "prev_likes_per_min": None,
    }
    for field in _TRACKED_FIELDS:
        result[f"{field}_per_min"] = 0.0
        result[f"{field}_delta"] = 0

    # 直近区間として使える観測を、新しい方から探す
    # (MIN_WINDOW_MINUTES 未満しか離れていない観測は飛ばす)
    baseline = None
    baseline_index = None
    for idx in range(len(history) - 1, -1, -1):
        window = minutes_between(history[idx]["observed_at"], now)
        if window >= MIN_WINDOW_MINUTES:
            baseline = history[idx]
            baseline_index = idx
            break

    if baseline is None:
        # ── 初回観測(または直前すぎる観測しかない) ──
        # 差分が取れないので、投稿からの平均速度で代用する。
        # ただし is_measured=False とし、判定側で厳しめに扱う。
        safe_age = max(age_min, MIN_AGE_MINUTES_FOR_LIFETIME)
        for field in _TRACKED_FIELDS:
            result[f"{field}_per_min"] = (post.get(field) or 0) / safe_age
        result["window_minutes"] = round(safe_age, 1)
        result["basis"] = "初回観測(投稿からの平均速度で代用)"
        return result

    window = minutes_between(baseline["observed_at"], now)
    result["is_measured"] = True
    result["window_minutes"] = round(window, 1)
    result["basis"] = f"実測({window:.0f}分間の差分)"

    for field in _TRACKED_FIELDS:
        current = post.get(field) or 0
        previous = baseline.get(field) or 0
        result[f"{field}_delta"] = max(current - previous, 0)
        result[f"{field}_per_min"] = _rate(current, previous, window)

    # ── 加速度: 1つ前の区間の速度と比べる ──
    prev_baseline = None
    for idx in range(baseline_index - 1, -1, -1):
        prev_window = minutes_between(history[idx]["observed_at"], baseline["observed_at"])
        if prev_window >= MIN_WINDOW_MINUTES:
            prev_baseline = history[idx]
            break

    if prev_baseline is not None:
        prev_window = minutes_between(prev_baseline["observed_at"], baseline["observed_at"])
        prev_rate = _rate(baseline.get("likes"), prev_baseline.get("likes"), prev_window)
        result["prev_likes_per_min"] = round(prev_rate, 2)
        if prev_rate >= MIN_PREV_RATE_FOR_ACCEL:
            result["acceleration"] = round(result["likes_per_min"] / prev_rate, 2)

    # 表示用に丸める
    for field in _TRACKED_FIELDS:
        result[f"{field}_per_min"] = round(result[f"{field}_per_min"], 2)

    return result


def describe(growth: dict) -> str:
    """通知本文に載せる、人間が読める1行の要約"""
    lpm = growth.get("likes_per_min", 0)
    accel = growth.get("acceleration")
    if growth.get("is_measured"):
        text = f"+{lpm:.0f}いいね/分(直近{growth.get('window_minutes')}分の実測)"
    else:
        text = f"約{lpm:.0f}いいね/分(初回観測のため推定)"
    if accel is not None:
        arrow = "加速" if accel >= 1.0 else "失速"
        text += f" / {arrow}{accel:.1f}x"
    return text
