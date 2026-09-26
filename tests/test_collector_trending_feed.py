import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class TrendingFeedAgeTests(unittest.TestCase):
    def test_age_is_recomputed_from_posted_at(self):
        age = main._age_minutes_at(
            "2026-09-26T01:20:15+00:00",
            "2026-09-26T13:20:15+00:00",
        )
        self.assertAlmostEqual(age, 720.0, places=2)

    def test_invalid_posted_at_is_not_eligible(self):
        self.assertIsNone(main._age_minutes_at("bad-date", "2026-09-26T13:20:15+00:00"))

    def test_trending_excludes_actual_age_over_24h_even_when_last_age_is_stale(self):
        started = "2026-09-26T13:20:15+00:00"
        watch_state = {
            "old": {
                "post_id": "old",
                "author_handle": "old_author",
                "posted_at": "2026-09-22T22:31:00+00:00",
                "last_age_minutes": 481.3,
                "last_impressions": 10_316_117,
                "last_impressions_per_min": 500,
                "text_snippet": "stale row",
                "url": "https://x.com/i/status/1",
            },
            "fresh": {
                "post_id": "fresh",
                "author_handle": "fresh_author",
                "posted_at": "2026-09-26T01:20:15+00:00",
                "last_age_minutes": 300,
                "last_impressions": 2_000_000,
                "last_impressions_per_min": 100,
                "text_snippet": "fresh row",
                "url": "https://x.com/i/status/2",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hits_json = root / "hits.json"
            hits_md = root / "hits.md"
            with patch.object(main, "HITS_JSON_PATH", hits_json), patch.object(main, "HITS_FILE_PATH", hits_md):
                main._write_hits([], [], started, watch_state)
            payload = json.loads(hits_json.read_text(encoding="utf-8"))

        self.assertEqual([p["post_id"] for p in payload["trending_posts"]], ["fresh"])
        self.assertAlmostEqual(payload["trending_posts"][0]["age_minutes"], 720.0, places=2)


if __name__ == "__main__":
    unittest.main()
