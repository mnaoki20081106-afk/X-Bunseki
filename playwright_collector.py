"""
playwright_collector.py
ランタイムで既知の良い実装を取得し、パッチを当てる。

数値主導: キーワード無しの広域検索を早めの min_faves で回し、
文なし画像バズもメトリクスで拾えるようにする。
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
    # パッチ内容が変わったら再取得させる
    force = Path(__file__).with_name("_collector_patch_ver.txt")
    ver = "2026-09-20-metrics"
    if force.exists() and force.read_text().strip() == ver and _CACHE.exists() and _CACHE.stat().st_size >= 5000:
        return _CACHE

    data = urllib.request.urlopen(_URL, timeout=45).read().decode("utf-8")

    data = data.replace(
        'SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "3h")',
        'SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "off")',
    )
    # 広域はキーワード無し・数値のみ。早期発見のため床を下げる
    data = data.replace(
        'BROAD_MIN_FAVES = int(_env("BROAD_MIN_FAVES", "800"))',
        'BROAD_MIN_FAVES = int(_env("BROAD_MIN_FAVES", "250"))',
    )

    # build_queries 末尾にメトリクス専用クエリを追加
    anchor = '    queries.append((broad, "top", SCROLLS_BROAD))'
    extra = '''    queries.append((broad, "top", SCROLLS_BROAD))
    # 数値のみ: 返信・RTが立っている投稿（文なし画像バズ含む）
    metric_replies = _with_recency(
        f"lang:ja -filter:retweets -filter:replies min_replies:{max(BROAD_MIN_FAVES // 8, 40)}"
    )
    metric_rts = _with_recency(
        f"lang:ja -filter:retweets -filter:replies min_retweets:{max(BROAD_MIN_FAVES // 5, 60)}"
    )
    queries.append((metric_replies, "live", max(SCROLLS_BROAD, 4)))
    queries.append((metric_rts, "live", max(SCROLLS_BROAD, 4)))'''
    if anchor in data and "metric_replies" not in data:
        data = data.replace(anchor, extra)

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
    force.write_text(ver)
    return _CACHE


_path = _ensure()
_ns = {"__name__": "playwright_collector"}
exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), _ns)
SessionExpiredError = _ns["SessionExpiredError"]
fetch_posts = _ns["fetch_posts"]
globals().update({k: v for k, v in _ns.items() if not k.startswith("_")})
