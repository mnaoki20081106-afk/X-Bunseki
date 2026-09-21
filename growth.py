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
        "prev_impressions_per_min": None,
        "impressions_acceleration": None,
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
        prev_imp_rate = _rate(baseline.get("impressions"), prev_baseline.get("impressions"), prev_window)
        result["prev_impressions_per_min"] = round(prev_imp_rate, 2)
        if prev_imp_rate >= MIN_PREV_RATE_FOR_ACCEL:
            result["impressions_acceleration"] = round(result["impressions_per_min"] / prev_imp_rate, 2)

    # 表示用に丸める
    for field in _TRACKED_FIELDS:
        result[f"{field}_per_min"] = round(result[f"{field}_per_min"], 2)

    return result



def predict_final_impressions(post: dict, growth: dict) -> dict:
    """Grok由来の経験則を初期priorにした最終imp予測。実測蓄積後に係数を校正する。"""
    current = max(int(post.get("impressions") or 0), 0)
    age = max(float(growth.get("age_minutes") or 0.1), 0.1)
    if current <= 0:
        return {"predicted_final_impressions": None, "prediction_confidence": "none",
                "prediction_basis": "impressions未取得"}

    # Grokの remaining_multiplier_base を時間方向に線形補間。
    anchors = [(15, 15.0), (30, 8.0), (60, 4.5), (120, 2.5), (240, 1.5)]
    if age <= anchors[0][0]:
        remaining = anchors[0][1]
    elif age >= anchors[-1][0]:
        remaining = anchors[-1][1]
    else:
        remaining = anchors[-1][1]
        for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
            if x0 <= age <= x1:
                t = (age - x0) / (x1 - x0)
                remaining = y0 + (y1 - y0) * t
                break

    # impの実測加速度を最重要補正にする。
    imp_accel = growth.get("impressions_acceleration")
    if imp_accel is None:
        accel_adjust = 1.0
    elif imp_accel >= 1.5:
        accel_adjust = min(1.5 + (imp_accel - 1.5) * 0.5, 3.0)
    elif imp_accel >= 1.2:
        accel_adjust = 1.2
    elif imp_accel >= 0.9:
        accel_adjust = 1.0
    elif imp_accel < 0.6 and age >= 60:
        accel_adjust = 0.55
    else:
        accel_adjust = 0.8

    # engagementは倍率を暴れさせず±25%程度の補正に制限。
    likes = max(post.get("likes") or 0, 0)
    rts = max(post.get("retweets") or 0, 0)
    replies = max(post.get("replies") or 0, 0)
    bookmarks = max(post.get("bookmarks") or 0, 0)
    like_r = likes / current
    rt_r = rts / current
    reply_r = replies / current
    bookmark_r = bookmarks / current
    quality_raw = (
        0.30 * min(like_r / 0.03, 2.0)
        + 0.35 * min(rt_r / 0.005, 2.0)
        + 0.20 * min(bookmark_r / 0.01, 2.0)
        + 0.15 * min(reply_r / 0.005, 2.0)
    )
    quality_adjust = min(max(0.75 + 0.25 * quality_raw, 0.75), 1.25)

    predicted = max(current, int(round(current * remaining * accel_adjust * quality_adjust)))
    samples = int(growth.get("samples") or 0)
    confidence = "high" if samples >= 3 and imp_accel is not None else ("medium" if samples >= 1 else "low")
    return {
        "predicted_final_impressions": predicted,
        "prediction_confidence": confidence,
        "prediction_basis": "Grok経験則prior+15分観測",
        "prediction_remaining_multiplier": round(remaining, 2),
        "prediction_accel_adjustment": round(accel_adjust, 2),
        "prediction_quality_adjustment": round(quality_adjust, 2),
    }

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
