"""
keyword_filter.py (v4)

v3までの役割: keywords.txt を「収集後の足切りフィルタ」として使う。
v4での役割:  keywords.txt を「X検索クエリそのもの」に変換して、Xのサーバー側で
             絞り込ませる。あわせて、除外ワード(keywords_ng.txt)と
             関連度スコアリングを担当する。

★なぜ変えたか(実データに基づく):
  v3では `lang:ja min_faves:500` という汎用クエリで毎回50件前後を集め、
  そこからキーワードで足切りしていた。実行履歴を49回分集計すると、
  キーワードを通過した投稿は1回あたり1〜2件程度で、39%の実行(49回中19回)は0件だった。
  つまり「50件集めて48件捨てる」という、極めて効率の悪い漏斗になっていた。

  さらに深刻なのは min_faves:500 という条件そのもの。
  「すでに500いいねついた投稿」しか入口を通れないなら、定義上
  “バズる前に見つける”ことは原理的に不可能。早期発見を掲げながら、
  入口が遅行指標になっていた。

  v4ではキーワードをOR条件としてX検索クエリに埋め込む。
  Xのサーバー側で話題を絞ってくれるので、
    - 入口の min_faves を 500 → 50 まで下げられる(＝早期発見が可能になる)
    - 集まる投稿の大半が最初から狙ったジャンルになる(＝無駄が消える)
  という二重の効果が出る。
"""

import re
from pathlib import Path

_BASE_DIR = Path(__file__).parent
KEYWORDS_FILE_PATH = _BASE_DIR / "keywords.txt"
NG_KEYWORDS_FILE_PATH = _BASE_DIR / "keywords_ng.txt"

# 1つのX検索クエリに詰め込むキーワード群の最大文字数。
# Xの検索クエリには長さ制限があるため、長くなりすぎないよう分割する。
DEFAULT_QUERY_GROUP_CHARS = 250

# 検索クエリのOR条件に使うワードの最小文字数。
# 日本語は「地震」「速報」「炎上」のような2文字語が最も情報量が高いので
# 2文字は必ず含める。1文字語だけをクエリから外す。
MIN_CHARS_FOR_QUERY = 2

# 文字数は足りていても、検索語としては一般的すぎて無関係な投稿を
# 大量に呼び込むワード。クエリからは外すが、スコア加点には引き続き使う。
# (例:「特定」は「特定の条件で」「特定できました」など日常語として頻出する)
QUERY_STOPWORDS = {
    "闇", "特定", "吊り", "死ぬ", "死ね", "殺す", "通報", "加害", "偽善",
    "処分なし", "謝罪なし",
}


def _load_words(path: Path) -> list[str]:
    """
    ワードリストファイルを読み込む。
    改行区切り・カンマ区切りの両方に対応し、コメント行(#)・空行は無視する。
    """
    if not path.exists():
        return []

    words = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # コメント行の判定は「# のあと空白」または「# のみの行」に限定する。
        # "#PR" のように # で始まるハッシュタグ自体をワードとして
        # 登録できるようにするため。
        if not line or line == "#" or line.startswith("# "):
            continue
        for part in re.split(r"[,、]", line):
            word = part.strip()
            if word:
                words.append(word)

    seen = set()
    unique = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique


_KEYWORDS = _load_words(KEYWORDS_FILE_PATH)
_NG_KEYWORDS = _load_words(NG_KEYWORDS_FILE_PATH)

print(
    f"[keyword_filter] 対象ワード{len(_KEYWORDS)}個 / "
    f"除外ワード{len(_NG_KEYWORDS)}個を読み込みました"
)


# ──────────────────────────────────────────────
#  1. X検索クエリの組み立て
# ──────────────────────────────────────────────

def query_groups(max_chars: int = DEFAULT_QUERY_GROUP_CHARS) -> list[str]:
    """
    キーワードを "地震 OR 台風 OR 速報" 形式のOR句に分割して返す。
    Xの検索クエリ長制限に収まるよう、max_chars ごとに区切る。

    短すぎるワード(一般語)はノイズ源になるのでクエリには含めない。
    """
    usable = [
        w for w in _KEYWORDS
        if len(w) >= MIN_CHARS_FOR_QUERY and w not in QUERY_STOPWORDS
    ]
    if not usable:
        return []

    groups = []
    current: list[str] = []
    current_len = 0

    for word in usable:
        # 空白を含むワードはフレーズ検索として引用符で囲む
        token = f'"{word}"' if " " in word or "　" in word else word
        added = len(token) + 4  # " OR " の分
        if current and current_len + added > max_chars:
            groups.append(" OR ".join(current))
            current, current_len = [], 0
        current.append(token)
        current_len += added

    if current:
        groups.append(" OR ".join(current))

    return groups


# ──────────────────────────────────────────────
#  2. 除外(NG)判定
# ──────────────────────────────────────────────

def find_ng_keyword(text: str) -> str | None:
    """
    除外ワードに引っかかったら、そのワードを返す。
    広告・懸賞・プレゼント企画など「エンゲージメントは高いが動画ネタにならない」
    投稿をここで落とす。
    """
    if not text:
        return None
    for word in _NG_KEYWORDS:
        if word in text:
            return word
    return None


def is_ng(text: str) -> bool:
    return find_ng_keyword(text) is not None


# ──────────────────────────────────────────────
#  3. 関連度スコアリング
# ──────────────────────────────────────────────

def matched_keywords(text: str) -> list[str]:
    """本文に含まれる対象ワードを全て返す"""
    if not text:
        return []
    return [w for w in _KEYWORDS if w in text]


def relevance_score(text: str) -> float:
    """
    狙っているジャンルへの近さを 0.0〜1.0 で返す。

    v3の「1つでも含めば通過、含まなければ即除外」という0/1判定をやめ、
    連続値にした。理由は2つ:
      - 検索クエリ側で既にジャンルを絞っているので、二重に0/1で切る必要がない
      - 複数ワードに当たる投稿ほど「ど真ん中の話題」である可能性が高く、
        その差を判定に活かせる
    """
    hits = matched_keywords(text)
    if not hits:
        return 0.0
    # 1ワード=0.6、2ワード=0.8、3ワード以上=1.0 と頭打ちにする
    return min(0.6 + 0.2 * (len(hits) - 1), 1.0)


def primary_keyword(text: str) -> str | None:
    """診断・通知本文用に、最初にマッチしたワードを1つ返す"""
    hits = matched_keywords(text)
    return hits[0] if hits else None


# ── v3互換API(既存コードからの呼び出しを壊さないために残す) ──

def matches_keyword(text: str) -> bool:
    if not _KEYWORDS:
        return True
    return bool(matched_keywords(text))


def find_matching_keyword(text: str) -> str | None:
    return primary_keyword(text)


if __name__ == "__main__":
    print(f"--- 生成される検索クエリ用OR句 ({len(query_groups())}グループ) ---")
    for i, g in enumerate(query_groups(), 1):
        print(f"[{i}] ({g})")
