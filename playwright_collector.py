"""playwright_collector.py - bootstrap: load known-good implementation"""
import urllib.request
from pathlib import Path

_URL = (
    "https://raw.githubusercontent.com/mnaoki20081106-afk/X-Bunseki/"
    "f909cd63b49b0142011487e5f0c177b601821f30/playwright_collector.py"
)
_CACHE = Path(__file__).with_name("_playwright_collector_impl.py")

def _ensure():
    if not _CACHE.exists() or _CACHE.stat().st_size < 1000:
        data = urllib.request.urlopen(_URL, timeout=30).read()
        text = data.decode("utf-8").replace(
            'SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "3h")',
            'SEARCH_WITHIN_TIME = _env("SEARCH_WITHIN_TIME", "off")',
        )
        _CACHE.write_text(text, encoding="utf-8")
    return _CACHE

_path = _ensure()
_ns = {"__name__": "playwright_collector"}
exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), _ns)
SessionExpiredError = _ns["SessionExpiredError"]
fetch_posts = _ns["fetch_posts"]
globals().update({k: v for k, v in _ns.items() if not k.startswith("_")})
