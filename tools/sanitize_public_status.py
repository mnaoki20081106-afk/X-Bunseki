from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATUS_PATH = Path("status.json")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if key in source}


def sanitize_status(raw: dict[str, Any]) -> dict[str, Any]:
    health = as_dict(raw.get("collection_health"))
    rate = as_dict(health.get("search_rate_limit"))
    observation = as_dict(raw.get("observation_db"))

    public_health = pick(
        health,
        (
            "status",
            "error_code",
            "search_succeeded",
            "search_failed",
            "detail_succeeded",
            "detail_failed",
            "detail_skipped",
            "detail_unavailable",
            "detail_incomplete",
            "invalid_tweets",
            "incomplete_search_pages",
        ),
    )
    if rate:
        public_health["search_rate_limit"] = pick(
            rate,
            ("limit", "remaining", "reset"),
        )

    result = pick(
        raw,
        (
            "updated_at",
            "status",
            "started_at",
            "finished_at",
            "posts_scanned",
            "posts_with_history",
            "posts_flagged",
            "notified_count",
            "notified_last_24h",
            "minutes_since_last_notification",
            "viral_discovery_posts",
            "million_imp_rescued",
            "watchlist_active",
        ),
    )
    result["collection_health"] = public_health
    if observation:
        result["observation_db"] = pick(
            observation,
            ("total", "posts", "oldest"),
        )
    return result


def main() -> int:
    if not STATUS_PATH.exists():
        print("status.json missing; nothing to sanitize")
        return 0

    try:
        raw = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"status.json is invalid JSON: {exc}")

    if not isinstance(raw, dict):
        raise SystemExit("status.json must contain an object")

    sanitized = sanitize_status(raw)
    STATUS_PATH.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("status.json sanitized for public repository")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
