from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

OUTPUT = Path("x-monitor-private-snapshot.json")


def _as_dict(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def load_json(path: str) -> dict[str, Any]:
    try:
        return _as_dict(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}


def load_latest_historical_json(path: str) -> dict[str, Any]:
    """Return the newest committed version of path that still contains JSON.

    This is used only for the one-time bootstrap after hits.json was removed
    from the public repository. It does not contact X and it does not re-add
    the file to the working tree.
    """
    try:
        commits = subprocess.check_output(
            ["git", "log", "--format=%H", "--", path],
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {}

    for commit in commits:
        try:
            text = subprocess.check_output(
                ["git", "show", f"{commit}:{path}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        value = _as_dict(text)
        if value:
            return value
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bootstrap-history",
        action="store_true",
        help="read the last committed hits.json when the live file was intentionally removed",
    )
    args = parser.parse_args()

    hits = load_json("hits.json")
    if args.bootstrap_history and not hits:
        hits = load_latest_historical_json("hits.json")

    if not hits:
        raise SystemExit("hits.json is unavailable; refusing to sync an empty premium feed")

    payload = {
        "hits": hits,
        "status": load_json("status.json"),
        "model_registry": load_json("data/model_registry.json"),
        "impression_model": load_json("data/impression_model.json"),
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"private snapshot bytes={OUTPUT.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
