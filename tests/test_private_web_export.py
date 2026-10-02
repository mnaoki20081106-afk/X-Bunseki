from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_sanitizer():
    path = ROOT / "tools" / "sanitize_public_status.py"
    spec = importlib.util.spec_from_file_location("sanitize_public_status", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_sanitizer():
    module = load_sanitizer()
    raw = {
        "updated_at": "2026-09-27T00:00:00+00:00",
        "status": "success",
        "posts_scanned": 123,
        "top5": [{"text": "premium text", "url": "https://x.com/i/status/1"}],
        "config": {"secret-ish": "not-needed"},
        "collection_health": {
            "status": "success",
            "error_code": None,
            "search_rate_limit": {"limit": 50, "remaining": 10, "reset": 123456},
            "failures": {"query text": "details"},
        },
        "observation_db": {"total": 10, "posts": 4, "oldest": "x"},
    }
    sanitized = module.sanitize_status(raw)
    assert sanitized["updated_at"] == raw["updated_at"]
    assert sanitized["posts_scanned"] == 123
    assert sanitized["collection_health"]["search_rate_limit"]["reset"] == 123456
    assert sanitized["observation_db"]["posts"] == 4
    assert "top5" not in sanitized
    assert "config" not in sanitized
    assert "failures" not in sanitized["collection_health"]


def test_workflow_private_sync_contract():
    text = (ROOT / ".github" / "workflows" / "monitor.yml").read_text(encoding="utf-8")
    assert "id-token: write" in text
    assert "push:" in text
    assert 'branches: [main]' in text
    assert '"tools/build_private_web_snapshot.py"' in text
    assert "https://post-link.net/api/internal/x-monitor-sync" in text
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in text
    assert "ACTIONS_ID_TOKEN_REQUEST_URL" in text
    assert "x-monitor-private-snapshot.json" in text
    assert "--bootstrap-history" in text
    assert "github.event.inputs.test_notification != 'true' && steps.pace.outputs.skip != 'true'" in text
    assert "github.event_name == 'push' || steps.pace.outputs.skip != 'true'" not in text

    builder = (ROOT / "tools" / "build_private_web_snapshot.py").read_text(encoding="utf-8")
    assert "load_latest_historical_json" in builder
    assert '["git", "log"' in builder
    assert '["git", "show"' in builder
    assert "refusing to sync an empty premium feed" in builder

    commit_line = next(
        line for line in text.splitlines()
        if "for p in status.json" in line
    )
    assert "hits.json" not in commit_line
    assert "hits.md" not in commit_line


def test_premium_feed_files_are_not_tracked_source_inputs():
    assert not (ROOT / "hits.json").exists()
    assert not (ROOT / "hits.md").exists()


def test_private_sync_requires_storage_confirmation():
    text = (ROOT / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
    step = text.split("      - name: Sync premium monitor snapshot privately\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    assert "X_MONITOR_SYNC_URL: https://www.post-link.net/api/internal/x-monitor-sync" in step
    assert "X_MONITOR_SYNC_AUDIENCE: https://post-link.net/api/internal/x-monitor-sync" in step
    script = "\n".join(line[10:] for line in step.split("        run: |\n", 1)[1].splitlines())
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "x-monitor-private-snapshot.json").write_text("{}", encoding="utf-8")
        curl = root / "curl"
        curl.write_text(
            '#!/bin/bash\n'
            'if [[ "$*" == *mock-oidc-endpoint* ]]; then\n'
            '  printf \'{"value":"test-token"}\'\n'
            'else\n'
            '  printf "%s" "$MOCK_STATUS"\n'
            '  exit "$MOCK_EXIT"\n'
            'fi\n',
            encoding="utf-8",
        )
        curl.chmod(0o755)
        for status, exit_code in [("204", "0"), ("308", "0"), ("200", "0"), ("000", "7"), ("401", "22")]:
            result = subprocess.run(
                ["bash", "-c", script], cwd=root, text=True, capture_output=True,
                env={
                    **os.environ,
                    "PATH": directory + os.pathsep + os.environ["PATH"],
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test",
                    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://mock-oidc-endpoint?test=1",
                    "X_MONITOR_SYNC_URL": "https://www.post-link.net/api/internal/x-monitor-sync",
                    "X_MONITOR_SYNC_AUDIENCE": "https://post-link.net/api/internal/x-monitor-sync",
                    "MOCK_STATUS": status,
                    "MOCK_EXIT": exit_code,
                },
            )
            expected_success = status == "204" and exit_code == "0"
            assert (result.returncode == 0) == expected_success, (status, result.stderr)
            assert ("private monitor snapshot synced (HTTP 204)" in result.stdout) == expected_success


if __name__ == "__main__":
    tests = [
        test_status_sanitizer,
        test_workflow_private_sync_contract,
        test_premium_feed_files_are_not_tracked_source_inputs,
        test_private_sync_requires_storage_confirmation,
    ]
    for test in tests:
        test()
        print(f"  ✅ {test.__name__}")
    print(f"{len(tests)}/{len(tests)} 件成功")
