"""確定性（不靠模型）的稿件規則：切句、正文結構、觀點段、時間詞、易變數據、hashtag 內容、數字 token、歸屬語氣。
gates（寫稿後、review 前）與 review.evaluate（Codex 結果判定）共用；全部 fail-closed。"""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from . import semantic
from .common import fold_width
from .write import COMMENT_MARK


# ---------- 正規化 ----------
def norm_text(s: str) -> str:
    """詞表比對用：NFKC、小寫、移除所有空白（「明 天」-> 「明天」、全形 -> 半形）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").lower())


def core(s: str) -> str:
    """句子比對核心：只轉全形 ASCII（不做 NFKC，保留 ² 等上標），去空白與標點，
    但保留數字語義字元：小數點（僅夾在數字間）、% 、+、-。"""
    t = fold_width(s).replace("−", "-")
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", "", t)
    return re.sub(r"[^\w.%+\-]|_", "", t)


# ---------- 切句與正文結構 ----------
_SPLIT = re.compile(r"(?:[。！？!?；;…\n\r]|(?<!\d)\.|\.(?!\d))+")


def split_sentences(text: str) -> list[str]:
    """以「。！？；…」（含半形 !?;）、換行，以及『不夾在數字間』的英文句點為界切句（0.5 倍不會被切開）。
    沒有文字內容的殘片（如單獨的 」）丟掉。"""
    out = []
    for part in _SPLIT.split(text or ""):
        if core(part):
            out.append(part.strip())
    return out


def paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n", body or "") if p.strip()]


def split_body(body: str) -> tuple[list[str], str | None]:
    """回 (事實段落, 觀點段或 None)。只有『最後一段且以【小編觀點】開頭』才算觀點段；其他任何段落都是事實段。"""
    paras = paragraphs(body)
    if paras and paras[-1].startswith(COMMENT_MARK):
        return paras[:-1], paras[-1]
    return paras, None


def factual_sentences(draft: dict) -> list[str]:
    """標題 + 正文中『結尾觀點段』以外的所有句子，這些都必須有 claim 與 evidence。
    以【小編觀點】開頭但不是最後一段的段落，照樣當事實句查核（不能靠標記豁免）。"""
    sents = split_sentences(draft.get("title", ""))
    facts, _op = split_body(draft.get("body", ""))
    for p in facts:
        sents += split_sentences(p)
    return sents


def structure_problem(draft: dict, min_fact: int) -> str | None:
    """正文結構：先 >= min_fact 句事實句，最後才是唯一一段【小編觀點】。標題、hashtag 不可有標記。"""
    body = draft.get("body", "") or ""
    if body.count(COMMENT_MARK) != 1:
        return f"「{COMMENT_MARK}」須剛好出現 1 次，實際 {body.count(COMMENT_MARK)} 次"
    if COMMENT_MARK in (draft.get("title", "") or "") or any(COMMENT_MARK in str(h) for h in draft.get("hashtags") or []):
        return f"「{COMMENT_MARK}」只能出現在 body"
    facts, op = split_body(body)
    if op is None:
        return f"「{COMMENT_MARK}」必須是最後一段的開頭"
    n = sum(len(split_sentences(p)) for p in facts)
    if n < min_fact:
        return f"觀點段之前只有 {n} 句事實句，至少要 {min_fact} 句"
    if not core(op[len(COMMENT_MARK):]):
        return "觀點段沒有內容"
    return None


# ---------- 觀點段 ----------
_CN_BIG_NUM = re.compile(r"[零〇一二兩三四五六七八九十][百千萬億兆]")
_CN_NUM_UNIT = re.compile(r"[零〇一二兩三四五六七八九十]+[成倍元歲名局場屆年月日號]")
_QUOTED = re.compile(r"[「『《〈“\"]([^」』》〉”\"]{2,20})[」』》〉”\"]")


def quoted_terms(texts: list[str]) -> list[str]:
    out = []
    for t in texts:
        out += _QUOTED.findall(t or "")
    return out


def keyword_entities(keyword: str) -> list[str]:
    """話題關鍵詞整串 + 以空白/標點切開的每一段（>= 2 個有意義字元）。"""
    kw = unicodedata.normalize("NFKC", keyword or "")
    parts = [kw] + re.split(r"[\s\W_]+", kw)
    return [p for p in dict.fromkeys(norm_text(x) for x in parts) if len(core(p)) >= 2]


def _cjk_grams(s: str, n: int = 4) -> set[str]:
    out = set()
    for run in re.findall(r"[㐀-䶿一-鿿]+", s or ""):
        out |= {run[i:i + n] for i in range(len(run) - n + 1)}
    return out


# 觀點段常見的慣用感想語：與事實句偶然重疊不算「重提人事物」
_STOCK_PHRASES = ("值得掌聲", "值得關注", "值得期待", "值得鼓勵", "值得肯定", "令人期待", "讓人期待", "大家加油", "一起加油",
                  "希望一切", "一切順利", "繼續加油", "加油打氣")
# 3 字詞若以這些字結尾，多半是機關/隊名/地名（大聯盟、籃網隊、文化局…），重疊就算重提專有名詞
_ORG_SUFFIX = "盟隊局部會院校市縣區鄉鎮村路街園館所司團協盃杯賽山湖島港橋站廠社署處"


def _mask_stock(s: str) -> str:
    for w in _STOCK_PHRASES:
        s = s.replace(w, "□")
    return s


def opinion_problem(opinion: str, entities: list[str], banned_words: list[str], fact_text: str = "") -> str | None:
    """觀點段只能是感想：不可有數字（阿拉伯、全形、上標、圈號、中文大數/數量）、英文字、職稱/指控詞、
    專有名詞（entities：話題關鍵詞、Codex named_entities、「」內詞），也不可與標題/事實句共用任何連續 3 個中文字
    （程式自行抽出事實句中的專有名詞與事件描述，不依賴 Codex 的 named_entities）。命中就拒絕。"""
    if opinion is None:
        return "沒有觀點段"
    o_txt = _mask_stock(norm_text(opinion.replace(COMMENT_MARK, "", 1)))
    f_txt = _mask_stock(norm_text(fact_text))
    # 2026-11-09 放寬：只比 6 字以上連續重疊（4 字會誤擋「己真心喜」這類常見字串）；3 字重疊只在『像專有名詞』時才算
    shared = sorted(_cjk_grams(o_txt, 6) & _cjk_grams(f_txt, 6))
    if shared:
        return f"觀點段與事實句共用「{shared[0]}」（不可在觀點段重提人事物）"
    title_line = _mask_stock(norm_text((fact_text or "").split(chr(10), 1)[0]))
    g3 = _cjk_grams(o_txt, 3) & _cjk_grams(f_txt, 3)
    pn = sorted(g for g in g3 if g in _cjk_grams(title_line, 3) or g[-1] in _ORG_SUFFIX)
    if pn:
        return f"觀點段與事實句共用專有名詞「{pn[0]}」（不可在觀點段重提人事物）"
    o = norm_text(opinion.replace(COMMENT_MARK, "", 1))
    if re.search(r"\d", o) or any(unicodedata.category(ch) == "No" for ch in opinion):
        return "觀點段含數字"
    if _CN_BIG_NUM.search(o) or _CN_NUM_UNIT.search(o):
        return "觀點段含中文數字"
    if re.search(r"[a-z]", o):
        return "觀點段含英文字（可能是專有名詞）"
    for w in banned_words:
        if norm_text(w) and norm_text(w) in o:
            return f"觀點段含禁用詞「{w}」"
    for e in entities:
        ne = norm_text(e)
        if len(core(ne)) >= 2 and ne in o:
            return f"觀點段含專有名詞「{e}」"
    return None


# ---------- 時間詞與易變數據 ----------
# 含有禁用時間詞字面、但實際不是相對時間的複合詞（動詞「現」+「在」）
_TIME_FALSE_POS = ("出現在", "表現在", "體現在", "實現在", "發現在", "呈現在", "顯現在", "展現在", "浮現在", "重現在",
                   "湧現在", "再現在", "兌現在", "呈現在")


def time_word_hit(texts: list[str], words: list[str]) -> str | None:
    blob = norm_text("".join(texts))
    for fp in _TIME_FALSE_POS:  # 詞界：「出現在」不是「現在」
        blob = blob.replace(fp, "□")
    for w in words:
        if norm_text(w) and norm_text(w) in blob:
            return w
    return None


def volatile_hit(texts: list[str], patterns: list[str]) -> str | None:
    blob = norm_text("".join(texts))
    for p in patterns:
        m = re.search(p, blob)
        if m:
            return m.group(0)
    return None


# ---------- hashtag 內容 ----------
def hashtag_content_problem(tags: list, allowed_texts: list[str] | None, whitelist: list[str]) -> str | None:
    """每個 hashtag 必須在白名單，或（去掉 #）是話題關鍵詞/合格來源標題的子字串。allowed_texts=None 時只認白名單。"""
    wl = {norm_text(w) for w in whitelist}
    hay = [norm_text(t) for t in (allowed_texts or []) if t]
    for t in tags:
        nt = norm_text(t)
        if nt in wl:
            continue
        body = nt.lstrip("#")
        if not body or not any(body in h for h in hay):
            return f"hashtag「{t}」不是話題關鍵詞、來源標題中的詞，也不在白名單"
    return None


# ---------- 數字 token ----------
_NUM = re.compile(r"(?:(?<![0-9A-Za-z])[+\-])?\d+(?:\.\d+)?[⁰¹²³⁴⁵⁶⁷⁸⁹]*%?")
_YEAR = re.compile(r"(\d{4})年")
_MONTH_DAY = re.compile(r"(\d{1,2})月(\d{1,2})日")
_REL_YEAR = {"前年": -2, "去年": -1, "今年": 0, "明年": 1, "後年": 2}
_REL_DAY = {"前天": -2, "昨天": -1, "昨日": -1, "今天": 0, "今日": 0, "明天": 1, "明日": 1}


def _num_prep(s: str) -> str:
    t = fold_width(s).replace("−", "-")
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)  # 千分位
    t = re.sub(r"(?<=\d)\s+(?=[\d+\-])", "|", t)  # 「3 5」不可黏成 35
    return re.sub(r"\s+", "", t)


def number_tokens(s: str) -> list[str]:
    """數字 token：保留正負號、小數點、上標、百分比。10² ≠ 102、3.5 ≠ 35、+5 ≠ -5。"""
    return _NUM.findall(_num_prep(s))


def number_problem(sentence: str, evidence: str, published=None) -> str | None:
    """句子的每個數字 token 都必須在 evidence 的 token 中（完整相等）。
    例外只限完整日期片段：「YYYY年」等於該來源發佈年份、「M月D日」中 D 在 evidence 且 M 等於發佈月份，
    或 M/D 恰為該來源發佈日。其他（如「6勝3敗」）一律要 evidence 有。"""
    s = _num_prep(sentence)
    ev = {semantic.canon_token(t) for t in number_tokens(evidence)} | semantic.evidence_number_tokens(evidence)
    # 2026-11-09：來源的相對時間依發佈日換算（稿件被時效關禁用相對時間詞，只能寫絕對日期）
    rel_days = {(published + timedelta(days=d)).date() for w, d in _REL_DAY.items() if w in evidence} if published else set()
    rel_years = {published.year + d for w, d in _REL_YEAR.items() if w in evidence} if published else set()
    for m in list(_MONTH_DAY.finditer(s)):
        mo, d = m.group(1), m.group(2)
        ok = (mo in ev and d in ev)
        if not ok and published is not None:
            ok = ((d in ev and int(mo) == published.month) or (int(mo) == published.month and int(d) == published.day)
                  or any(x.month == int(mo) and x.day == int(d) for x in rel_days))
        if not ok:
            return f"日期「{m.group(0)}」在 evidence 與來源發佈日都找不到依據"
    s = _MONTH_DAY.sub("〔日期〕", s)
    for m in list(_YEAR.finditer(s)):
        y = m.group(1)
        if y not in ev and not (published is not None and (int(y) == published.year or int(y) in rel_years)):
            return f"年份「{m.group(0)}」在 evidence 找不到"
    s = _YEAR.sub("〔年〕", s)
    bad = semantic.unit_problem(sentence, evidence)  # 數字綁單位：5萬元 != 5億元；三千元 == 3000元
    if bad:
        return bad
    s = semantic.strip_multiplied(s)  # 阿拉伯數字+倍數已由 unit_problem 以總值比對
    for tok in _NUM.findall(s):
        if semantic.canon_token(tok) not in ev:  # 10 == 10.0
            return f"數字「{tok}」不在 evidence 中"
    return None


# ---------- 歸屬語氣 ----------
PARA = "\x01"  # 來源段落分隔符：必須不是空白字元（U+2029 會被 \s 移除，曾導致跨段 evidence 漏擋）
_SRC_SENT_END = "。！？!?；;" + PARA


def attribution_problem(sentence: str, scope: str, source_markers: list[str], sentence_markers: list[str]) -> str | None:
    """scope = 來源中『該句句首到 evidence 結尾』。scope 有歸屬/不確定語氣，稿句卻沒有任何歸屬語氣 -> 拒絕。"""
    scope = re.sub(r"(?<!不)排除[^，,。；;]{0,10}?可能", "", scope)  # 「排除…可能」是否定可能性，不是不確定語氣
    hit = next((m for m in source_markers if m in scope), None)
    if hit and not any(m in sentence for m in sentence_markers):
        return f"來源有「{hit}」，稿件卻寫成確定句"
    return None
