"""
content_scorer.py (v4)
通知候補をLLM(Groq無料枠)にかけ、「TikTok動画のネタとして使えるか」を
判定させる。

★なぜ genre_classifier.py を置き換えたのか:

  v3では通知が確定したあとにジャンル(スポーツ/芸能/政治…)を分類していた。
  しかしジャンル名は、動画を作るうえで何の判断材料にもなっていなかった。
  「[芸能]」と表示されても、それをスクープ動画にできるかどうかは分からない。

  このプロジェクトのゴールは「バズ投稿の検知」ではなく
  「需要のあるTikTok動画を作ってフォロワーを獲得すること」。
  だとすれば、LLMに聞くべきなのはジャンル名ではなく、

      - これは動画のネタとして成立するのか
      - スクープ型なのか、解説型なのか
      - どんな掴み(フック)で始めればいいのか
      - 出したら危ないネタではないか

  であるべき。v4ではそれを聞く。
  通知本文にタイトル案まで載るので、通知を見た瞬間に撮り始められる。

  なお呼び出すのは「クラスタリング後の上位数件」だけなので、
  Groqの無料枠は十分に足りる(1実行あたり2〜3回程度)。
"""

import json
import os

GENRES = ["スポーツ", "芸能", "政治", "経済", "エンタメ", "事件・事故", "テクノロジー", "その他"]

# Groqで使うモデル。無料枠で使えるモデルは入れ替わるので環境変数で差し替えられるようにする。
MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = """あなたは、Xで話題になっている投稿を素材にしてTikTokのショート動画
(スクープ速報・解説動画)を作る、日本のショート動画クリエイターの編集アシスタントです。

与えられたX投稿の本文を読み、その投稿が「TikTokのショート動画のネタとして
どれだけ価値があるか」を判定してください。

判定の観点:
- scoop_score(0〜10): 速報性・意外性。「まだ多くの人が知らない」ほど高い。
  すでに全メディアが報じている一般ニュースは低くする。
- explain_score(0〜10): 解説需要。「経緯が複雑」「賛否が割れている」
  「背景を知りたい人が多い」ほど高い。
- tiktok_fit(0〜10): 総合的な動画化適性。素材(画像・映像)が無くても
  テロップと語りで15〜60秒に成立するなら高い。
  身内ネタ・文脈が必要すぎるもの・数字だけの投稿は低い。
- risk: "低" / "中" / "高"。死亡事故・自殺・重大災害・未成年被害・
  医療デマなど、TikTokの規約や視聴者感情の面で扱いに注意が要るものは
  "中" 以上にする。センシティブなだけで即NGにはしない。
- genre: 次のいずれか1つ … スポーツ, 芸能, 政治, 経済, エンタメ, 事件・事故, テクノロジー, その他
- hook: 動画の最初の3秒で読み上げる掴みの一文(20〜35文字、日本語)
- title: TikTokの投稿タイトル案(25文字以内、日本語)
- summary: 何が起きたのかの要約(50文字以内、日本語)

出力は必ず次のJSONのみ。説明文は不要です。
{"genre":"...","scoop_score":0,"explain_score":0,"tiktok_fit":0,"risk":"低","hook":"...","title":"...","summary":"..."}
"""

FALLBACK = {
    "genre": "その他",
    "scoop_score": 0,
    "explain_score": 0,
    "tiktok_fit": 0,
    "risk": "不明",
    "hook": "",
    "title": "",
    "summary": "",
    "llm_ok": False,
}


def _clamp_int(value, low=0, high=10) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def score_post(text: str) -> dict:
    """
    投稿本文を評価する。失敗してもパイプラインは止めず、FALLBACKを返す。
    (通知が飛ばなくなる方が、ジャンル名が無いことより遥かに損失が大きい)
    """
    if not text or not text.strip():
        return dict(FALLBACK)

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("[content_scorer] GROQ_API_KEY が未設定のため、LLM評価をスキップします")
        return dict(FALLBACK)

    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text[:600]},
            ],
            temperature=0.2,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        raw = json.loads(completion.choices[0].message.content)
    except Exception as e:  # noqa: BLE001
        print(f"[content_scorer] LLM評価に失敗しました(処理は続行します): {e}")
        return dict(FALLBACK)

    genre = raw.get("genre")
    result = {
        "genre": genre if genre in GENRES else "その他",
        "scoop_score": _clamp_int(raw.get("scoop_score")),
        "explain_score": _clamp_int(raw.get("explain_score")),
        "tiktok_fit": _clamp_int(raw.get("tiktok_fit")),
        "risk": raw.get("risk") if raw.get("risk") in ("低", "中", "高") else "不明",
        "hook": str(raw.get("hook") or "")[:60],
        "title": str(raw.get("title") or "")[:40],
        "summary": str(raw.get("summary") or "")[:80],
        "llm_ok": True,
    }
    return result


def enrich(posts: list[dict], limit: int = 3) -> list[dict]:
    """
    上位limit件だけLLMにかけて、結果を各postにマージして返す。
    limitを絞るのは、Groqの無料枠(レート制限)を確実に守るため。
    """
    enriched = []
    for i, post in enumerate(posts):
        p = dict(post)
        if i < limit:
            p.update(score_post(p.get("text_snippet", "")))
        else:
            p.update(FALLBACK)
        enriched.append(p)
    return enriched
