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
COMBO_FILE_PATH = _BASE_DIR / "keywords_combo.txt"

# 1つのX検索クエリに詰め込むキーワードの最大個数。
#
# ★★これは文字数ではなく「演算子の個数」で決まる制限です(2026-08-24に判明)。
#
# X検索には演算子の個数の上限があり、おおよそ22〜23個を超えると
# **超えた分の条件がエラーも出さずに無視されます**。
# OR 自体も演算子として数えられるため、N個のワードをORでつなぐと
#
#     演算子数 = (N - 1)個のOR + lang:ja + -filter:retweets + min_faves:
#              = N + 2
#
# となります。当初は文字数(250字)で分割していたため1グループが32ワードになり、
# 演算子34個で上限を大幅に超過していました。
# つまり**各グループの後半のワードは、検索に一切使われていませんでした。**
# エラーが出ないので、ログを見ても気づけない種類の不具合です。
#
# 14ワード = 演算子16個。上限に対して十分な余裕を持たせています。
DEFAULT_QUERY_GROUP_WORDS = 14

# 演算子がこの数を超えたら警告をログに出す(将来ワードを足したときの保険)
OPERATOR_WARN_THRESHOLD = 20

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


def _load_lines(path: Path) -> list[str]:
    """
    1行を1単位として読み込む(カンマで分割しない)。
    組み合わせ検索の行は "(逮捕 OR 起訴) (俳優 OR タレント)" のように
    括弧やスペースを含む検索式そのものなので、分割してはいけない。
    """
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
    """
    組み合わせ検索の式から、素のワードだけを取り出す。

    "(逮捕 OR 起訴) (俳優 OR タレント)" → ["逮捕", "起訴", "俳優", "タレント"]

    ★何のために必要か:
    組み合わせ検索でヒットした投稿は、keywords.txt のワードを1つも
    含まない場合がある。そのままだと関連度スコアが0点になり、
    「わざわざ狙って取りに行った投稿なのに減点される」という
    ちぐはぐな状態になる。そこで式の中のワードも採点対象に含める。
    """
    cleaned = re.sub(r"[()\"]", " ", expression)
    cleaned = re.sub(r"\bOR\b|\bAND\b", " ", cleaned)
    words = []
    for token in cleaned.split():
        token = token.lstrip("-").strip()
        # 検索演算子(min_faves: など)は除く
        if not token or ":" in token or len(token) < 2:
            continue
        words.append(token)
    return words


_KEYWORDS = _load_words(KEYWORDS_FILE_PATH)
_NG_KEYWORDS = _load_words(NG_KEYWORDS_FILE_PATH)
_COMBO_EXPRESSIONS = _load_lines(COMBO_FILE_PATH)

# 採点に使う語彙 = keywords.txt + 組み合わせ検索の式に出てくるワード
_SCORING_WORDS = list(_KEYWORDS)
_seen_scoring = set(_SCORING_WORDS)
for _expression in _COMBO_EXPRESSIONS:
    for _word in _words_in_expression(_expression):
        if _word not in _seen_scoring:
            _seen_scoring.add(_word)
            _SCORING_WORDS.append(_word)

print(
    f"[keyword_filter] 対象ワード{len(_KEYWORDS)}個 / "
    f"組み合わせ検索{len(_COMBO_EXPRESSIONS)}本 / "
    f"除外ワード{len(_NG_KEYWORDS)}個を読み込みました"
)


# ──────────────────────────────────────────────
#  1. X検索クエリの組み立て
# ──────────────────────────────────────────────

def count_operators(query: str) -> int:
    """
    X検索クエリに含まれる演算子の個数を数える。

    上限(22〜23個)を超えた分は、エラーも出さずに無視されてしまうため、
    クエリを組み立てたら必ずこれで確認する。
    """
    operators = len(re.findall(r"\bOR\b", query))          # OR
    operators += len(re.findall(r"\b\w+:", query))          # lang: min_faves: within_time: など
    operators += len(re.findall(r"(?:^|\s)-", query))       # -filter:retweets などの除外
    return operators


def query_groups(max_words: int = DEFAULT_QUERY_GROUP_WORDS) -> list[str]:
    """
    キーワードを "炎上 OR 大炎上 OR 批判殺到" 形式のOR句に分割して返す。

    X検索の演算子上限を超えないよう、max_words ごとに区切る。
    1文字のワードと、一般的すぎるワードはノイズ源になるので除外する。
    """
    usable = [
        w for w in _KEYWORDS
        if len(w) >= MIN_CHARS_FOR_QUERY and w not in QUERY_STOPWORDS
    ]
    if not usable:
        return []

    groups = []
    for start in range(0, len(usable), max_words):
        chunk = usable[start:start + max_words]
        # 空白を含むワードはフレーズ検索として引用符で囲む
        tokens = [f'"{w}"' if " " in w or "　" in w else w for w in chunk]
        groups.append(" OR ".join(tokens))

    return groups


def combo_queries() -> list[str]:
    """
    keywords_combo.txt の各行を、そのまま検索クエリの本体として返す。
    1行 = 1本の検索になる。
    """
    return list(_COMBO_EXPRESSIONS)


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
    """
    本文に含まれる対象ワードを全て返す。
    keywords.txt のワードに加えて、組み合わせ検索の式に出てくる
    ワードも対象にする(_words_in_expression の説明を参照)。
    """
    if not text:
        return []
    return [w for w in _SCORING_WORDS if w in text]


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
    print(f"\n--- 組み合わせ検索 ({len(combo_queries())}本) ---")
    for i, q in enumerate(combo_queries(), 1):
        ops = count_operators(f"{q} lang:ja -filter:retweets min_faves:20")
        print(f"[{i}] 演算子{ops}個 {q}")
    print(f"\n採点に使う語彙: {len(_SCORING_WORDS)}個")
