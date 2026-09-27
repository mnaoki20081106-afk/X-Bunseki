from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUTPUT = Path("x-monitor-private-snapshot.json")


def load_json(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    payload = {
        "hits": load_json("hits.json"),
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
