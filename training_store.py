from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Iterable, Iterator

LEGACY_PATH = Path("data/training_snapshots.jsonl")
SHARD_DIR = Path("data/training_snapshots")
DEFAULT_SHARD_MAX_BYTES = 32 * 1024 * 1024
_SHARD_RE = re.compile(r"^part-(\d{6})\.jsonl$")


def _iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except Exception:
                    continue
                if isinstance(value, dict):
                    yield value
    except FileNotFoundError:
        return


def snapshot_paths(
    legacy_path: Path = LEGACY_PATH,
    shard_dir: Path = SHARD_DIR,
) -> list[Path]:
    shards = sorted(
        (p for p in shard_dir.glob("part-*.jsonl") if p.is_file()),
        key=lambda p: p.name,
    ) if shard_dir.exists() else []
    # Once shards exist they are authoritative. This avoids double-reading if
    # a migration was interrupted after shard creation but before legacy unlink.
    if shards:
        return shards
    return [legacy_path] if legacy_path.exists() else []


def iter_snapshot_rows(
    legacy_path: Path = LEGACY_PATH,
    shard_dir: Path = SHARD_DIR,
) -> Iterator[dict]:
    for path in snapshot_paths(legacy_path, shard_dir):
        yield from _iter_jsonl(path)


def _next_shard_index(shard_dir: Path) -> int:
    highest = 0
    if shard_dir.exists():
        for path in shard_dir.glob("part-*.jsonl"):
            match = _SHARD_RE.match(path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest or 1


def _encoded_row(row: dict) -> bytes:
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def append_snapshot_rows(
    rows: Iterable[dict],
    *,
    shard_dir: Path = SHARD_DIR,
    max_bytes: int = DEFAULT_SHARD_MAX_BYTES,
) -> tuple[int, list[Path]]:
    shard_dir.mkdir(parents=True, exist_ok=True)
    index = _next_shard_index(shard_dir)
    current = shard_dir / f"part-{index:06d}.jsonl"
    touched: list[Path] = []
    written = 0

    for row in rows:
        encoded = _encoded_row(row)
        size = current.stat().st_size if current.exists() else 0
        if size > 0 and size + len(encoded) > max_bytes:
            index += 1
            current = shard_dir / f"part-{index:06d}.jsonl"
            size = 0
        with current.open("ab") as handle:
            handle.write(encoded)
        if not touched or touched[-1] != current:
            touched.append(current)
        written += 1

    return written, touched


def migrate_legacy_snapshots(
    *,
    legacy_path: Path = LEGACY_PATH,
    shard_dir: Path = SHARD_DIR,
    max_bytes: int = DEFAULT_SHARD_MAX_BYTES,
) -> dict:
    if not legacy_path.exists():
        return {"migrated": False, "rows": 0, "shards": len(snapshot_paths(legacy_path, shard_dir))}

    existing_shards = list(shard_dir.glob("part-*.jsonl")) if shard_dir.exists() else []
    if existing_shards:
        raise RuntimeError(
            "legacy snapshot file and shard directory both exist; refusing a duplicate migration"
        )

    temp_dir = shard_dir.with_name(shard_dir.name + ".migrating")
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    rows = 0
    bad_lines = 0
    index = 1
    current = temp_dir / f"part-{index:06d}.jsonl"
    current_size = 0

    with legacy_path.open("r", encoding="utf-8") as source:
        for raw in source:
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("snapshot row is not an object")
                encoded = _encoded_row(value)
            except Exception:
                # Preserve malformed legacy bytes instead of silently deleting them.
                # Current readers already skip malformed JSONL rows.
                bad_lines += 1
                encoded = raw.rstrip("\n").encode("utf-8") + b"\n"

            if current_size > 0 and current_size + len(encoded) > max_bytes:
                index += 1
                current = temp_dir / f"part-{index:06d}.jsonl"
                current_size = 0
            with current.open("ab") as handle:
                handle.write(encoded)
            current_size += len(encoded)
            rows += 1

    shard_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.rename(shard_dir)
    legacy_size = legacy_path.stat().st_size
    legacy_path.unlink()

    shards = sorted(shard_dir.glob("part-*.jsonl"))
    largest = max((p.stat().st_size for p in shards), default=0)
    return {
        "migrated": True,
        "rows": rows,
        "bad_lines": bad_lines,
        "legacy_bytes": legacy_size,
        "shards": len(shards),
        "largest_shard_bytes": largest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=int(os.environ.get("TRAINING_SNAPSHOT_SHARD_MAX_BYTES", DEFAULT_SHARD_MAX_BYTES)),
    )
    args = parser.parse_args()

    if args.migrate:
        result = migrate_legacy_snapshots(max_bytes=max(1024, args.max_bytes))
        print("[training-store] " + json.dumps(result, ensure_ascii=False))
    else:
        paths = snapshot_paths()
        print(
            "[training-store] "
            + json.dumps(
                {
                    "paths": [str(p) for p in paths],
                    "total_bytes": sum(p.stat().st_size for p in paths if p.exists()),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
