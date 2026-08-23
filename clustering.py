"""
clustering.py
「同じ話題の投稿」をまとめるための、依存ライブラリ不要の簡易クラスタリング。

★なぜ必要か(実データに基づく):
  2026-08-22 17:36 の実行で8件、18:49 の実行で5件が同時に通知されている
  (49回の実行で出た通知17件のうち14件が、この1つの地震だった)。
  中身を見ると全て同じ地震に関する別々の投稿だった。
  通知を受け取る側からすると「1つの出来事」でしかないのに13回鳴る。
  これでは通知の価値が下がり、本当に見るべきものが埋もれる。

  逆に閾値を上げると、今度は丸1日1件も鳴らなくなる(実際、
  8/21〜8/23の49回の実行のうち、通知が出たのは6回だけ)。

  「1話題1通知」にまとめれば、閾値を下げて感度を上げても
  通知が溢れなくなる。感度と静けさは両立できる。

方式:
  本文をノーマライズ(URL・メンション・記号・空白を除去)して
  文字bigramの集合にし、Jaccard係数で類似度を測る。
  日本語は分かち書きが要るが、文字bigramなら形態素解析器なしで
  十分実用的な精度が出る(固有名詞が一致すれば必ず高スコアになる)。
"""

import re
import unicodedata

# 類似度がこの値以上なら「同じ話題」とみなす
DEFAULT_THRESHOLD = 0.35

# 短すぎるテキストは偶然の一致で類似度が跳ね上がるため、
# 一定の長さが無い場合は判定を厳しくする
MIN_SHINGLES_FOR_OVERLAP = 8

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"[@＠]\w+")
_NON_WORD_RE = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]+", re.IGNORECASE)


def normalize(text: str) -> str:
    """比較用に本文を正規化する(全角半角統一・URL/メンション/記号除去)"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _URL_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    text = _NON_WORD_RE.sub("", text)
    return text.lower()


def shingles(text: str, n: int = 2) -> set:
    """正規化済みテキストから文字n-gramの集合を作る"""
    norm = normalize(text)
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def _similarity_from_shingles(a: set, b: set) -> float:
    """
    2つの文字bigram集合の類似度(0.0〜1.0)。

    ★Jaccard係数ではなく Overlap係数(共通部分 ÷ 短い方の集合サイズ)を使う。

    理由: 同じ出来事についての投稿でも、長さは大きく違う。
      「【速報】東京で震度5弱の地震が発生しました」(140字のニュース速報)
      「東京 震度5弱の地震 みなさん大丈夫ですか」(短い反応投稿)
    この2つのJaccard係数は0.24しかなく、無関係な投稿(0.06)との差が
    小さいため、閾値をどこに置いても分離できない。
    Overlap係数なら 0.39 vs 0.11 とはっきり分かれる。

    実測値(同一話題 / 無関係):
      Jaccard : 0.24, 0.48, 0.27  /  0.06, 0.00, 0.06
      Overlap : 0.39, 0.77, 0.48  /  0.11, 0.00, 0.14
    """
    if not a or not b:
        return 0.0

    intersection = len(a & b)
    smaller = min(len(a), len(b))

    if smaller < MIN_SHINGLES_FOR_OVERLAP:
        # 極端に短いテキストはOverlapが当てにならないのでJaccardで判定する
        return intersection / len(a | b)

    return intersection / smaller


def similarity(text_a: str, text_b: str) -> float:
    """2つの本文の類似度(0.0〜1.0)"""
    return _similarity_from_shingles(shingles(text_a), shingles(text_b))


def is_similar_to_any(text: str, others: list[str], threshold: float = DEFAULT_THRESHOLD) -> str | None:
    """
    others のいずれかと類似していれば、その相手のテキストを返す。
    「直近◯時間に通知済みの話題かどうか」の判定に使う。
    """
    for other in others:
        if similarity(text, other) >= threshold:
            return other
    return None


def cluster(posts: list[dict], threshold: float = DEFAULT_THRESHOLD,
            key: str = "text_snippet") -> list[list[dict]]:
    """
    投稿リストを話題ごとにまとめる(貪欲法)。
    入力の並び順が優先度になる(先に来た投稿がクラスタの代表になる)。
    """
    clusters: list[list[dict]] = []
    signatures: list[set] = []

    for post in posts:
        sig = shingles(post.get(key, ""))
        placed = False
        for idx, existing in enumerate(signatures):
            if not sig or not existing:
                continue
            if _similarity_from_shingles(sig, existing) >= threshold:
                clusters[idx].append(post)
                placed = True
                break
        if not placed:
            clusters.append([post])
            signatures.append(sig)

    return clusters


def pick_representatives(posts: list[dict], threshold: float = DEFAULT_THRESHOLD,
                         score_key: str = "buzz_score") -> list[dict]:
    """
    話題ごとに1件だけ代表を選んで返す。
    代表にはクラスタのサイズを cluster_size として付与する
    (同じ話題で何件伸びているか自体が「話題の大きさ」の指標になる)。
    """
    ordered = sorted(posts, key=lambda p: p.get(score_key, 0), reverse=True)
    representatives = []
    for group in cluster(ordered, threshold=threshold):
        rep = dict(group[0])
        rep["cluster_size"] = len(group)
        rep["cluster_urls"] = [p.get("url", "") for p in group[1:4]]
        representatives.append(rep)
    return representatives
