"""第六輪：確定性語意檢查（稿句 vs 其 evidence 引文，不呼叫模型）。
1. 否定詞一致（A1）  2. 數字綁單位 + 中文/阿拉伯數字正規化（A2、B1）  3. 人名/專有名詞字元換位（A3）
全部只比『該句』與『該句 claim 的 evidence』。數字歸屬互換（A組5、B組10 -> A組10、B組5）刻意不處理，
是已接受的殘留風險，靠 Codex + Claude 雙審降低。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .common import fold_width

# ---------- 中文數字 ----------
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_SMALL = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"萬": 10 ** 4, "億": 10 ** 8, "兆": 10 ** 12}
CN_NUM_CHARS = "零〇一二兩三四五六七八九十百千萬億兆"
MULT = {"千": 1000, "百": 100, "萬": 10 ** 4, "億": 10 ** 8, "兆": 10 ** 12}


def cn_to_int(s: str) -> int | None:
    """中文數字 -> 整數；無法解析回 None。『二〇二六』這類逐位寫法直接串接。"""
    if not s:
        return None
    if all(c in _CN_DIGIT for c in s):
        return int("".join(str(_CN_DIGIT[c]) for c in s))
    total = section = num = 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_SMALL:
            section += (num or 1) * _CN_SMALL[ch]
            num = 0
        elif ch in _CN_BIG:
            section += num
            total += (section or 1) * _CN_BIG[ch]
            section = num = 0
        else:
            return None
    return total + section + num


# ---------- 數字 + 單位 ----------
UNITS = ["小時", "公里", "公斤", "公尺", "公分", "美元", "元", "%", "人", "名", "位", "歲", "年", "月", "日", "天", "場", "次",
         "件", "戶", "家", "輛", "台", "張", "筆", "局", "分", "秒", "噸"]
_UNIT_RE = "|".join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))
_PAIR = re.compile(
    rf"(?P<arab>\d+(?:\.\d+)?)(?P<mult>[千百萬億兆]*)(?P<aunit>{_UNIT_RE})?"
    rf"|(?P<cn>[{CN_NUM_CHARS}]+)(?P<cunit>{_UNIT_RE})?")
_UNIT_CLASS = {"人": "人", "名": "人", "位": "人"}
_CN_SKIP_PREV = "數幾"  # 數十人、幾百人：概數，不比對


def _prep(s: str) -> str:
    t = fold_width(s or "").replace("−", "-")
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)
    return re.sub(rf"(?<=\d)\s+(?=[千百萬億兆]|{_UNIT_RE})", "", t)  # 「6 萬元」-> 「6萬元」


def _cls(u: str) -> str:
    return _UNIT_CLASS.get(u, u)


def _compat(u1: str, u2: str) -> bool:
    return not u1 or not u2 or _cls(u1) == _cls(u2)


def _dec(x) -> Decimal | None:
    try:
        return Decimal(x)
    except (InvalidOperation, ValueError):
        return None


def number_pairs(text: str, for_sentence: bool) -> list[tuple[Decimal, str, str]]:
    """回 [(數值, 單位, 原文)]。for_sentence=True 只回『需要被 evidence 支持』的項目；
    False 回全部（含無單位的裸數字），供 evidence 當比對池。"""
    s = _prep(text)
    out = []
    for m in _PAIR.finditer(s):
        if for_sentence and m.start() > 0 and s[m.start() - 1] == "第":
            continue  # 序數（第三局、第6次）不是數量
        if m.group("arab") is not None:
            base = _dec(m.group("arab"))
            if base is None:
                continue
            mult = 1
            for ch in m.group("mult") or "":
                mult *= MULT[ch]
            unit = m.group("aunit") or ""
            val = base * mult
            if for_sentence and ((not unit and mult == 1) or unit == "%"):
                continue  # 裸數字與百分比由 rules.number_problem 的 token 檢查負責
        else:
            raw = m.group("cn")
            v = cn_to_int(raw)
            if v is None:
                continue
            unit = m.group("cunit") or ""
            val = Decimal(v)
            if for_sentence:
                prev = s[m.start() - 1] if m.start() > 0 else ""
                if (prev.isdigit() or not unit or prev in _CN_SKIP_PREV or raw == "一" or unit in {"月", "日", "年"}
                        or (raw == "十" and unit == "分")):
                    continue  # 一名網友/十分/數十人/日期：不比對
        out.append((val, unit, m.group(0)))
    return out


def evidence_number_tokens(evidence: str) -> set[str]:
    """evidence 中的中文數字換成阿拉伯字串，供 rules.number_problem 的 token 比對（三千元 -> 3000；五萬 -> 5、50000）。"""
    out = set()
    s = _prep(evidence)
    for m in re.finditer(rf"[{CN_NUM_CHARS}]+", s):
        raw = m.group(0)
        v = cn_to_int(raw)
        if v is None:
            continue
        out.add(str(v))
        if raw[-1] in "萬億兆" and len(raw) > 1:
            head = cn_to_int(raw[:-1])
            if head is not None:
                out.add(str(head))
    for m in _PAIR.finditer(s):  # 阿拉伯 + 倍數：5萬 -> 50000
        if m.group("arab") is not None and m.group("mult"):
            base, mult = _dec(m.group("arab")), 1
            for ch in m.group("mult"):
                mult *= MULT[ch]
            if base is not None:
                tot = base * mult
                out.add(str(int(tot)) if tot == tot.to_integral() else format(tot.normalize(), "f"))
    return out


def unit_problem(sentence: str, evidence: str) -> str | None:
    """稿中每個『數字+單位/倍數』的 (數值, 單位) 都必須出現在 evidence。5萬元 != 5億元；三千元 == 3000元；10 == 10.0。"""
    s = _MD_YEAR_MASK(_prep(sentence))
    pool = number_pairs(evidence, for_sentence=False)
    for val, unit, raw in number_pairs(s, for_sentence=True):
        if not any(val == v2 and _compat(unit, u2) for v2, u2, _r in pool):
            return f"數字單位「{raw}」在 evidence 找不到相同的數值與單位"
    return None


def _MD_YEAR_MASK(s: str) -> str:
    """日期（M月D日、YYYY年）由 rules.number_problem 既有邏輯處理，這裡遮掉避免重複判定。"""
    s = re.sub(r"\d{1,2}月\s*\d{1,2}日", "〔日期〕", s)
    return re.sub(r"\d{4}年", "〔年〕", s)


def strip_multiplied(s: str) -> str:
    """把『阿拉伯數字 + 倍數』整段遮掉（已由 unit_problem 以總值比對），避免 token 檢查要求尾數逐字相同。"""
    return re.sub(r"\d+(?:\.\d+)?[千百萬億兆]+", "〔量〕", s)


def canon_token(tok: str) -> str:
    """10.0 -> 10、3.50 -> 3.5；保留正負號、上標、%。"""
    m = re.fullmatch(r"([+\-]?)(\d+)(?:\.(\d+))?([⁰¹²³⁴⁵⁶⁷⁸⁹]*)(%?)", tok)
    if not m:
        return tok
    sign, ip, frac, sup, pct = m.groups()
    frac = (frac or "").rstrip("0")
    return sign + ip + ("." + frac if frac else "") + sup + pct


# ---------- A1 否定一致 ----------
_NEG_EXCLUDE = sorted([
    "不得不", "不少", "不過", "不僅", "不但", "不只", "不錯", "不同", "不斷", "不論", "不管", "不久", "不凡", "不已", "不禁",
    "不免", "不妨", "不愧", "不料", "不外乎", "不光", "不單", "不如", "未來", "無論", "無數", "無限", "無疑", "無人機",
    "無線", "無尾熊", "無花果", "無上", "無比", "無奈", "無窮", "無名", "非常", "非凡", "非洲", "非但", "非得", "是否", "與否", "否則", "沒收", "出沒", "淹沒",
    "埋沒", "沒落", "特別", "分別", "區別", "差別", "別人", "別的", "別處", "別墅", "別名", "別具", "別有", "勿論",
], key=len, reverse=True)
_NEG_RE = re.compile(r"[未不沒無非否勿]|別(?=[再讓想要忘去用擔怕把被做說管])")
_NEG_CHARS = "未不沒無非否勿別"
_CLAUSE_SPLIT = re.compile(r"[，,、；;：:。！？!?\s]+|但是|但|卻|而")


_A_NOT_A = re.compile(r"(.)[不沒]\1")  # 有沒有、會不會、是不是：疑問句型，不是否定


def has_negation(text: str) -> bool:
    t = _A_NOT_A.sub("□", text)
    for w in _NEG_EXCLUDE:
        t = t.replace(w, "□")
    return bool(_NEG_RE.search(t))


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(fold_width(text or "")) if c]


def _content(clause: str) -> list[str]:
    return [c for c in clause if c.isalpha() and c not in _NEG_CHARS]  # 不含數字：數字由 unit/token 檢查負責


def _recall(a: list[str], b_clause: str) -> float:
    bs = set(b_clause)
    return sum(1 for c in a if c in bs) / len(a)


def negation_problem(sentence: str, evidence: str, min_recall: float = 0.75, min_chars: int = 4) -> str | None:
    """逐子句配對（以不含否定字的內容字元覆蓋率配對），配到的兩側否定狀態必須一致。
    例：來源「未通過審查」、稿「通過審查」-> 擋；「不少」「非常」「無論」「不過」等非否定詞已排除。"""
    S, E = _clauses(sentence), _clauses(evidence)
    for a_list, b_list in ((S, E), (E, S)):
        for a in a_list:
            ac = _content(a)
            if len(ac) < min_chars or not b_list:
                continue
            scored = [(_recall(ac, b), b) for b in b_list]
            best = max(r for r, _b in scored)
            if best < min_recall:
                continue
            cands = [b for r, b in scored if r == best]
            if not any(has_negation(b) == has_negation(a) for b in cands):
                return f"否定語氣與 evidence 不一致（「{a[:20]}」）"
    return None


# ---------- A3 字元換位 ----------
_FUNC_CHARS = "的了著在是為於和與及並把被將也都就才仍再還會可能要有不未這那其之以而到從向對給讓"


def _adjacent_swap(a: str, b: str) -> bool:
    """b 恰好是 a 交換相鄰兩個不同字的結果（志明 -> 明志）。整詞對調、旋轉（入札申請/申請入札、前兩天/兩天前）不算。"""
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
            and not (set(a[diff[0]] + a[diff[1]]) & set(_FUNC_CHARS)))  # 對調的是虛字（接著靠/靠著）= 語序不同，不是名字


def transposition_problem(sentence: str, evidence: str) -> str | None:
    """稿中 3–4 字 CJK 片段不在 evidence，但 evidence 有『相鄰兩字對調』的片段（林志明 vs 林明志）-> 擋。
    只抓換位，不做編輯距離；2 字詞（完投/投完）與整詞旋轉不算，避免誤殺語序不同的正常改寫。"""
    sruns = re.findall(r"[㐀-䶿一-鿿]+", sentence or "")
    eruns = re.findall(r"[㐀-䶿一-鿿]+", evidence or "")
    ev_all = "\x00".join(eruns)
    for n in (3, 4):
        ev_windows = set()
        for run in eruns:
            ev_windows |= {run[i:i + n] for i in range(len(run) - n + 1)}
        for run in sruns:
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if g in ev_all:
                    continue
                other = next((w for w in ev_windows if _adjacent_swap(g, w)), None)
                if other:
                    return f"「{g}」與 evidence 的「{other}」只差相鄰兩字對調（疑似名稱錯字）"
    return None


def claim_problem(sentence: str, evidence: str) -> str | None:
    """review._check_claim 呼叫：否定一致、字元換位。（數字單位在 rules.number_problem 內。）"""
    return negation_problem(sentence, evidence) or transposition_problem(sentence, evidence)
