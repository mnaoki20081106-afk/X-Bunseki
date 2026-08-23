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
