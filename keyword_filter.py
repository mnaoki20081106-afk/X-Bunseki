"""
keyword_filter.py
keywords.txt に書かれたワードのうち、投稿本文にいずれか1つでも
含まれていれば通知候補として扱う(ホワイトリスト方式)。

これにより、広告・懸賞キャンペーン・特定ジャンルのファン投稿など、
「いいねやRTは伸びているが、監視したい話題とは関係ない投稿」を
除外できる。

keywords.txt はリポジトリ内のファイルなので、GitHub上で直接編集すれば
次回の実行(最大1時間後)から反映される。専用の管理画面は用意していない。
"""

import re
from pathlib import Path

KEYWORDS_FILE_PATH = Path(__file__).parent / "keywords.txt"


def _load_keywords() -> list[str]:
    """
    keywords.txt を読み込み、ワードのリストを返す。
    改行区切り・カンマ区切りの両方に対応し、コメント行(#)・空行は無視する。
    """
    if not KEYWORDS_FILE_PATH.exists():
        print(f"[警告] {KEYWORDS_FILE_PATH} が見つかりません。フィルタは無効(全件通過)になります。")
        return []

    text = KEYWORDS_FILE_PATH.read_text(encoding="utf-8")

    keywords = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # 1行にカンマ区切りで複数書かれている場合にも対応
        for part in re.split(r"[,、]", line):
            word = part.strip()
            if word:
                keywords.append(word)

    # 重複除去(順序は維持)
    seen = set()
    unique_keywords = []
    for w in keywords:
        if w not in seen:
            seen.add(w)
            unique_keywords.append(w)

    return unique_keywords


# モジュール読み込み時に1回だけ読み込む(1回の実行=1プロセスなので、
# 実行中にkeywords.txtが変わることは無い前提)
_KEYWORDS = _load_keywords()
print(f"[keyword_filter] {len(_KEYWORDS)}個のキーワードを読み込みました")


def matches_keyword(text: str) -> bool:
    """
    投稿本文(text)に、keywords.txt のワードが1つでも含まれていれば True。
    キーワードが1つも登録されていない場合(空リスト)は、
    フィルタ自体が無効という扱いで常に True を返す(安全側に倒す)。
    """
    if not _KEYWORDS:
        return True
    if not text:
        return False
    return any(keyword in text for keyword in _KEYWORDS)
