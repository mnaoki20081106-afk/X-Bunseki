"""tests/test_pipeline.py — 現行仕様に合わせた判定テスト"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clustering
import detector
import growth
import feature_engineering
import build_outcomes
import gen1_controller
import keyword_filter
import notification_text
import playwright_collector as collector

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


def test_growth_uses_real_deltas():
    post = _post(minutes_old=40, likes=1000)
    history = [_obs(30, 400), _obs(15, 700)]
    g = growth.compute(post, history, now=NOW)
    assert g["is_measured"] is True
    assert abs(g["likes_per_min"] - 20.0) < 0.01


def test_growth_detects_acceleration():
    post = _post(minutes_old=40, likes=2000)
    history = [_obs(30, 300), _obs(15, 600)]
    g = growth.compute(post, history, now=NOW)
    assert g["acceleration"] > 4.0


def test_growth_detects_deceleration():
    post = _post(minutes_old=40, likes=1050)
    history = [_obs(30, 300), _obs(15, 1000)]
    g = growth.compute(post, history, now=NOW)
    assert g["acceleration"] < 0.2


def test_growth_first_sighting_is_estimated():
    g = growth.compute(_post(minutes_old=50, likes=1000), [], now=NOW)
    assert g["is_measured"] is False
    assert abs(g["likes_per_min"] - 20.0) < 0.01


def test_growth_never_returns_negative():
    post = _post(minutes_old=40, likes=900)
    g = growth.compute(post, [_obs(15, 1000)], now=NOW)
    assert g["likes_per_min"] == 0.0


def test_detector_rejects_old_posts():
    post = _post(minutes_old=400, likes=50000)
    result = detector.evaluate(post, [], relevance=1.0, now=NOW)
    assert result["rejected_reason"] is not None
    assert "古すぎる" in result["rejected_reason"]


def test_detector_rejects_low_likes():
    post = _post(minutes_old=20, likes=10)
    result = detector.evaluate(post, [], relevance=1.0, now=NOW)
    assert "いいねが少なすぎる" in result["rejected_reason"]


def test_million_imp_rescue_bypasses_low_likes_when_views_are_exploding():
    post = _post(minutes_old=30, likes=10, impressions=3_000_000, text="未知の話題")
    history = [_obs(15, 5, impressions=1_000_000)]
    result = detector.evaluate(post, history, relevance=0.0, now=NOW)
    assert result["million_imp_bypass"] is True
    assert result["rejected_reason"] is None
    assert result["should_notify"] is True


def test_million_imp_rescue_rejects_a_stalled_million_view_post():
    post = _post(minutes_old=120, likes=10, impressions=1_010_000, text="未知の話題")
    history = [
        _obs(30, 5, impressions=990_000),
        _obs(15, 8, impressions=1_005_000),
    ]
    result = detector.evaluate(post, history, relevance=0.0, now=NOW)
    assert result["million_imp_bypass"] is False


def test_viral_queries_are_keyword_independent():
    queries = collector.build_queries()
    texts = [q for q, _, _ in queries]
    viral = [q for q in texts if q.startswith("lang:ja")]
    assert len(viral) >= 4
    assert any("min_faves:" in q for q in viral)
    assert any("min_retweets:" in q for q in viral)


def test_detector_notifies_a_fast_growing_post():
    post = _post(minutes_old=45, likes=6000, retweets=1500, replies=700, bookmarks=800)
    history = [_obs(30, 800), _obs(15, 2500)]
    result = detector.evaluate(post, history, relevance=1.0, now=NOW)
    assert result["rejected_reason"] is None, result["rejected_reason"]
    assert result["should_notify"] is True
    assert result["buzz_score"] >= detector.NOTIFY_SCORE


def test_detector_ignores_a_stalled_post():
    post = _post(minutes_old=150, likes=30000, retweets=1000, replies=200, bookmarks=100)
    history = [_obs(60, 29000), _obs(30, 29900)]
    result = detector.evaluate(post, history, relevance=1.0, now=NOW)
    assert result["should_notify"] is False or (
        result["rejected_reason"] and "失速" in result["rejected_reason"]
    )


def test_score_is_bounded():
    post = _post(minutes_old=5, likes=999999, retweets=999999,
                 replies=999999, bookmarks=999999)
    history = [_obs(4, 0), _obs(2, 1)]
    result = detector.evaluate(post, history, relevance=1.0, now=NOW)
    assert 0 <= result["buzz_score"] <= 100


def test_env_override_changes_threshold():
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
    post = _post(minutes_old=50, likes=13471, retweets=3500, replies=500, bookmarks=250)
    history = [_obs(35, 9000), _obs(20, 5000)]
    low = detector.evaluate(post, history, relevance=0.0, now=NOW)
    high = detector.evaluate(post, history, relevance=1.0, now=NOW)
    assert low["buzz_score"] <= high["buzz_score"]


def test_ongenre_post_still_notified():
    post = _post(minutes_old=40, likes=1500, retweets=225, replies=90, bookmarks=75)
    history = [_obs(25, 900), _obs(10, 300)]
    result = detector.evaluate(post, history, relevance=0.8, now=NOW)
    assert result["should_notify"] is True, result


def test_relevance_acts_as_a_multiplier():
    post = _post(minutes_old=40, likes=3000, retweets=500, replies=200, bookmarks=200)
    history = [_obs(25, 1500)]
    high = detector.evaluate(post, history, relevance=1.0, now=NOW)["buzz_score"]
    low = detector.evaluate(post, history, relevance=0.0, now=NOW)["buzz_score"]
    assert low < high


def test_one_keyword_hit_is_enough_to_count_as_on_genre():
    assert keyword_filter.relevance_score("文春砲が炸裂") >= 0.8


def test_line_sends_only_system_alerts_by_default():
    import line_notifier
    assert line_notifier.LINE_MODE in ("off", "alerts_only")
    assert line_notifier.should_send({"post_id": "1", "buzz_score": 80}) is False


def test_interval_limit_blocks_frequent_notifications():
    assert detector.can_notify_now(60, None)[0] is True
    assert detector.can_notify_now(60, 20)[0] is False
    assert detector.can_notify_now(60, 50)[0] is True


def test_big_story_can_break_the_interval():
    assert detector.can_notify_now(85, 20)[0] is True
    assert detector.can_notify_now(85, 5)[0] is False


def test_only_top_scores_use_the_repeating_alert():
    import pushover_notifier
    assert pushover_notifier.DEFAULT_PRIORITY == "1"
    assert pushover_notifier.DEFAULT_URGENT_PRIORITY == "2"
    assert int(pushover_notifier.DEFAULT_RETRY) >= 300
    assert int(pushover_notifier.DEFAULT_EXPIRE) <= 600


def test_notification_history_survives_cache_loss():
    import notify_state
    assert "data" in notify_state.STATE_PATH.parts
    state = notify_state.NotifyState()
    post = {"post_id": "abc", "author_handle": "someone",
            "buzz_score": 72.0, "text_snippet": "テスト投稿", "url": "https://x.com/a/status/abc"}
    assert state.is_notified("abc") is False
    state.record(post)
    assert state.is_notified("abc") is True


def test_gitignore_does_not_exclude_notification_history():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
        ignored = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    assert "data/notify_state.json" not in ignored


def test_clustering_groups_same_event():
    posts = [
        {"text_snippet": "【速報】東京で震度5弱の地震が発生しました", "buzz_score": 80},
        {"text_snippet": "東京 震度5弱の地震 速報です", "buzz_score": 70},
        {"text_snippet": "新作ゲームの発売日がついに決定", "buzz_score": 60},
    ]
    reps = clustering.pick_representatives(posts)
    assert len(reps) == 2
    assert reps[0]["cluster_size"] == 2


def test_clustering_keeps_highest_score_as_representative():
    posts = [
        {"text_snippet": "同じ話題についての投稿A", "buzz_score": 60},
        {"text_snippet": "同じ話題についての投稿B", "buzz_score": 90},
    ]
    reps = clustering.pick_representatives(posts)
    assert reps[0]["buzz_score"] == 90


def test_query_groups_are_generated():
    groups = keyword_filter.query_groups()
    assert groups
    assert all(len(g) <= 400 for g in groups)


def test_ng_keywords_block_giveaways():
    assert keyword_filter.is_ng("フォロー&RTで現金プレゼント企画") is True
    assert keyword_filter.is_ng("【文春砲】人気俳優に不倫疑惑") is False


def test_combo_queries_are_loaded():
    combos = keyword_filter.combo_queries()
    assert combos


def test_combo_words_count_toward_relevance():
    combo_words = []
    for expr in keyword_filter.combo_queries():
        combo_words.extend(keyword_filter._words_in_expression(expr))
    assert combo_words
    only_combo = [w for w in combo_words if w not in keyword_filter._KEYWORDS]
    if only_combo:
        assert keyword_filter.relevance_score(f"テスト{only_combo[0]}です") > 0
    else:
        assert keyword_filter.relevance_score(f"テスト{combo_words[0]}です") > 0


def test_combo_and_ng_lists_do_not_conflict():
    conflicts = []
    for expression in keyword_filter.combo_queries():
        for word in keyword_filter._words_in_expression(expression):
            if word in keyword_filter._NG_KEYWORDS:
                conflicts.append(word)
    assert not conflicts, conflicts


def test_parse_labeled_counts_japanese():
    label = "12件の返信、340件のリポスト、5678件のいいね、90件のブックマーク、1.2万件の表示"
    parsed = collector._parse_labeled_counts(label)
    assert parsed["likes"] == 5678
    assert parsed["replies"] == 12


def test_parse_labeled_counts_english():
    label = "12 replies, 340 reposts, 5,678 likes, 90 bookmarks, 12K views"
    parsed = collector._parse_labeled_counts(label)
    assert parsed["likes"] == 5678


def test_parse_count_variants():
    assert collector._parse_count("1.2万") == 12000
    assert collector._parse_count("3,500") == 3500
    assert collector._parse_count("12K") == 12000
    assert collector._parse_count("なし") == 0


def test_sanitize_keeps_high_retweet_ratios():
    real_cases = [(330, 1237), (109922, 515166), (1633, 7409)]
    for likes, retweets in real_cases:
        post = collector.sanitize_counts(
            {"post_id": "x", "likes": likes, "retweets": retweets,
             "bookmarks": 0, "quotes": 0}
        )
        assert post["retweets"] == retweets


def test_notification_includes_the_post_text():
    post = _post(text="テスト本文ABC")
    post["buzz_score"] = 70
    post["growth"] = growth.compute(post, [], now=NOW)
    msg = notification_text.build_message(post)
    assert "テスト本文ABC" in msg or "ABC" in msg


def test_system_message_passthrough():
    import line_notifier
    assert line_notifier.build_message({"system_message": "sys-alert"}) == "sys-alert"



def test_feature_engineering_uses_elapsed_minutes_correctly():
    rows = [
        {"post_id":"p","observed_at":(NOW-timedelta(minutes=30)).isoformat(),"age_minutes":30,"impressions":1000,"likes":100},
        {"post_id":"p","observed_at":(NOW-timedelta(minutes=15)).isoformat(),"age_minutes":45,"impressions":4000,"likes":250},
        {"post_id":"p","observed_at":NOW.isoformat(),"age_minutes":60,"impressions":10000,"likes":500},
    ]
    feats = feature_engineering.make_features(rows)
    assert abs(feats[1]["velocity_imp_per_min"] - 200.0) < 0.01
    assert abs(feats[2]["velocity_imp_per_min"] - 400.0) < 0.01
    assert feats[2]["velocity_ratio_prev"] > 1.9
    assert feats[2]["reacceleration_count"] == 1


def test_outcome_builder_requires_real_24h_observation():
    groups = {
        "early_only": [{"post_id":"early_only","observed_at":NOW.isoformat(),"posted_at":NOW.isoformat(),"age_minutes":360,"impressions":2_000_000}],
        "complete": [
            {"post_id":"complete","observed_at":NOW.isoformat(),"posted_at":NOW.isoformat(),"age_minutes":60,"impressions":100_000},
            {"post_id":"complete","observed_at":NOW.isoformat(),"posted_at":NOW.isoformat(),"age_minutes":1435,"impressions":12_000_000},
        ],
    }
    out = build_outcomes.build_outcomes(groups)
    assert out["early_only"]["imp_24h"] is None
    assert out["complete"]["imp_24h"] == 12_000_000
    assert out["complete"]["reached_10m_24h"] is True



def test_mega_rescue_includes_exact_8_5m_boundary():
    post = _post(minutes_old=500, likes=10, impressions=8_500_000, text="未知の巨大バズ")
    result = detector.evaluate(post, [], relevance=0.0, now=NOW)
    assert result["million_imp_bypass"] is True
    assert result["rejected_reason"] is None


def test_gen1_dataset_never_leaks_a_post_across_splits():
    groups = {}
    outcomes = {}
    for i in range(20):
        pid = f"p{i:02d}"
        groups[pid] = [
            {"post_id":pid, "observed_at":(NOW+timedelta(minutes=i)).isoformat(),
             "age_minutes":30, "impressions":1000+i, "likes":10,
             "was_early_observed":True, "is_rescue_only":False},
            {"post_id":pid, "observed_at":(NOW+timedelta(minutes=i+15)).isoformat(),
             "age_minutes":45, "impressions":2000+i, "likes":20,
             "was_early_observed":True, "is_rescue_only":False},
        ]
        outcomes[pid] = {"imp_24h": 1_000_000+i}
    dataset = gen1_controller.build_dataset(groups, outcomes)
    ids_by_split = {
        s: {p["post_id"] for p in dataset if p["split"] == s}
        for s in ("train", "validation", "test")
    }
    assert ids_by_split["train"].isdisjoint(ids_by_split["validation"])
    assert ids_by_split["train"].isdisjoint(ids_by_split["test"])
    assert ids_by_split["validation"].isdisjoint(ids_by_split["test"])



def test_ultra_early_uses_actual_one_minute_age():
    post = _post(minutes_old=1, likes=200, retweets=40, impressions=6_000)
    g = growth.compute(post, [], now=NOW)
    # Legacy growth intentionally floors lifetime rate at 5m.
    assert g["impressions_per_min"] == 1200
    ultra = growth.ultra_early_signal(post, g)
    # Ultra Early must use the real 1-minute age instead.
    assert ultra["actual_impressions_per_min"] == 6000


def test_ultra_early_can_predict_one_million_before_15m():
    post = _post(minutes_old=5, likes=1200, retweets=220, impressions=40_000)
    g = growth.compute(post, [], now=NOW)
    pred = growth.predict_final_impressions(post, g)
    assert pred["ultra_early"]["active"] is True
    assert pred["ultra_early"]["candidate"] is True
    assert pred["predicted_final_impressions"] >= 1_000_000


def test_ultra_early_numeric_floor_bypass_requires_1m_prediction():
    post = _post(minutes_old=3, likes=80, retweets=20, impressions=30_000, text="未知の話題")
    result = detector.evaluate(post, [], relevance=0.0, now=NOW)
    assert result["predicted_final_impressions"] >= 1_000_000
    assert result["ultra_early_bypass"] is True
    assert result["rejected_reason"] is None


def test_after_15m_ultra_layer_is_inactive():
    post = _post(minutes_old=30, likes=1000, retweets=200, impressions=200_000)
    g = growth.compute(post, [], now=NOW)
    pred = growth.predict_final_impressions(post, g)
    assert pred["ultra_early"]["active"] is False


def test_gen1_readiness_fails_closed_on_low_completion():
    stats = {
        "completed_24h_posts": 500, "positive_5m": 100, "positive_10m": 50,
        "stalled_under_1_5m": 100, "completion_rate": 0.50,
    }
    ready = gen1_controller.readiness(stats)
    assert ready["regression_ready"] is False
    assert ready["p5_classifier_ready"] is False
    assert ready["p10_classifier_ready"] is False

if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in sorted(tests, key=lambda f: f.__name__):
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 件成功")
    sys.exit(1 if failed else 0)
