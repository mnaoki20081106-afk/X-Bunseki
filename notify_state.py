"""
notify_state.py
「いつ・何を通知したか」だけを、リポジトリ内のJSONファイルで管理する。

★なぜDB(SQLite)と分けるのか:

  観測データ(observations)はGitHub Actionsのキャッシュに置いている。
  量が多く、失っても数回の実行で回復するからだ。

  しかし「通知済みかどうか」はまったく性質が違う。
  ここを失うと、**同じ投稿を何度でも通知してしまう**。
  キャッシュはLRUで消えることがあり、消えた瞬間に
  「昨日通知した投稿がまた鳴る」という最悪の挙動になる。

  そこでこのファイルだけはリポジトリにコミットして永続化する。
  1日数件しか書き込まれないので、リポジトリが膨らむ心配もない。

★副次的な利点:
  GitHubでこのファイルを開けば、「いつ・何を・何点で通知したか」が
  そのまま履歴として読める。通知が多い/少ないの判断材料になる。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(__file__).parent / "data" / "notify_state.json"

# 保持する通知履歴の件数(これを超えたら古いものから捨てる)
MAX_HISTORY = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class NotifyState:
    """通知履歴。読み込み → 判定 → 記録 → 保存 の順で使う"""

    def __init__(self, notifications: list[dict] | None = None):
        self.notifications = notifications or []

    # ── 読み書き ────────────────────────────────

    @classmethod
    def load(cls) -> "NotifyState":
        if not STATE_PATH.exists():
            return cls()
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return cls(data.get("notifications", []))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[notify_state] 読み込みに失敗しました(空として続行): {e}")
            return cls()

    def save(self):
        # 新しい順に並べ、古すぎるものは捨てる
        self.notifications.sort(key=lambda n: n.get("at", ""), reverse=True)
        self.notifications = self.notifications[:MAX_HISTORY]

        payload = {
            "updated_at": _now().isoformat(),
            "notified_last_24h": self.count_last_24h(),
            "notifications": self.notifications,
        }
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            print(f"[notify_state] 保存に失敗しました: {e}")

    # ── 判定 ────────────────────────────────────

    def is_notified(self, post_id: str) -> bool:
        """この投稿は既に通知済みか"""
        return any(n.get("post_id") == post_id for n in self.notifications)

    def last_notified_at(self) -> datetime | None:
        """直近の通知時刻(通知が1件も無ければNone)"""
        times = [_parse(n.get("at")) for n in self.notifications]
        times = [t for t in times if t]
        return max(times) if times else None

    def minutes_since_last(self) -> float | None:
        """最後に通知してから何分経ったか(未通知ならNone)"""
        last = self.last_notified_at()
        return None if last is None else (_now() - last).total_seconds() / 60.0

    def count_last_24h(self) -> int:
        cutoff = _now() - timedelta(hours=24)
        return sum(1 for n in self.notifications
                   if (_parse(n.get("at")) or cutoff) >= cutoff)

    def last_notified_at_for_author(self, author: str) -> datetime | None:
        times = [_parse(n.get("at")) for n in self.notifications
                 if n.get("author") == author]
        times = [t for t in times if t]
        return max(times) if times else None

    def recent_texts(self, hours: float) -> list[str]:
        """直近hours時間に通知した投稿の本文(同じ話題の再通知を防ぐため)"""
        cutoff = _now() - timedelta(hours=hours)
        return [
            n.get("text", "") for n in self.notifications
            if (_parse(n.get("at")) or cutoff - timedelta(days=1)) >= cutoff
        ]

    # ── 記録 ────────────────────────────────────

    def record(self, post: dict):
        self.notifications.append({
            "at": _now().isoformat(),
            "post_id": post.get("post_id"),
            "author": post.get("author_handle"),
            "score": post.get("buzz_score"),
            "genre": post.get("genre"),
            "text": (post.get("text_snippet") or "")[:150],
            "url": post.get("url"),
        })


if __name__ == "__main__":
    state = NotifyState.load()
    print(f"通知履歴: {len(state.notifications)}件")
    print(f"直近24時間: {state.count_last_24h()}件")
    since = state.minutes_since_last()
    print(f"最後の通知から: {'まだ通知なし' if since is None else f'{since:.0f}分'}")
    for n in state.notifications[:5]:
        print(f"  {n.get('at', '')[:16]} {n.get('score')}点 @{n.get('author')} {n.get('text', '')[:40]}")
