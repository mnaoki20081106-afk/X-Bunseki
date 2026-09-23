import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from playwright_collector import CollectionError, SessionExpiredError, decode_collection


class CollectorHealthTests(unittest.TestCase):
    def test_valid_empty_is_not_failure(self):
        posts = decode_collection(json.dumps({"posts": [], "health": {"status": "success"}}))
        self.assertEqual(posts, [])
        self.assertEqual(posts.health["status"], "success")

    def test_partial_observations_remain_list_compatible(self):
        posts = decode_collection(json.dumps({"posts": [{"post_id": "123"}], "health": {"status": "degraded"}}))
        self.assertEqual([p["post_id"] for p in posts], ["123"])
        self.assertEqual(posts.health["status"], "degraded")

    def test_failed_collection_never_returns_empty_success(self):
        for status in ("rate_limited", "schema_changed", "account_restricted", "upstream_error"):
            with self.subTest(status=status), self.assertRaises(CollectionError):
                decode_collection(json.dumps({"posts": [], "health": {"status": status}}))

    def test_auth_failure_retains_existing_relogin_path(self):
        with self.assertRaises(SessionExpiredError):
            decode_collection(json.dumps({"posts": [], "health": {"status": "session_expired"}}))

    def test_malformed_output_is_not_misclassified_as_bad_login(self):
        for output in ("", "[]", "null", "not json"):
            with self.subTest(output=output), self.assertRaises(CollectionError):
                decode_collection(output)

    def test_failed_cycle_preserves_published_hits_and_does_not_observe(self):
        import main
        with tempfile.TemporaryDirectory() as directory:
            hits = Path(directory) / 'hits.json'
            status = Path(directory) / 'status.json'
            hits.write_text('{"posts":[{"post_id":"123"}]}')
            original = hits.read_bytes()
            error = CollectionError({"status": "schema_changed", "error_code": "schema_changed"})
            with patch.object(main, 'HITS_JSON_PATH', hits), patch.object(main, 'STATUS_FILE_PATH', status), \
                 patch.object(main, '_validate_session_file', return_value=None), \
                 patch.object(main, 'fetch_posts', side_effect=error), \
                 patch.object(main.db, 'init_db'), patch.object(main.db, 'log_run'), \
                 patch.object(main.db, 'get_observation_history') as observations:
                with self.assertRaises(SystemExit) as stopped:
                    main.run_once()
                self.assertEqual(stopped.exception.code, 1)
                observations.assert_not_called()
            self.assertEqual(hits.read_bytes(), original)
            self.assertEqual(json.loads(status.read_text())['status'], 'error')


if __name__ == '__main__':
    unittest.main()
