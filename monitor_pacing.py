from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_MIN_INTERVAL_SECONDS = 16 * 60
DEFAULT_DUPLICATE_SKIP_BELOW_SECONDS = 10 * 60
DEFAULT_MAX_WAIT_SECONDS = 6 * 60
DEFAULT_RESET_GRACE_SECONDS = 20


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return max(0, int(value))
    except ValueError:
        return default


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def decide_pacing(
    status: dict[str, Any],
    *,
    now_ts: float | None = None,
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    duplicate_skip_below_seconds: int = DEFAULT_DUPLICATE_SKIP_BELOW_SECONDS,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
    reset_grace_seconds: int = DEFAULT_RESET_GRACE_SECONDS,
) -> tuple[str, int, str]:
    """Return (run|wait|skip, wait_seconds, reason).

    Prefer X's own SearchTimeline reset timestamp when the collector recorded it.
    Older status files fall back to spacing requests from the last completed cycle.
    """
    now = time.time() if now_ts is None else now_ts
    health = status.get("collection_health")
    if isinstance(health, dict):
        rate = health.get("search_rate_limit")
        if isinstance(rate, dict):
            reset = _positive_number(rate.get("reset"))
            if reset is not None:
                until_reset = math.ceil(reset + reset_grace_seconds - now)
                if until_reset <= 0:
                    return "run", 0, "recorded X SearchTimeline window has reset"
                if until_reset > max_wait_seconds:
                    return "skip", 0, f"X SearchTimeline reset is {until_reset}s away"
                return "wait", until_reset, f"waiting {until_reset}s for X SearchTimeline reset"

    updated = _timestamp(status.get("updated_at"))
    if updated is None:
        return "run", 0, "no usable previous collection timestamp"

    age = max(0, math.floor(now - updated))
    remaining = math.ceil(min_interval_seconds - age)
    if remaining <= 0:
        return "run", 0, f"previous collection is {age}s old"
    if age < duplicate_skip_below_seconds:
        return "skip", 0, f"duplicate dispatch: previous collection is only {age}s old"
    if remaining > max_wait_seconds:
        return "skip", 0, f"safe interval needs {remaining}s, above wait budget"
    return "wait", remaining, f"waiting {remaining}s to keep X collection windows separated"


def _load_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_output(skip: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as handle:
        handle.write(f"skip={'true' if skip else 'false'}\n")


def main() -> int:
    status_path = Path(os.environ.get("X_MONITOR_STATUS_PATH", "status.json"))
    mode, wait_seconds, reason = decide_pacing(
        _load_status(status_path),
        min_interval_seconds=_env_int(
            "X_MIN_COLLECTION_INTERVAL_SECONDS", DEFAULT_MIN_INTERVAL_SECONDS
        ),
        duplicate_skip_below_seconds=_env_int(
            "X_DUPLICATE_SKIP_BELOW_SECONDS", DEFAULT_DUPLICATE_SKIP_BELOW_SECONDS
        ),
        max_wait_seconds=_env_int("X_MAX_PACING_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS),
        reset_grace_seconds=_env_int("X_RATE_RESET_GRACE_SECONDS", DEFAULT_RESET_GRACE_SECONDS),
    )
    print(f"[pacing] {mode}: {reason}")
    if mode == "skip":
        _write_output(True)
        return 0

    _write_output(False)
    if mode == "wait" and wait_seconds > 0:
        time.sleep(wait_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
