import json
import tempfile
import unittest
from pathlib import Path

import training_store


class TrainingStoreTests(unittest.TestCase):
    def test_migrates_legacy_file_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "training_snapshots.jsonl"
            shards = root / "training_snapshots"
            rows = [
                {
                    "post_id": str(i),
                    "observed_at": f"2026-09-23T00:{i:02d}:00+00:00",
                    "impressions": i * 100,
                    "text": "x" * 80,
                }
                for i in range(12)
            ]
            legacy.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )

            result = training_store.migrate_legacy_snapshots(
                legacy_path=legacy,
                shard_dir=shards,
                max_bytes=700,
            )

            self.assertTrue(result["migrated"])
            self.assertFalse(legacy.exists())
            paths = training_store.snapshot_paths(legacy, shards)
            self.assertGreater(len(paths), 1)
            self.assertTrue(all(path.stat().st_size <= 700 for path in paths))
            self.assertEqual(list(training_store.iter_snapshot_rows(legacy, shards)), rows)

    def test_append_rotates_shards_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shards = root / "training_snapshots"
            rows = [
                {"post_id": str(i), "observed_at": f"t{i}", "payload": "y" * 100}
                for i in range(10)
            ]
            written, touched = training_store.append_snapshot_rows(
                rows, shard_dir=shards, max_bytes=500
            )
            self.assertEqual(written, len(rows))
            self.assertGreater(len(touched), 1)
            self.assertEqual(
                list(training_store.iter_snapshot_rows(root / "legacy.jsonl", shards)),
                rows,
            )

    def test_shards_are_authoritative_if_legacy_also_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "training_snapshots.jsonl"
            shards = root / "training_snapshots"
            legacy.write_text('{"post_id":"legacy"}\n', encoding="utf-8")
            training_store.append_snapshot_rows(
                [{"post_id": "shard"}], shard_dir=shards, max_bytes=1024
            )
            rows = list(training_store.iter_snapshot_rows(legacy, shards))
            self.assertEqual([row["post_id"] for row in rows], ["shard"])


if __name__ == "__main__":
    unittest.main()
