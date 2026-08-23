"""
detector.py (v4)
「バズる前の投稿」を、実測の伸び率にもとづいて判定・採点する。

════════════════════════════════════════════════════════════
 v3までの何が問題だったか(実行履歴49回分の実データから)
════════════════════════════════════════════════════════════

【問題1】AND条件の掛け算で、確率的にほぼ絶対に通らない設計になっていた

  v3.4の「持続バズ」は次の4条件を全て同時に満たすことを要求していた。
      いいね速度 AND インプレッション20万 AND 返信率4.5% AND (BM20 or 引用10)
  「瞬間バズ」も いいね1800 AND RT150 AND 返信70 の3条件AND。

  仮に各条件の通過率が3割だとすると、4条件ANDの通過率は 0.3^4 = 0.8%。
  条件を1つ足すたびに通過率は指数的に落ちる。
  実際、8/21〜8/23の49回の実行のうち通知が出たのはわずか6回。
  さらに通知17件中14件が同じ地震で、1回の実行で8件まとめて鳴っていた。
  「ほぼ無音か、一度に大量」という最悪の挙動になっていたのは、
  閾値の高さではなくAND構造そのものが原因。

  → v4では「加重スコア1本 + 閾値1つ」に置き換えた。
     調整すべきツマミが12個から1個になり、感度を連続的に動かせる。

【問題2】そもそも伸び率を計算していなかった

  v3の velocity は「いいね総数 ÷ 投稿からの経過時間」＝平均速度であって
  伸び率ではない。詳しくは growth.py の冒頭を参照。
  v4では前回観測との差分(growth.py)を使う。

【問題3】入口が min_faves:500 だった

  すでに500いいね付いた投稿しか入口を通れない設計では、
  「バズる前に見つける」ことは定義上できない。
  → v4では keyword_filter が検索クエリ側でジャンルを絞るので、
     min_faves を50前後まで下げられる(playwright_collector.py 参照)。

════════════════════════════════════════════════════════════
 v4の判定方針
════════════════════════════════════════════════════════════

  1. 足切り(ハードフィルタ) … 明らかに対象外のものだけを落とす
       - 投稿から古すぎる
       - いいねが少なすぎる(ノイズ)
       - NGワード(広告・懸賞)を含む
  2. 採点(0〜100点)          … 伸び率・加速度・議論量・保存率・拡散率・関連度
  3. 閾値との比較            … NOTIFY_SCORE 以上なら通知候補

  全ての閾値は環境変数で上書きできる。Grokの提案などを試すときに
  コードを書き換えずワークフローのenvだけで実験できるようにするため。
"""

import math
import os
from datetime import datetime, timedelta, timezone

import growth


def _envf(name: str, default: float) -> float:
    """環境変数があればそれを使う(コードを触らずに閾値を実験できるようにする)"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[detector] 環境変数 {name}={raw!r} は数値として解釈できません。既定値{default}を使います")
        return default


# ── 足切り(ハードフィルタ) ──────────────────────────
MAX_AGE_MINUTES = _envf("MAX_AGE_MINUTES", 180)      # 3時間より古い投稿は追わない
MIN_LIKES_FLOOR = _envf("MIN_LIKES_FLOOR", 150)      # これ未満はノイズとして無視
# 初回観測(＝伸び率が実測できていない)投稿に要求する、最低限の平均速度。
# 実測値より信頼できないぶん、少しだけ厳しくする。
FIRST_SIGHT_MIN_LIKES_PER_MIN = _envf("FIRST_SIGHT_MIN_LIKES_PER_MIN", 8)

# ── 通知の閾値 ────────────────────────────────────
NOTIFY_SCORE = _envf("NOTIFY_SCORE", 55)             # このスコア以上で通知
GEKIATSU_SCORE = _envf("GEKIATSU_SCORE", 75)         # このスコア以上は「激アツ」表示

# ── 採点の配点(合計100点) ────────────────────────
POINTS_GROWTH = _envf("POINTS_GROWTH", 44)           # 伸び率そのもの
POINTS_ACCEL = _envf("POINTS_ACCEL", 17)             # 加速しているか
POINTS_DISCUSSION = _envf("POINTS_DISCUSSION", 17)   # 議論の量(返信÷いいね)
POINTS_SAVE = _envf("POINTS_SAVE", 11)               # 保存率(BM÷いいね)
POINTS_SPREAD = _envf("POINTS_SPREAD", 11)           # 拡散率(RT÷いいね)

# ★関連度は「加点」ではなく「係数」として効かせる(2026-08-24に変更)。
#
# 変更前は関連度を10点の加点として扱っていた。しかしそれだと
#   伸び率40 + 加速度7.5 + 拡散率10 = 57.5点
# となり、**関連度0点(＝狙っているジャンルと全く関係ない投稿)でも
# 閾値55点を超えて通知できてしまった。**
#
# 実際、初回の本番実行で通知された2件は、どちらも関連度0.0の
# アイドル事務所公式アカウントの告知だった:
#     @Aegroupofficial 64.9点 関連度0.0
#     @SN__20200122    64.8点 関連度0.0
# 数字が大きいだけの、動画のネタにならない投稿である。
#
# 係数にすると、ジャンル外の投稿は最大でも65%に減点される。
# 上の2件は46点前後まで下がり、通知されなくなる。
# 一方で「本当に巨大な出来事」なら、キーワードに無くても
# 高得点を取って通知できる余地は残してある(完全な足切りにはしない)。
RELEVANCE_FLOOR = _envf("RELEVANCE_FLOOR", 0.65)     # 関連度0のときの係数

# ── 採点の基準値(この値で満点になる) ──────────────
GROWTH_FULL_LIKES_PER_MIN = _envf("GROWTH_FULL_LIKES_PER_MIN", 120)
ACCEL_FULL = _envf("ACCEL_FULL", 2.0)                # 加速度2.0倍で満点
DISCUSSION_FULL_RATIO = _envf("DISCUSSION_FULL_RATIO", 0.10)   # 返信÷いいね 10%
SAVE_FULL_RATIO = _envf("SAVE_FULL_RATIO", 0.15)               # BM÷いいね 15%
SPREAD_FULL_RATIO = _envf("SPREAD_FULL_RATIO", 0.25)           # RT÷いいね 25%

# 実測できていない(初回観測の)投稿の伸び率点にかける割引率
UNMEASURED_CONFIDENCE = _envf("UNMEASURED_CONFIDENCE", 0.7)

# 投稿が古くなるほどスコアを下げる。この分数を過ぎた分だけ減衰する。
FRESHNESS_FULL_MINUTES = _envf("FRESHNESS_FULL_MINUTES", 60)
FRESHNESS_MIN_MULTIPLIER = _envf("FRESHNESS_MIN_MULTIPLIER", 0.6)

# ── 通知量の制御 ──────────────────────────────────
NOTIFY_MAX_PER_RUN = int(_envf("NOTIFY_MAX_PER_RUN", 2))     # 1回の実行で鳴らす上限
NOTIFY_MAX_PER_DAY = int(_envf("NOTIFY_MAX_PER_DAY", 12))    # 1日あたりの上限
ACCOUNT_COOLDOWN_HOURS = _envf("ACCOUNT_COOLDOWN_HOURS", 3.0)
TOPIC_COOLDOWN_HOURS = _envf("TOPIC_COOLDOWN_HOURS", 8.0)    # 同じ話題を再通知しない時間


# ──────────────────────────────────────────────────
#  互換用ユーティリティ
# ──────────────────────────────────────────────────

def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def elapsed_hours(posted_at, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    return max((now - _parse_dt(posted_at)).total_seconds() / 3600.0, 0.01)


def is_in_cooldown(author_handle: str, last_notified_at, now=None) -> bool:
    """同じ投稿者から立て続けに通知しないためのクールダウン判定"""
    if not last_notified_at:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - _parse_dt(last_notified_at)) < timedelta(hours=ACCOUNT_COOLDOWN_HOURS)


# ──────────────────────────────────────────────────
#  1. 足切り
# ──────────────────────────────────────────────────

def hard_filter_reason(post: dict, g: dict) -> str | None:
    """
    足切りに引っかかった理由を返す(通過するならNone)。
    理由を文字列で返すのは、ログを見て「なぜ何も鳴らないのか」を
    すぐ切り分けられるようにするため。v3ではここが不透明だった。
    """
    if g["age_minutes"] > MAX_AGE_MINUTES:
        return f"古すぎる({g['age_minutes']:.0f}分 > {MAX_AGE_MINUTES:.0f}分)"

    if (post.get("likes") or 0) < MIN_LIKES_FLOOR:
        return f"いいねが少なすぎる({post.get('likes', 0)} < {MIN_LIKES_FLOOR:.0f})"

    if not g["is_measured"] and g["likes_per_min"] < FIRST_SIGHT_MIN_LIKES_PER_MIN:
        return (
            f"初回観測かつ平均速度が低い"
            f"({g['likes_per_min']:.1f} < {FIRST_SIGHT_MIN_LIKES_PER_MIN:.1f}/分)"
        )

    return None


# ──────────────────────────────────────────────────
#  2. 採点
# ──────────────────────────────────────────────────

def _ratio_points(numerator: int, denominator: int, full_ratio: float, max_points: float) -> float:
    if denominator <= 0 or full_ratio <= 0:
        return 0.0
    ratio = numerator / denominator
    return max_points * min(ratio / full_ratio, 1.0)


def score(post: dict, g: dict, relevance: float = 0.0) -> dict:
    """
    投稿を0〜100点で採点し、内訳も一緒に返す。
    内訳を返すのは、通知が多い/少ないときに「どの要素で点が入って
    いるのか」を見て調整できるようにするため。
    """
    likes = post.get("likes") or 0
    breakdown = {}

    # (1) 伸び率: 対数スケール。1分あたり120いいねで満点。
    #     対数にするのは、10/分 と 20/分 の差の方が
    #     200/分 と 210/分 の差より意味が大きいため。
    lpm = g["likes_per_min"]
    growth_ratio = math.log1p(max(lpm, 0)) / math.log1p(GROWTH_FULL_LIKES_PER_MIN)
    growth_points = POINTS_GROWTH * min(growth_ratio, 1.0)
    if not g["is_measured"]:
        growth_points *= UNMEASURED_CONFIDENCE
    breakdown["伸び率"] = round(growth_points, 1)

    # (2) 加速度: 前の区間より速くなっているか。
    #     判定不能(観測3回未満)のときは中間点を入れる。
    accel = g.get("acceleration")
    if accel is None:
        accel_points = POINTS_ACCEL * 0.5
    else:
        accel_points = POINTS_ACCEL * max(min(accel / ACCEL_FULL, 1.0), 0.0)
    breakdown["加速度"] = round(accel_points, 1)

    # (3) 議論量: 返信÷いいね。高いほど賛否が割れている=解説動画の需要が高い。
    discussion_points = _ratio_points(
        post.get("replies") or 0, likes, DISCUSSION_FULL_RATIO, POINTS_DISCUSSION
    )
    breakdown["議論量"] = round(discussion_points, 1)

    # (4) 保存率: ブックマーク÷いいね。「後で見返したい情報」=解説需要。
    save_points = _ratio_points(
        post.get("bookmarks") or 0, likes, SAVE_FULL_RATIO, POINTS_SAVE
    )
    breakdown["保存率"] = round(save_points, 1)

    # (5) 拡散率: RT÷いいね。ニュース性の高さ。
    spread_points = _ratio_points(
        post.get("retweets") or 0, likes, SPREAD_FULL_RATIO, POINTS_SPREAD
    )
    breakdown["拡散率"] = round(spread_points, 1)

    raw_total = sum(breakdown.values())

    # (6) 関連度: 狙ったジャンルにどれだけ近いか。加点ではなく係数として効かせる。
    relevance = max(min(relevance, 1.0), 0.0)
    relevance_multiplier = RELEVANCE_FLOOR + (1.0 - RELEVANCE_FLOOR) * relevance

    # (7) 鮮度による減衰: 発見が遅い投稿ほど、動画にする価値が下がる。
    age = g["age_minutes"]
    if age <= FRESHNESS_FULL_MINUTES:
        freshness = 1.0
    else:
        over = (age - FRESHNESS_FULL_MINUTES) / max(MAX_AGE_MINUTES - FRESHNESS_FULL_MINUTES, 1)
        freshness = max(1.0 - over * (1.0 - FRESHNESS_MIN_MULTIPLIER), FRESHNESS_MIN_MULTIPLIER)

    total = raw_total * freshness * relevance_multiplier

    return {
        "buzz_score": round(total, 1),
        "score_breakdown": breakdown,
        "freshness_multiplier": round(freshness, 2),
        "relevance_multiplier": round(relevance_multiplier, 2),
        "relevance": round(relevance, 2),
        "is_gekiatsu": total >= GEKIATSU_SCORE,
    }


# ──────────────────────────────────────────────────
#  3. 評価(足切り + 採点をまとめて実行)
# ──────────────────────────────────────────────────

def evaluate(post: dict, history: list[dict], relevance: float = 0.0, now=None) -> dict:
    """
    1投稿を評価して、post に評価結果を足した新しいdictを返す。

    付与するキー:
      growth          : growth.compute() の結果
      buzz_score      : 0〜100点
      score_breakdown : 配点の内訳
      rejected_reason : 足切り理由(通過ならNone)
      should_notify   : 通知候補かどうか
    """
    g = growth.compute(post, history, now=now)
    enriched = dict(post)
    enriched["growth"] = g
    enriched["elapsed_minutes"] = g["age_minutes"]
    enriched["elapsed_hours"] = round(g["age_minutes"] / 60.0, 2)

    reason = hard_filter_reason(post, g)
    if reason:
        enriched["rejected_reason"] = reason
        enriched["buzz_score"] = 0.0
        enriched["score_breakdown"] = {}
        enriched["is_gekiatsu"] = False
        enriched["should_notify"] = False
        return enriched

    enriched["rejected_reason"] = None
    enriched.update(score(post, g, relevance=relevance))
    enriched["should_notify"] = enriched["buzz_score"] >= NOTIFY_SCORE
    return enriched


def evaluate_all(posts: list[dict], history_map: dict, relevance_fn=None, now=None) -> list[dict]:
    """
    投稿リストをまとめて評価し、スコア降順で返す。
    history_map: {post_id: [過去の観測レコード...]}
    """
    results = []
    for post in posts:
        relevance = relevance_fn(post) if relevance_fn else 0.0
        results.append(
            evaluate(post, history_map.get(post["post_id"], []), relevance=relevance, now=now)
        )
    results.sort(key=lambda p: p["buzz_score"], reverse=True)
    return results


def config_summary() -> dict:
    """現在有効な閾値を返す(status.jsonに載せて、いつでも確認できるようにする)"""
    return {
        "NOTIFY_SCORE": NOTIFY_SCORE,
        "MIN_LIKES_FLOOR": MIN_LIKES_FLOOR,
        "MAX_AGE_MINUTES": MAX_AGE_MINUTES,
        "FIRST_SIGHT_MIN_LIKES_PER_MIN": FIRST_SIGHT_MIN_LIKES_PER_MIN,
        "NOTIFY_MAX_PER_RUN": NOTIFY_MAX_PER_RUN,
        "NOTIFY_MAX_PER_DAY": NOTIFY_MAX_PER_DAY,
    }
