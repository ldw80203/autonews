"""原創關：與每個來源的最長連續重疊字數 + 評論段落（只能一段、必須在結尾）。"""
from __future__ import annotations

import difflib
import re
import unicodedata

from .write import COMMENT_MARK


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\W_]+", "", s)


def longest_overlap(a: str, b: str) -> int:
    """a、b 去除空白標點後的最長連續相同字串長度。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size


def max_overlap(draft: dict, source_texts: list[str]) -> int:
    text = (draft.get("title", "") + "\n" + draft.get("body", ""))
    return max((longest_overlap(text, s) for s in source_texts), default=0)


def commentary_problem(draft: dict, min_chars: int = 30) -> str | None:
    """評論段落規則：整份稿（title/body/hashtags）只出現一次標記，且是 body 最後一段開頭。回問題描述或 None。"""
    body = draft.get("body", "") or ""
    total = body.count(COMMENT_MARK)
    if total != 1:
        return f"「{COMMENT_MARK}」須剛好出現 1 次，實際 {total} 次"
    if COMMENT_MARK in (draft.get("title", "") or "") or any(COMMENT_MARK in h for h in draft.get("hashtags", [])):
        return f"「{COMMENT_MARK}」只能出現在 body"
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", body) if p.strip()]
    if not paras or not paras[-1].startswith(COMMENT_MARK):
        return f"「{COMMENT_MARK}」必須是最後一段的開頭"
    if len(_norm(paras[-1])) - len(_norm(COMMENT_MARK)) < min_chars:
        return f"評論段不足 {min_chars} 字"
    return None


def check(draft: dict, source_texts: list[str], max_chars: int, min_comment: int = 30) -> tuple[bool, str]:
    ov = max_overlap(draft, source_texts)
    if ov > max_chars:
        return False, f"原創關：與來源最長連續重疊 {ov} 字 > 門檻 {max_chars}"
    bad = commentary_problem(draft, min_comment)
    if bad:
        return False, f"原創關：{bad}"
    return True, f"最長重疊 {ov} 字"
