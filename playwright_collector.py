"""
playwright_collector.py
ランタイムで既知の良い実装を取得し、2026-09の診断パッチを当てる。
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

_URL = (
    "https://raw.githubusercontent.com/mnaoki20081106-afk/X-Bunseki/"
    "f909cd63b49b0142011487e5f0c177b601821f30/playwright_collector.py"
)
_CACHE = Path(__file__).with_name("_playwright_collector_impl.py")


def _ensure() -> Path:
    if not _CACHE.exists() or _CACHE.stat().st_size < 5000:
        data = urllib.request.urlopen(_URL, timeout=45).read().decode("utf-8")
        # within_time 既定オフ
        data = data.replace(
            'SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "3h")',
            'SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "off")',
        )
        # ヘッドレス緩和 + UA
        old = (
            "        browser = p.chromium.launch(headless=True)\n"
            "        context = browser.new_context(storage_state=session_path)\n"
            "        page = context.new_page()\n"
        )
        new = (
            "        browser = p.chromium.launch(\n"
            "            headless=True,\n"
            "            args=[\"--disable-blink-features=AutomationControlled\", \"--no-sandbox\"],\n"
            "        )\n"
            "        context = browser.new_context(\n"
            "            storage_state=session_path,\n"
            "            user_agent=(\n"
            "                \"Mozilla/5.0 (Windows NT 10.0; Win64; x64) \"\n"
            "                \"AppleWebKit/537.36 (KHTML, like Gecko) \"\n"
            "                \"Chrome/128.0.0.0 Safari/537.36\"\n"
            "            ),\n"
            "            locale=\"ja-JP\",\n"
            "            viewport={\"width\": 1280, \"height\": 900},\n"
            "        )\n"
            "        page = context.new_page()\n"
            "        try:\n"
            "            page.add_init_script(\n"
            "                \"Object.defineProperty(navigator, 'webdriver', { get: () => undefined });\"\n"
            "            )\n"
            "        except Exception:\n"
            "            pass\n"
        )
        if old in data:
            data = data.replace(old, new)
        # セッション確認
        marker = "        queries = build_queries()"
        smoke = (
            "        # セッション健全性\n"
            "        try:\n"
            "            print(\"  [セッション確認] https://x.com/home …\")\n"
            "            page.goto(\"https://x.com/home\", wait_until=\"domcontentloaded\", timeout=45000)\n"
            "            page.wait_for_timeout(6000)\n"
            "            title = \"\"\n"
            "            try:\n"
            "                title = page.title() or \"\"\n"
            "            except Exception:\n"
            "                pass\n"
            "            print(f\"  [セッション確認] URL={page.url} title={title[:60]!r}\")\n"
            "            if \"/login\" in page.url or \"/i/flow/login\" in page.url:\n"
            "                raise SessionExpiredError(\"ホームがログイン画面 → セッション切れ\")\n"
            "            if \"しばらくお待ちください\" in title:\n"
            "                page.reload(wait_until=\"domcontentloaded\", timeout=45000)\n"
            "                page.wait_for_timeout(10000)\n"
            "                title = page.title() or \"\"\n"
            "            if \"しばらくお待ちください\" in title:\n"
            "                raise SessionExpiredError(\n"
            "                    \"ホームが『しばらくお待ちください』のまま。セッション無効の可能性。\"\n"
            "                )\n"
            "        except SessionExpiredError:\n"
            "            browser.close()\n"
            "            raise\n"
            "        except Exception as e:\n"
            "            print(f\"  [セッション確認] 警告: {e}\")\n"
            "\n"
        )
        if marker in data and "しばらくお待ちください" not in data:
            data = data.replace(marker, smoke + marker)
        _CACHE.write_text(data, encoding="utf-8")
    return _CACHE


_path = _ensure()
_ns = {"__name__": "playwright_collector"}
exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), _ns)
SessionExpiredError = _ns["SessionExpiredError"]
fetch_posts = _ns["fetch_posts"]
globals().update({k: v for k, v in _ns.items() if not k.startswith("_")})
