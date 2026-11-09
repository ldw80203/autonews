"""選題：分類、黑名單、來源關、去重、評分。（只能以 src.select 匯入，勿把 src/ 放進 sys.path。）"""
from __future__ import annotations

from pathlib import Path

import ipaddress
import re
import unicodedata
from urllib.parse import urlparse

from .common import BLOCKLIST_PATH, cjk_ratio, norm_key
from . import db

VIRAL_WORDS = ["網友", "網曝", "PO文", "po文", "Po文", "爆紅", "Dcard", "dcard", "PTT", "ptt",
               "Threads", "threads", "臉書社團", "網路熱議", "引發熱議", "酸民", "鄉民"]


def load_blocklist(path: Path | None = None) -> list[str]:
    out = []
    for line in Path(path or BLOCKLIST_PATH).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def blocklist_hit(texts: list[str], terms: list[str]) -> str | None:
    """任一文字含黑名單詞 -> 回該詞；否則 None。"""
    for t in texts:
        if not t:
            continue
        for term in terms:
            if term in t:
                return term
    return None


def classify(topic: dict, ok_only: bool = False) -> str:
    """ok_only=False：抓內文前的初步分類（任一標題含網友熱議詞即 viral）。
    ok_only=True：只用合格（ok）來源重新判斷——每一篇合格來源的標題都含網友熱議詞才算 viral；
    時事與網友熱議混合、或沒有合格來源，一律 news（套用 2 家門檻）。"""
    if ok_only:
        titles = [s.get("title") or "" for s in topic.get("sources", []) if s.get("ok")]
        if titles and all(any(w in t for w in VIRAL_WORDS) for t in titles):
            return "viral"
        return "news"
    texts = [topic.get("keyword", "")] + [s.get("title", "") for s in topic.get("sources", [])]
    blob = " ".join(texts)
    return "viral" if any(w in blob for w in VIRAL_WORDS) else "news"


_SLD = {"com", "org", "net", "gov", "edu", "co", "ac", "idv"}


def domain_of(url: str) -> str | None:
    """正規化媒體網域：小寫、去 www./m./amp.，取可註冊網域（ltn.com.tw）。認不出來回 None。"""
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = [l for l in host.split(".") if l]
    while labels and labels[0] in ("www", "m", "amp"):
        labels = labels[1:]
    if len(labels) < 2 or not all(l.replace("-", "").isalnum() for l in labels):
        return None
    if len(labels) >= 3 and labels[-2] in _SLD and len(labels[-1]) == 2:
        labels = labels[-3:]
    else:
        labels = labels[-2:]
    return ".".join(labels)


def _fold(s: str) -> str:
    """比對用正規化：NFKC（全形->半形）、小寫、移除所有空白。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").lower())


_ALNUM_TOKEN = re.compile(r"[a-z0-9]+")
_CJK = r"[㐀-䶿一-鿿]"


def _alnum_tokens(keyword: str) -> list[str]:
    """關鍵詞中的英數 token（NFKC、小寫；單一字母也算，例如「張三 A」的 a）。"""
    return list(dict.fromkeys(_ALNUM_TOKEN.findall(unicodedata.normalize("NFKC", keyword or "").lower())))


def keyword_units(keyword: str) -> list[str]:
    """關鍵詞比對單位：NFKC 正規化、小寫後，(1) 每段連續中文的相鄰二字 bigram；(2) 英數 token（任何長度，如 u23、a）。"""
    kw = unicodedata.normalize("NFKC", keyword or "").lower()
    units: list[str] = []
    for run in re.findall(_CJK + "+", kw):
        units += [run[i:i + 2] for i in range(len(run) - 1)]
    units += _ALNUM_TOKEN.findall(kw)
    return list(dict.fromkeys(units))  # 去重、保序


def keyword_too_short(keyword: str) -> bool:
    """有意義字元（中文字 + 英數）少於 2 個 -> 太短，無法判斷相關性。"""
    kw = unicodedata.normalize("NFKC", keyword or "").lower()
    return len(re.findall(_CJK + "|[a-z0-9]", kw)) < 2


def _hay(title: str, text: str, body_chars: int) -> tuple[str, set[str]]:
    """回 (中文比對用：NFKC 小寫去空白, 英數完整 token 集合：保留空白分隔，英數 token 必須整段相等)。"""
    raw = unicodedata.normalize("NFKC", (title or "") + "\n" + (text or "")[:body_chars]).lower()
    return re.sub(r"\s+", "", raw), set(_ALNUM_TOKEN.findall(raw))


def match_ratio(keyword: str, title: str, text: str, body_chars: int) -> float:
    """關鍵詞單位出現在『來源標題 + 內文前 body_chars 字』的比例。中文 bigram 用子字串比對；
    英數 token 用完整 token 比對（"ai" 不會命中 "said"、"taiwan"）。"""
    units = keyword_units(keyword)
    if not units:
        return 0.0
    cjk_hay, tokens = _hay(title, text, body_chars)
    hit = 0
    for u in units:
        if _ALNUM_TOKEN.fullmatch(u):
            hit += u in tokens
        else:
            hit += u in cjk_hay
    return hit / len(units)


def is_relevant(text: str, keyword: str, rcfg: dict, title: str = "") -> bool:
    """來源相關性（fail-closed）：
    1. 關鍵詞有意義字元 < 2 -> 不相關；
    2. 關鍵詞中的每個英數 token（u23、a、AI）都必須以完整 token 出現——識別字元不得被比例抵銷；
    3. 單位命中比例 >= min_match_ratio。"""
    if keyword_too_short(keyword):
        return False
    _cjk_hay, tokens = _hay(title, text, rcfg["body_chars"])
    if not all(t in tokens for t in _alnum_tokens(keyword)):
        return False
    return match_ratio(keyword, title, text, rcfg["body_chars"]) >= rcfg["min_match_ratio"]


def publisher_of(url: str, publishers: dict | None = None) -> str | None:
    """網址 -> 出版者識別。publishers（config [media.publishers]，網域 -> 出版者名）有列的回出版者名；
    沒列的回網域本身（是否可信由 trusted_media 判斷）。認不出網域回 None。"""
    d = domain_of(url)
    if d is None:
        return None
    return (publishers or {}).get(d, d)


def trusted_media(url: str, publishers: dict) -> bool:
    """可信媒體：正規化網域必須明列在 config [media.publishers]。"""
    d = domain_of(url)
    return d is not None and d in publishers


def source_gate(topic: dict, category: str, publishers: dict | None = None,
                min_news: int = 2) -> tuple[bool, str]:
    """來源關。sources 內每筆需有 ok(bool：抓到內文、是中文)。以出版者去重（轉址後網址優先；
    同集團多網域算一家，見 config [media.publishers]），認不出網域者不計。
    min_news：時事需要的不同出版者數（config [select].min_news_publishers）。"""
    domains = {publisher_of(s.get("final_url") or s.get("url", ""), publishers)
               for s in topic.get("sources", []) if s.get("ok")}
    domains.discard(None)
    if category == "news":
        if len(domains) < min_news:
            return False, f"來源關：時事需 ≥{min_news} 個不同媒體網域且抓到中文內文，實際 {len(domains)}"
    elif len(domains) < 1:
        return False, "來源關：網友熱議需 ≥1 個新聞媒體且抓到中文內文，實際 0"
    return True, ""


def prefilter(topics: list[dict], terms: list[str], con, cfg: dict | None = None) -> tuple[list[dict], list[dict]]:
    """抓內文前的便宜過濾：黑名單（標題層）、去重。回 (候選依流量排序, 丟棄清單)。"""
    cands, rejected = [], []
    seen_keys: set[str] = set()
    for t in topics:
        key = norm_key(t["keyword"])
        urls = [s["url"] for s in t["sources"]]
        titles = [t["keyword"]] + [s["title"] for s in t["sources"]]
        if cfg and cjk_ratio(t["keyword"]) < cfg["language"]["min_cjk_ratio_title"]:
            rejected.append({"keyword": t["keyword"], "reason": "非中文話題"})
            continue
        hit = blocklist_hit(titles, terms)
        if hit:
            rejected.append({"keyword": t["keyword"], "reason": f"黑名單命中「{hit}」"})
            continue
        if not urls:
            rejected.append({"keyword": t["keyword"], "reason": "沒有任何來源連結"})
            continue
        if key in seen_keys:
            rejected.append({"keyword": t["keyword"], "reason": "本次重複話題"})
            continue
        dup = db.is_duplicate(con, key, urls)
        if dup:
            rejected.append({"keyword": t["keyword"], "reason": dup})
            continue
        seen_keys.add(key)
        t["norm_key"] = key
        t["category"] = classify(t)
        cands.append(t)
    cands.sort(key=lambda x: (x["traffic"], len(x["sources"])), reverse=True)
    return cands, rejected
