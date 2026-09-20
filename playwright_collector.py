"""playwright_collector.py - compressed v2 loader"""
import zlib, base64
from pathlib import Path
_DIR = Path(__file__).parent
_b64 = (_DIR / "collector_chunk_0.txt").read_text() + (_DIR / "collector_chunk_1.txt").read_text()
_CODE = zlib.decompress(base64.b64decode(_b64)).decode("utf-8")
_ns = {"__name__": "playwright_collector"}
exec(compile(_CODE, "playwright_collector_impl.py", "exec"), _ns)
SessionExpiredError = _ns["SessionExpiredError"]
fetch_posts = _ns["fetch_posts"]
globals().update({k: v for k, v in _ns.items() if not k.startswith("_")})
