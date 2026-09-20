"""
keyword_filter.py (v4)

keywords.txt  … 投稿収集の参考（検索ORのヒント）。NGではない。
keywords_ng.txt … 除外リストのみ（プレゼント等）。
keywords_combo.txt … AND組み合わせ検索。

採点の関連度は弱い参考（detector の RELEVANCE_FLOOR 既定 0.90）。
キーワードに当たらない投稿を落とす用途ではない。
"""

import re
from pathlib import Path

_BASE_DIR = Path(__file__).parent
KEYWORDS_FILE_PATH = _BASE_DIR / "keywords.txt"
NG_KEYWORDS_FILE_PATH = _BASE_DIR / "keywords_ng.txt"
COMBO_FILE_PATH = _BASE_DIR / "keywords_combo.txt"

DEFAULT_QUERY_GROUP_WORDS = 14
OPERATOR_WARN_THRESHOLD = 20
MIN_CHARS_FOR_QUERY = 2

QUERY_STOPWORDS = {
    "闇", "特定", "吊り", "死ぬ", "死ね", "殺す", "通報", "加害", "偽善",
    "処分なし", "謝罪なし",
}


def _load_words(path: Path) -> list[str]:
    if not path.exists():
        return []
    words = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
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


def _load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line == "#" or line.startswith("# "):
            continue
        lines.append(line)
    return lines


def _words_in_expression(expression: str) -> list[str]:
    cleaned = re.sub(r"[()\"]", " ", expression)
    cleaned = re.sub(r"\bOR\b|\bAND\b", " ", cleaned)
    words = []
    for token in cleaned.split():
        token = token.lstrip("-").strip()
        if not token or ":" in token or len(token) < 2:
            continue
        words.append(token)
    return words


_KEYWORDS = _load_words(KEYWORDS_FILE_PATH)
_NG_KEYWORDS = _load_words(NG_KEYWORDS_FILE_PATH)
_COMBO_EXPRESSIONS = _load_lines(COMBO_FILE_PATH)

_SCORING_WORDS = list(_KEYWORDS)
_seen_scoring = set(_SCORING_WORDS)
for _expression in _COMBO_EXPRESSIONS:
    for _word in _words_in_expression(_expression):
        if _word not in _seen_scoring:
            _seen_scoring.add(_word)
            _SCORING_WORDS.append(_word)

print(
    f"[keyword_filter] 収集参考ワード{len(_KEYWORDS)}個 / "
    f"組み合わせ{len(_COMBO_EXPRESSIONS)}本 / "
    f"NG除外{len(_NG_KEYWORDS)}個"
)


def count_operators(query: str) -> int:
    operators = len(re.findall(r"\bOR\b", query))
    operators += len(re.findall(r"\b\w+:", query))
    operators += len(re.findall(r"(?:^|\s)-", query))
    return operators


def query_groups(max_words: int = DEFAULT_QUERY_GROUP_WORDS) -> list[str]:
    """検索の参考用OR句。ここに無い投稿を落とす用途ではない。"""
    usable = [
        w for w in _KEYWORDS
        if len(w) >= MIN_CHARS_FOR_QUERY and w not in QUERY_STOPWORDS
    ]
    if not usable:
        return []
    groups = []
    for start in range(0, len(usable), max_words):
        chunk = usable[start:start + max_words]
        tokens = [f'"{w}"' if " " in w or "　" in w else w for w in chunk]
        groups.append(" OR ".join(tokens))
    return groups


def combo_queries() -> list[str]:
    return list(_COMBO_EXPRESSIONS)


def find_ng_keyword(text: str) -> str | None:
    """keywords_ng.txt のみ。ここに無い語では落とさない。"""
    if not text:
        return None
    for word in _NG_KEYWORDS:
        if word in text:
            return word
    return None


def is_ng(text: str) -> bool:
    return find_ng_keyword(text) is not None


def matched_keywords(text: str) -> list[str]:
    if not text:
        return []
    return [w for w in _SCORING_WORDS if w in text]


def relevance_score(text: str) -> float:
    """弱い参考スコア 0.0〜1.0。外れても detector 側でほぼ減点しない。"""
    hits = matched_keywords(text)
    if not hits:
        return 0.0
    return min(0.8 + 0.1 * (len(hits) - 1), 1.0)


def primary_keyword(text: str) -> str | None:
    hits = matched_keywords(text)
    return hits[0] if hits else None


def matches_keyword(text: str) -> bool:
    if not _SCORING_WORDS:
        return True
    return bool(matched_keywords(text))


def find_matching_keyword(text: str) -> str | None:
    return primary_keyword(text)


if __name__ == "__main__":
    print(f"--- OR句 ({len(query_groups())}グループ) ---")
    for i, g in enumerate(query_groups(), 1):
        ops = count_operators(f"({g}) lang:ja -filter:retweets min_faves:30")
        print(f"[{i}] 演算子{ops}個 ({g})")
    print(f"\n--- 組み合わせ ({len(combo_queries())}本) ---")
    for i, q in enumerate(combo_queries(), 1):
        print(f"[{i}] {q}")
