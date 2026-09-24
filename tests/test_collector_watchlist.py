import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import watchlist


class WatchlistRescheduleTests(unittest.TestCase):
    def test_skipped_detail_advances_to_next_checkpoint(self):
        now = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
        posted = now - timedelta(minutes=20)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            path.write_text(json.dumps({
                "123": {
                    "post_id": "123",
                    "posted_at": posted.isoformat(),
                    "next_due_at": (posted + timedelta(minutes=15)).isoformat(),
                    "next_target_minutes": 15,
                    "completed": False,
                }
            }), encoding="utf-8")
            with patch.object(watchlist, "PATH", path):
                state = watchlist.mark_detail_skipped(["123"], now=now)
                row = state["123"]
                self.assertFalse(row["completed"])
                self.assertEqual(row["next_target_minutes"], 30)
                self.assertGreater(
                    datetime.fromisoformat(row["next_due_at"]),
                    now,
                )
                self.assertEqual(row["consecutive_detail_skips"], 1)
                self.assertEqual(watchlist.due_posts(now=now), [])

    def test_unseen_rows_age_out_after_last_checkpoint(self):
        now = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
        posted = now - timedelta(minutes=1500)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.json"
            path.write_text(json.dumps({
                "old": {
                    "post_id": "old",
                    "posted_at": posted.isoformat(),
                    "next_due_at": (posted + timedelta(minutes=1440)).isoformat(),
                    "next_target_minutes": 1440,
                    "completed": False,
                    "added_at": posted.isoformat(),
                }
            }), encoding="utf-8")
            with patch.object(watchlist, "PATH", path):
                state = watchlist.update([], now=now)
                self.assertTrue(state["old"]["completed"])
                self.assertIsNone(state["old"]["next_due_at"])
                self.assertEqual(watchlist.due_posts(now=now), [])


if __name__ == "__main__":
    unittest.main()
