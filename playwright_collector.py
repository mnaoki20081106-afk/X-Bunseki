"""playwright_collector.py - full collector with session diagnostics"""
import zlib, base64
from pathlib import Path
_DIR = Path(__file__).parent
_b64 = "".join((_DIR / f"collector_chunk_{i}.txt").read_text() for i in range(3))
_CODE = zlib.decompress(base64.b64decode(_b64)).decode("utf-8")
_ns = {"__name__": "playwright_collector"}
exec(compile(_CODE, "playwright_collector_impl.py", "exec"), _ns)
SessionExpiredError = _ns["SessionExpiredError"]
fetch_posts = _ns["fetch_posts"]
globals().update({k: v for k, v in _ns.items() if not k.startswith("_")})
