import unittest
from datetime import datetime, timezone

from monitor_pacing import decide_pacing


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class MonitorPacingTests(unittest.TestCase):
    def test_runs_without_previous_status(self):
        self.assertEqual(decide_pacing({}, now_ts=1_000_000)[0], "run")

    def test_skips_close_duplicate_dispatch(self):
        now = 1_000_000
        mode, wait, _ = decide_pacing(
            {"updated_at": iso(now - 8 * 60)},
            now_ts=now,
        )
        self.assertEqual((mode, wait), ("skip", 0))

    def test_waits_to_keep_fallback_window_separated(self):
        now = 1_000_000
        mode, wait, _ = decide_pacing(
            {"updated_at": iso(now - 14 * 60)},
            now_ts=now,
        )
        self.assertEqual(mode, "wait")
        self.assertEqual(wait, 2 * 60)

    def test_runs_after_fallback_interval(self):
        now = 1_000_000
        self.assertEqual(
            decide_pacing({"updated_at": iso(now - 17 * 60)}, now_ts=now)[0],
            "run",
        )

    def test_prefers_recorded_x_reset_and_waits_with_grace(self):
        now = 1_000_000
        status = {
            "updated_at": iso(now - 60),
            "collection_health": {
                "search_rate_limit": {"limit": 50, "remaining": 14, "reset": now + 100}
            },
        }
        mode, wait, _ = decide_pacing(status, now_ts=now)
        self.assertEqual(mode, "wait")
        self.assertEqual(wait, 120)

    def test_skips_when_recorded_reset_is_beyond_wait_budget(self):
        now = 1_000_000
        status = {
            "collection_health": {
                "search_rate_limit": {"limit": 50, "remaining": 14, "reset": now + 500}
            }
        }
        self.assertEqual(decide_pacing(status, now_ts=now)[0], "skip")

    def test_runs_when_recorded_reset_has_passed(self):
        now = 1_000_000
        status = {
            "updated_at": iso(now - 60),
            "collection_health": {
                "search_rate_limit": {"limit": 50, "remaining": 50, "reset": now - 30}
            },
        }
        self.assertEqual(decide_pacing(status, now_ts=now)[0], "run")


if __name__ == "__main__":
    unittest.main()
