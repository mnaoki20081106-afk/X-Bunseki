"""
genre_classifier.py
Groq API (無料枠) を使って投稿本文からジャンルを分類する。

事前準備:
  pip install groq
  環境変数 GROQ_API_KEY を設定 (https://console.groq.com で無料取得)
"""

import json
import os

from groq import Groq

GENRES = ["スポーツ", "芸能", "政治", "経済", "エンタメ", "事件・事故", "テクノロジー", "その他"]

MODEL = "llama-3.3-70b-versatile"  # Groq無料枠で使える汎用モデル。精度/速度で他モデルに変更可

SYSTEM_PROMPT = f"""あなたはSNS投稿のジャンル分類器です。
与えられた投稿本文を読み、以下のジャンルのうち最も当てはまるものを1つだけ選んでください。
ジャンル一覧: {', '.join(GENRES)}

出力は必ず以下のJSON形式のみで返してください。説明文は不要です。
{{"genre": "選んだジャンル", "reason": "10文字程度の簡潔な理由"}}
"""


def _client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("環境変数 GROQ_API_KEY が設定されていません")
    return Groq(api_key=api_key)


def classify(text: str) -> dict:
    """
    投稿本文を分類する。
    戻り値: {"genre": "スポーツ", "reason": "..."}
    失敗時は genre="その他" を返す(パイプラインを止めないため)。
    """
    if not text or not text.strip():
        return {"genre": "その他", "reason": "本文なし"}

    try:
        client = _client()
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text[:500]},  # トークン節約のため先頭500字
            ],
            temperature=0.0,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        raw = completion.choices[0].message.content
        result = json.loads(raw)
        if result.get("genre") not in GENRES:
            result["genre"] = "その他"
        return result
    except Exception as e:  # noqa: BLE001
        return {"genre": "その他", "reason": f"分類失敗: {e}"}


def classify_batch(texts: list[str]) -> list[dict]:
    """複数投稿をまとめて分類(現状は単純に逐次呼び出し)"""
    return [classify(t) for t in texts]
