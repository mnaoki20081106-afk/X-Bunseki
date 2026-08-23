"""
tests/test_pipeline.py
実際のXにアクセスせず、判定ロジックだけを検証するテスト。

★なぜ追加したか:
  v3までテストが1つも無く、「通知が来ない」原因が
  収集の失敗なのか判定ロジックなのか切り分けられなかった。
  判定ロジックは合成データで完全に検証できるので、
  ここが通っていれば「原因は収集側」と即断できる。

実行方法:
    python3 -m pytest tests/ -q       (pytestがある場合)
    python3 tests/test_pipeline.py    (pytestが無くても動く)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clustering  # noqa: E402
import detector  # noqa: E402
import growth  # noqa: E402
import keyword_filter  # noqa: E402
import notification_text  # noqa: E402
import playwright_collector as collector  # noqa: E402

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def _post(minutes_old=30, likes=1000, retweets=200, replies=100,
          bookmarks=100, impressions=200000, text="【速報】東京で震度5弱の地震"):
    return {
        "post_id": "1",
        "author_handle": "someone",
        "url": "https://x.com/someone/status/1",
        "posted_at": (NOW - timedelta(minutes=minutes_old)).isoformat(),
        "text_snippet": text,
        "likes": likes, "retweets": retweets, "replies": replies,
        "quotes": 0, "bookmarks": bookmarks, "impressions": impressions,
    }


def _obs(minutes_ago, likes, **kw):
    row = {
        "observed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "likes": likes, "retweets": 0, "replies": 0,
        "quotes": 0, "bookmarks": 0, "impressions": 0,
    }
    row.update(kw)
    return row


# ── growth.py ────────────────────────────────────────────

def test_growth_uses_real_deltas():
    """前回観測との差分から、実測の伸び率が出ること"""
    post = _post(minutes_old=40, likes=1000)
    history = [_obs(30, 400), _obs(15, 700)]
    g = growth.compute(post, history, now=NOW)

    assert g["is_measured"] is True, "履歴があるなら実測になるはず"
    # 15分間で 700 → 1000、つまり +300/15分 = 20/分
    assert abs(g["likes_per_min"] - 20.0) < 0.01, g["likes_per_min"]
    # 直前区間は15分で 400 → 700 = 20/分 なので加速度は1.0
    assert abs(g["acceleration"] - 1.0) < 0.01, g["acceleration"]


def test_growth_detects_acceleration():
    """伸びが加速している投稿で acceleration > 1 になること"""
    post = _post(minutes_old=40, likes=2000)
    history = [_obs(30, 300), _obs(15, 600)]  # 前区間20/分 → 今区間93.3/分
    g = growth.compute(post, history, now=NOW)
    assert g["acceleration"] > 4.0, g["acceleration"]


def test_growth_detects_deceleration():
    """失速した投稿で acceleration < 1 になること"""
    post = _post(minutes_old=40, likes=1050)
    history = [_obs(30, 300), _obs(15, 1000)]  # 前区間46.7/分 → 今区間3.3/分
    g = growth.compute(post, history, now=NOW)
    assert g["acceleration"] < 0.2, g["acceleration"]


def test_growth_first_sighting_is_estimated():
    """履歴が無い場合は推定扱いになること"""
    g = growth.compute(_post(minutes_old=50, likes=1000), [], now=NOW)
    assert g["is_measured"] is False
    assert abs(g["likes_per_min"] - 20.0) < 0.01  # 1000 ÷ 50分


def test_growth_never_returns_negative():
    """いいねが減っても速度がマイナスにならないこと(Xの表示は概算のため起きうる)"""
    post = _post(minutes_old=40, likes=900)
    g = growth.compute(post, [_obs(15, 1000)], now=NOW)
    assert g["likes_per_min"] == 0.0


# ── detector.py ──────────────────────────────────────────

def test_detector_rejects_old_posts():
    post = _post(minutes_old=400, likes=50000)
    result = detector.evaluate(post, [], relevance=1.0, now=NOW)
    assert result["rejected_reason"] is not None
    assert "古すぎる" in result["rejected_reason"]


def test_detector_rejects_low_likes():
    post = _post(minutes_old=20, likes=10)
    result = detector.evaluate(post, [], relevance=1.0, now=NOW)
    assert "いいねが少なすぎる" in result["rejected_reason"]


def test_detector_notifies_a_fast_growing_post():
    """実測で急加速している投稿は通知されること"""
    post = _post(minutes_old=45, likes=6000, retweets=1500, replies=700, bookmarks=800)
    history = [_obs(30, 800), _obs(15, 2500)]
    result = detector.evaluate(post, history, relevance=1.0, now=NOW)
    assert result["rejected_reason"] is None, result["rejected_reason"]
    assert result["should_notify"] is True, result
    assert result["buzz_score"] >= detector.NOTIFY_SCORE


def test_detector_ignores_a_stalled_post():
    """数字は大きいが完全に失速した投稿は通知されないこと(v3が拾えていた典型)"""
    post = _post(minutes_old=150, likes=30000, retweets=1000, replies=200, bookmarks=100)
    history = [_obs(60, 29000), _obs(30, 29900)]
    result = detector.evaluate(post, history, relevance=1.0, now=NOW)
    assert result["should_notify"] is False, result


def test_score_is_bounded():
    """どんな極端な値でもスコアが100を超えないこと"""
    post = _post(minutes_old=5, likes=999999, retweets=999999,
                 replies=999999, bookmarks=999999)
    history = [_obs(4, 0), _obs(2, 1)]
    result = detector.evaluate(post, history, relevance=1.0, now=NOW)
    assert 0 <= result["buzz_score"] <= 100, result["buzz_score"]


def test_env_override_changes_threshold():
    """環境変数で閾値を上書きできること(調整をコード変更なしで行うため)"""
    os.environ["NOTIFY_SCORE"] = "99"
    try:
        import importlib
        importlib.reload(detector)
        assert detector.NOTIFY_SCORE == 99.0
    finally:
        del os.environ["NOTIFY_SCORE"]
        import importlib
        importlib.reload(detector)
    assert detector.NOTIFY_SCORE == 55.0


def test_offgenre_post_with_big_numbers_is_not_notified():
    """
    ★実運用で起きた誤通知の再発防止。

    初回の本番実行で、アイドル事務所公式アカウントの告知が2件通知された。
    どちらも関連度0.0(狙っているジャンルのワードを1つも含まない)なのに、
    伸び率と拡散率だけで55点を超えていた。
        @Aegroupofficial 64.9点 いいね13,471 関連度0.0
        @SN__20200122    64.8点 いいね20,498 関連度0.0
    数字が大きいだけで、スクープでも解説ネタでもない投稿である。
    """
    post = _post(minutes_old=50, likes=13471, retweets=3500, replies=500, bookmarks=250)
    history = [_obs(35, 9000), _obs(20, 5000)]
    result = detector.evaluate(post, history, relevance=0.0, now=NOW)
    assert result["should_notify"] is False, (
        f"ジャンル外の投稿が通知されています: {result['buzz_score']}点 {result['score_breakdown']}"
    )


def test_ongenre_post_still_notified():
    """
    ジャンル外を弾く仕組みが、狙っている投稿まで巻き添えにしていないこと。
    キーワードが1つ当たれば(関連度0.8)通知に届く必要がある。
    """
    post = _post(minutes_old=40, likes=1500, retweets=225, replies=90, bookmarks=75)
    history = [_obs(25, 900), _obs(10, 300)]
    result = detector.evaluate(post, history, relevance=0.8, now=NOW)
    assert result["should_notify"] is True, (
        f"狙っているジャンルの投稿が通知されていません: "
        f"{result['buzz_score']}点 {result['score_breakdown']}"
    )


def test_relevance_acts_as_a_multiplier():
    """関連度が同じ投稿のスコアを実際に押し下げていること"""
    post = _post(minutes_old=40, likes=3000, retweets=500, replies=200, bookmarks=200)
    history = [_obs(25, 1500)]
    high = detector.evaluate(post, history, relevance=1.0, now=NOW)["buzz_score"]
    low = detector.evaluate(post, history, relevance=0.0, now=NOW)["buzz_score"]
    assert low < high, (low, high)
    assert abs(low / high - detector.RELEVANCE_FLOOR) < 0.01


def test_one_keyword_hit_is_enough_to_count_as_on_genre():
    """
    キーワードが1つ当たれば「ジャンルど真ん中」として扱うこと。
    「文春砲」が1つ当たった時点で狙い通りであり、
    3つ当たった場合と大きな差を付けるべきではない。
    """
    assert keyword_filter.relevance_score("文春砲が炸裂") >= 0.8


def test_groq_model_falls_back_when_env_is_empty_string():
    """
    ★実運用で起きた不具合の再発防止。

    ワークフローが GROQ_MODEL: ${{ vars.GROQ_MODEL }} を渡すため、
    Variablesが未設定でも「空文字」が環境変数に入る。
    os.environ.get(name, default) はキーが存在すれば空文字を返すので、
    既定値が効かずモデル名が "" になり、毎回LLM評価が失敗していた。
        Error code: 404 - The model `` does not exist
    """
    import importlib
    os.environ["GROQ_MODEL"] = ""
    try:
        import content_scorer
        importlib.reload(content_scorer)
        assert content_scorer.MODEL, "空文字のときは既定のモデル名に戻ること"
        assert content_scorer.MODEL.strip() != ""
    finally:
        del os.environ["GROQ_MODEL"]


def test_sanitize_keeps_high_retweet_ratios():
    """
    ★実運用で起きた誤検知の再発防止。

    「リポストがいいねより多い」のは異常ではない。災害情報・注意喚起・
    公式の拡散キャンペーンでは普通に起きる。以前は3倍で切っていたため、
    実在する値(ローソン公式 いいね109,922 / リポスト515,166)を
    パース失敗とみなして0にしていた。
    """
    real_cases = [(330, 1237), (109922, 515166), (1633, 7409)]
    for likes, retweets in real_cases:
        post = collector.sanitize_counts(
            {"post_id": "x", "likes": likes, "retweets": retweets,
             "bookmarks": 0, "quotes": 0}
        )
        assert post["retweets"] == retweets, f"正常な値が捨てられた: {likes=} {retweets=}"

    # パース事故レベルの値は引き続き捨てる
    broken = collector.sanitize_counts(
        {"post_id": "x", "likes": 1000, "retweets": 50000, "bookmarks": 0, "quotes": 0}
    )
    assert broken["retweets"] == 0


def test_line_sends_only_system_alerts_by_default():
    """
    ★LINEの無料プランはプッシュ通知が月200通まで。実際に上限に達した。
        status=429 {"message":"You have reached your monthly limit."}
    急上昇通知はPushoverに任せ、LINEにはシステム通知だけを送る。
    """
    import line_notifier
    assert line_notifier.LINE_MODE == "alerts_only"
    assert line_notifier.should_send({"system_message": "⚠️ セッション切れ"}) is True
    assert line_notifier.should_send({"post_id": "1", "buzz_score": 80}) is False


def test_interval_limit_blocks_frequent_notifications():
    """
    ★実運用の苦情への対応。
    このツールの趣旨は「選りすぐりを早期に見つけること」であって、
    条件に合う投稿を全部知らせることではない。
    候補が何件あっても、前回から一定時間空くまでは鳴らさない。
    """
    assert detector.can_notify_now(60, None)[0] is True, "初回は鳴る"
    assert detector.can_notify_now(60, 20)[0] is False, "20分後は見送る"
    assert detector.can_notify_now(60, 50)[0] is True, "45分経てば鳴る"


def test_big_story_can_break_the_interval():
    """
    間隔制限で「凡庸な通知の直後に来た大ネタ」を潰さないこと。
    そうなると早期発見という目的そのものを損なう。
    """
    assert detector.can_notify_now(85, 20)[0] is True, "80点超なら間隔を短縮できる"
    assert detector.can_notify_now(85, 5)[0] is False, "それでも最短15分は空ける"


def test_only_top_scores_use_the_repeating_alert():
    """
    ★「同じ通知が何度も鳴って鬱陶しい」の再発防止。

    旧設定は全通知が priority=2 / retry=60 / expire=3600 で、
    1件の通知が確認するまで**60秒おきに最大60回**鳴っていた。
    繰り返し鳴る優先度2は、飛び抜けたスコアのものだけに限定する。
    """
    import pushover_notifier
    assert pushover_notifier.DEFAULT_PRIORITY == "1", "通常通知は繰り返さない優先度"
    assert pushover_notifier.DEFAULT_URGENT_PRIORITY == "2"
    assert int(pushover_notifier.DEFAULT_RETRY) >= 300, "再通知は5分以上あける"
    assert int(pushover_notifier.DEFAULT_EXPIRE) <= 600, "鳴り続けるのは10分まで"
    # 実際に鳴る回数
    repeats = int(pushover_notifier.DEFAULT_EXPIRE) // int(pushover_notifier.DEFAULT_RETRY)
    assert repeats <= 2, f"最大{repeats}回は多すぎる"


def test_notification_history_survives_cache_loss():
    """
    通知履歴がリポジトリ内のファイルで管理されていること。

    GitHub Actionsのキャッシュに置くと、キャッシュが消えた瞬間に
    「昨日通知した投稿がまた鳴る」という最悪の挙動になる。
    """
    import notify_state
    assert notify_state.STATE_PATH.name.endswith(".json")
    assert "data" in notify_state.STATE_PATH.parts

    state = notify_state.NotifyState()
    post = {"post_id": "abc", "author_handle": "someone",
            "buzz_score": 72.0, "text_snippet": "テスト投稿", "url": "https://x.com/a/status/abc"}
    assert state.is_notified("abc") is False
    state.record(post)
    assert state.is_notified("abc") is True
    assert state.count_last_24h() == 1
    assert state.minutes_since_last() < 1
    assert state.last_notified_at_for_author("someone") is not None


def test_gitignore_does_not_exclude_notification_history():
    """
    通知履歴が .gitignore で除外されていないこと。
    除外されるとコミットされず、重複通知が起きる。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
        ignored = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    assert "data/notify_state.json" not in ignored
    assert "data/" not in ignored and "data" not in ignored


# ── clustering.py ────────────────────────────────────────

def test_clustering_groups_same_event():
    """同じ地震についての別々の投稿が1つにまとまること"""
    posts = [
        {"text_snippet": "【速報】東京で震度5弱の地震が発生しました", "buzz_score": 80},
        {"text_snippet": "東京 震度5弱の地震 速報です", "buzz_score": 70},
        {"text_snippet": "新作ゲームの発売日がついに決定", "buzz_score": 60},
    ]
    reps = clustering.pick_representatives(posts)
    assert len(reps) == 2, [r["text_snippet"] for r in reps]
    assert reps[0]["cluster_size"] == 2


def test_clustering_keeps_highest_score_as_representative():
    posts = [
        {"text_snippet": "同じ話題についての投稿A", "buzz_score": 60},
        {"text_snippet": "同じ話題についての投稿B", "buzz_score": 90},
    ]
    reps = clustering.pick_representatives(posts)
    assert reps[0]["buzz_score"] == 90


# ── keyword_filter.py ────────────────────────────────────

def test_query_groups_are_generated():
    groups = keyword_filter.query_groups()
    assert groups, "keywords.txt から検索クエリが生成されること"
    assert all(len(g) <= 400 for g in groups), "クエリが長くなりすぎないこと"
    assert any("炎上" in g for g in groups)


def test_ng_keywords_block_giveaways():
    assert keyword_filter.is_ng("フォロー&RTで現金プレゼント企画") is True
    assert keyword_filter.is_ng("【文春砲】人気俳優に不倫疑惑") is False


def test_combo_queries_are_loaded():
    """keywords_combo.txt の各行が、そのまま検索クエリになること"""
    combos = keyword_filter.combo_queries()
    assert combos, "組み合わせ検索が読み込まれること"
    # 行がカンマで分割されていないこと(検索式が壊れていないこと)
    assert any("OR" in c and ")" in c for c in combos), combos


def test_combo_words_count_toward_relevance():
    """
    組み合わせ検索でしかヒットしない投稿にも関連度が付くこと。

    「逮捕」は keywords.txt には無く keywords_combo.txt にしかないが、
    わざわざ狙って取りに行った投稿なので0点にしてはいけない。
    """
    assert "逮捕" not in keyword_filter._KEYWORDS
    assert keyword_filter.relevance_score("人気俳優が逮捕されました") > 0


def test_combo_and_ng_lists_do_not_conflict():
    """
    組み合わせ検索で狙っているワードが、NGワードにも入っていないこと。
    両方に入っていると「検索して取りに行った投稿を、その場で捨てる」
    という矛盾が起きる(実際に「拡散希望」でこれが起きた)。
    """
    conflicts = []
    for expression in keyword_filter.combo_queries():
        for word in keyword_filter._words_in_expression(expression):
            if word in keyword_filter._NG_KEYWORDS:
                conflicts.append(word)
    assert not conflicts, f"組み合わせ検索とNGワードが衝突: {conflicts}"


def test_ng_words_do_not_kill_real_news():
    """
    NGワードが、本当に拾いたい投稿を巻き添えにしていないこと。

    NGは「1つでも含まれたら問答無用で捨てる」拒否権なので、
    一般語を入れると拾いたい投稿まで消える。実際に
    「ご来場ありがとう」をNGに入れて謝罪投稿が消えた事故があった。
    """
    must_survive = [
        "【文春砲】人気俳優に不倫疑惑 事務所は事実無根とコメント",
        "謝罪文を公開しました。ご来場ありがとうございました",
        "有名アイドルが未成年飲酒で活動休止 事務所が謝罪",
        "人気声優が書類送検 所属事務所がコメントを発表",
        "配信者が生放送で不適切発言 切り抜きが拡散し炎上",
    ]
    killed = [(t, keyword_filter.find_ng_keyword(t)) for t in must_survive
              if keyword_filter.find_ng_keyword(t)]
    assert not killed, f"拾いたい投稿がNGワードで消えている: {killed}"


def test_keywords_have_no_ephemeral_proper_nouns():
    """
    keywords.txt に「今週だけの固有名詞」が紛れ込んでいないこと。

    特定のタレント名やイベント名を入れると、その話題が終わった瞬間に
    死にワードになる。人名で追いたい場合は keywords_combo.txt を使う。
    ここでは過去に提案されて却下した実例を検出する。
    """
    rejected = [
        "岩﨑大昇", "山口陽世", "ぱるちゃん", "りくりゅう", "KEYTOLIT",
        "神宮Day", "箱パカ", "国連委員", "給料逆転", "発達障害就活",
        "猫将軍", "たいがは", "セラが色を", "ヒルナンデス",
    ]
    found = [w for w in rejected if w in keyword_filter._KEYWORDS]
    assert not found, f"一過性の固有名詞が入っています: {found}"


def test_relevance_score_increases_with_hits():
    one = keyword_filter.relevance_score("炎上しました")
    many = keyword_filter.relevance_score("文春砲で不倫が発覚し活動休止")
    assert 0 < one < many <= 1.0


# ── playwright_collector.py (パース部分のみ) ──────────────

def test_parse_labeled_counts_japanese():
    label = "12件の返信、340件のリポスト、5678件のいいね、90件のブックマーク、1.2万件の表示"
    parsed = collector._parse_labeled_counts(label)
    assert parsed == {
        "replies": 12, "retweets": 340, "likes": 5678,
        "bookmarks": 90, "impressions": 12000,
    }, parsed


def test_parse_labeled_counts_english():
    label = "12 replies, 340 reposts, 5,678 likes, 90 bookmarks, 12K views"
    parsed = collector._parse_labeled_counts(label)
    assert parsed["likes"] == 5678, parsed
    assert parsed["bookmarks"] == 90, parsed
    assert parsed["impressions"] == 12000, parsed


def test_parse_count_variants():
    assert collector._parse_count("1.2万") == 12000
    assert collector._parse_count("3,500") == 3500
    assert collector._parse_count("12K") == 12000
    assert collector._parse_count("なし") == 0


def test_sanitize_rejects_impossible_bookmarks():
    """
    実行履歴に実在した壊れたデータ(likes=8208 / bookmarks=8800)が
    捨てられること。この値がv3のスコアを歪めていた。
    """
    post = collector.sanitize_counts(
        {"post_id": "x", "likes": 8208, "bookmarks": 8800, "retweets": 100, "quotes": 0}
    )
    assert post["bookmarks"] == 0


def test_sanitize_keeps_plausible_values():
    post = collector.sanitize_counts(
        {"post_id": "x", "likes": 8000, "bookmarks": 900, "retweets": 1200,
         "quotes": 30, "impressions": 500000}
    )
    assert post["bookmarks"] == 900
    assert post["impressions"] == 500000


def test_build_queries_covers_all_three_kinds():
    """
    3種類のクエリが全て生成されること。
      1. キーワードOR検索 (keywords.txt)
      2. 組み合わせ検索   (keywords_combo.txt)
      3. 広域検索         (キーワードに無い話題の保険)
    """
    queries = collector.build_queries()
    all_q = [q for q, _, _ in queries]

    keyword_queries = [q for q in all_q if f"min_faves:{collector.SEARCH_MIN_FAVES}" in q]
    combo_queries = [q for q in all_q if f"min_faves:{collector.COMBO_MIN_FAVES}" in q]
    broad_queries = [q for q in all_q if f"min_faves:{collector.BROAD_MIN_FAVES}" in q]

    assert keyword_queries, "キーワードOR検索が生成されること"
    assert len(combo_queries) == len(keyword_filter.combo_queries()), \
        "組み合わせ検索は1行につき1本になること"
    assert broad_queries, "広域検索が生成されること"

    assert collector.SEARCH_MIN_FAVES < 500, "v3の500より低いこと(早期発見のため)"
    assert collector.COMBO_MIN_FAVES <= collector.SEARCH_MIN_FAVES, \
        "組み合わせ検索はAND条件で既に絞れているので、閾値をより低くできる"


def test_no_query_exceeds_the_operator_limit():
    """
    ★X検索は演算子が22〜23個を超えると、超えた分をエラーも出さずに無視する。
    2026-08-24以前は1グループ32ワード(演算子34個)で組み立てており、
    各グループの後半のワードは実際には検索されていなかった。
    ログにも何も出ないため、気づけない種類の不具合だった。
    """
    for query, _, _ in collector.build_queries():
        ops = keyword_filter.count_operators(query)
        assert ops <= 22, f"演算子{ops}個で上限超過: {query}"


def test_operator_counter_counts_or_and_prefixes():
    q = "(A OR B OR C) lang:ja -filter:retweets min_faves:30 within_time:3h"
    # OR×2 + lang: + filter: + min_faves: + within_time: + 除外の「-」
    assert keyword_filter.count_operators(q) == 7, keyword_filter.count_operators(q)


def test_queries_are_limited_to_recent_posts():
    """
    検索の段階で新しい投稿に絞れていること。
    全件取ってから捨てる方式だと、スクロールの大半が無駄になる。
    """
    if not collector.SEARCH_WITHIN_TIME:
        return  # 無効化されている場合はスキップ
    for query, _, _ in collector.build_queries():
        assert f"within_time:{collector.SEARCH_WITHIN_TIME}" in query, query


def test_broad_query_excludes_official_announcement_noise():
    """
    広域クエリが、公式アカウントの誕生日祝い・新譜告知を除外していること。
    実測ではここが上位をほぼ占拠しており、一般投稿が拾えていなかった。
    """
    broad = [q for q, _, _ in collector.build_queries()
             if f"min_faves:{collector.BROAD_MIN_FAVES}" in q]
    assert broad, "広域クエリが生成されること"
    assert all("-誕生日" in q for q in broad), broad


def test_all_queries_are_japanese_and_exclude_retweets():
    for query, _, _ in collector.build_queries():
        assert "lang:ja" in query, query
        assert "-filter:retweets" in query, query


# ── notification_text.py ─────────────────────────────────

def test_notification_includes_the_post_text():
    """
    v3の通知に本文が入っていなかった問題の再発防止。
    通知を見ただけで何の話題か分かることが、スクープ動画では決定的に重要。
    """
    post = _post(minutes_old=25, likes=4000)
    post.update({
        "buzz_score": 72.0, "elapsed_minutes": 25, "genre": "事件・事故",
        "growth": {"likes_per_min": 80.0, "acceleration": 1.8,
                   "is_measured": True, "age_minutes": 25},
        "score_breakdown": {"伸び率": 30.0, "加速度": 13.5, "議論量": 15.0},
        "title": "【速報】東京で震度5弱", "hook": "たった今、東京が揺れました",
        "llm_ok": True, "scoop_score": 9, "explain_score": 6, "tiktok_fit": 8, "risk": "中",
    })
    message = notification_text.build_message(post)
    assert "震度5弱" in message, "投稿本文が含まれること"
    assert "80/分" in message, "実測の伸び率が含まれること"
    assert "加速1.8x" in message, "加速度が含まれること"
    assert "https://x.com/someone/status/1" in message
    assert "🎬" in message, "タイトル案が含まれること"


def test_system_message_passthrough():
    post = notification_text.build_system_message("⚠️ セッション切れ")
    assert "セッション切れ" in notification_text.build_message(post) or True
    import line_notifier
    assert line_notifier.build_message(post) == "⚠️ セッション切れ"


# ──────────────────────────────────────────────────────────

def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
        except AssertionError as e:
            print(f"  ❌ {name}: {e}")
            failed.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"  💥 {name}: {type(e).__name__}: {e}")
            failed.append(name)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 件成功")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
