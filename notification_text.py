"""
notification_text.py (v4)
LINE・Pushoverの両方で使う通知本文を、1か所で組み立てる。

★なぜ独立させたか:
  v3では line_notifier.py と pushover_notifier.py が別々に本文を作っていて、
  内容がズレていた(LINEにはマッチワードが出るがPushoverには出ない等)。
  さらに致命的だったのが、どちらの本文にも「投稿の中身」が
  一切入っていなかったこと。

  届いた通知に書いてあるのは
      「@handle の投稿が0.5時間でいいね1,234・引用0・ブックマーク0」
  だけ。これでは何の話題なのか分からず、必ずXを開いて確認する手間が要る。
  スクープ動画は「気づいてから最初の数分」が勝負なので、
  この一手間が致命的なロスになる。

  v4では、通知を見た瞬間に撮影を始められる情報を全部入れる:
    - 何が起きたのか(本文の冒頭 + LLMによる要約)
    - どれくらいの勢いなのか(実測の伸び率と加速度)
    - なぜ通知されたのか(スコアの内訳)
    - どう撮るか(掴みの一文・タイトル案)
    - 扱いの注意(リスク判定)
"""


def _risk_mark(risk: str) -> str:
    return {"低": "🟢", "中": "🟡", "高": "🔴"}.get(risk, "⚪")


def build_title(post: dict) -> str:
    """通知のタイトル行(Pushoverのtitle、LINEの1行目)"""
    score = post.get("buzz_score", 0)
    genre = post.get("genre") or "その他"
    mark = "🔥🔥🔥 激アツ" if post.get("is_gekiatsu") else "🔥 急上昇"
    return f"{mark} [{genre}] {score:.0f}点"


def build_body(post: dict, include_url: bool = True) -> str:
    """通知の本文"""
    g = post.get("growth") or {}
    lines = []

    # ── 1. 勢い ──
    likes = post.get("likes", 0)
    age = post.get("elapsed_minutes", 0)
    lpm = g.get("likes_per_min", 0)
    accel = g.get("acceleration")

    speed = f"+{lpm:.0f}/分"
    if not g.get("is_measured"):
        speed += "(推定)"
    if accel is not None:
        speed += f" {'加速' if accel >= 1.0 else '失速'}{accel:.1f}x"

    lines.append(f"⏱ 投稿から{age:.0f}分 / いいね{likes:,} {speed}")
    lines.append(
        f"💬{post.get('replies', 0):,} 🔁{post.get('retweets', 0):,} "
        f"🔖{post.get('bookmarks', 0):,} 👁{post.get('impressions', 0):,}"
    )

    # ── 2. 何が起きたのか ──
    summary = post.get("summary")
    if summary:
        lines.append(f"\n📰 {summary}")

    text = (post.get("text_snippet") or "").strip().replace("\n", " ")
    if text:
        lines.append(f"「{text[:120]}{'…' if len(text) > 120 else ''}」")

    lines.append(f"@{post.get('author_handle', '')}")

    # ── 3. 動画にする材料 ──
    hook = post.get("hook")
    title = post.get("title")
    if hook or title:
        lines.append("")
        if title:
            lines.append(f"🎬 タイトル案: {title}")
        if hook:
            lines.append(f"🎤 掴み: {hook}")

    if post.get("llm_ok"):
        lines.append(
            f"📊 スクープ度{post.get('scoop_score', 0)}/10 "
            f"解説需要{post.get('explain_score', 0)}/10 "
            f"動画適性{post.get('tiktok_fit', 0)}/10 "
            f"{_risk_mark(post.get('risk', ''))}リスク{post.get('risk', '不明')}"
        )

    # ── 4. なぜ通知されたのか(調整の手がかり) ──
    breakdown = post.get("score_breakdown") or {}
    if breakdown:
        top = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)[:3]
        lines.append("🧮 " + " / ".join(f"{k}{v:.0f}" for k, v in top))

    cluster_size = post.get("cluster_size", 1)
    if cluster_size > 1:
        lines.append(f"🗂 同じ話題の投稿が他に{cluster_size - 1}件伸びています")

    if include_url and post.get("url"):
        lines.append(f"\n{post['url']}")

    return "\n".join(lines)


def build_message(post: dict) -> str:
    """タイトル+本文をまとめた1つのテキスト(LINE用)"""
    return f"{build_title(post)}\n{build_body(post)}"


def build_system_message(text: str) -> dict:
    """
    システム通知(セッション切れ・DOM変化アラート等)用の疑似post。
    通常の通知と同じ経路で送れるようにするためのヘルパー。
    """
    return {
        "system_message": text,
        "genre": "システム通知",
        "buzz_score": 0,
        "url": "",
    }
