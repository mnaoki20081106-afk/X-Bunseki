"""playwright_collector.py - loads compressed implementation from chunks"""
import zlib, base64
from pathlib import Path

_DIR = Path(__file__).parent
_parts = []
for i in range(3):
    p = _DIR / f"collector_chunk_{i}.txt"
    _parts.append(p.read_text())
_CODE = zlib.decompress(base64.b64decode("".join(_parts))).decode("utf-8")
_ns = {"__name__": "playwright_collector"}
exec(compile(_CODE, "playwright_collector_impl.py", "exec"), _ns)
SessionExpiredError = _ns["SessionExpiredError"]
fetch_posts = _ns["fetch_posts"]
globals().update({k: v for k, v in _ns.items() if not k.startswith("_")})
