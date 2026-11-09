"""選文權重（2026-11-09 使用者裁示）：每個候選算 0–100 分，高分先做。

配分在 config [rank]：熱度 50／新鮮 25／類別 10／媒體 5／好寫 10（「熱度優先」）。
- 熱度：Google Trends 流量（取對數）與「幾家不同媒體報同一則」取大者；奇聞類另有基本分（無點閱數可量）。
- 新鮮：2 小時內滿分，24 小時降到 0；[freshness] unlimited_categories（奇聞）不扣分。
- 類別：8 類均衡輪替，今天發越少分越高，跟上一篇同類別再打折。政治不特別處理。
- 媒體：今天越少用到的出版者分越高。
- 好寫：有可信媒體來源、標題是中文。
硬規則：Trends 題目完全沒有可信媒體來源就跳過（來源關一定擋，不浪費抓取時間）；同一則新聞只留最高分那篇。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .common import cjk_ratio
from . import image
from . import select as sel

CATEGORIES = ["政治", "財經", "國際", "社會", "娛樂", "體育", "生活科技", "奇聞"]
OTHER = "其他"

_LTN_SUB = {"ec": "財經", "ent": "娛樂", "sports": "體育", "health": "生活科技", "3c": "生活科技", "def": "政治"}
_LTN_PATH = {"politics": "政治", "society": "社會", "life": "生活科技", "world": "國際", "novelty": "奇聞",
             "local": "社會", "business": "財經", "entertainment": "娛樂", "sports": "體育"}
_ETTODAY_SUB = {"star": "娛樂", "sports": "體育", "pets": "奇聞", "speed": "生活科技", "travel": "生活科技",
                "health": "生活科技", "finance": "財經", "game": "生活科技"}
_CNA_CODE = {"aipl": "政治", "asoc": "社會", "ahel": "生活科技", "acn": "國際", "aie": "財經", "aloc": "社會",
             "acul": "娛樂", "aspt": "體育", "aopl": "國際", "ait": "生活科技", "afe": "財經"}
_SCENE_CAT = {"baseball": "體育", "basketball": "體育", "football": "體育", "racket": "體育", "sports": "體育",
              "weather": "生活科技", "traffic": "生活科技", "environment": "生活科技", "food": "生活科技",
              "travel": "生活科技", "tech": "生活科技", "finance": "財經", "politics": "政治",
              "entertainment": "娛樂"}


def _url_category(url: str) -> str | None:
    p = urlparse(url or "")
    host, parts = (p.hostname or "").lower(), [x for x in p.path.lower().split("/") if x]
    if host.endswith("ltn.com.tw"):
        sub = host.split(".")[0]
        if sub in _LTN_SUB:
            return _LTN_SUB[sub]
        if len(parts) >= 2 and parts[0] == "news":
            return _LTN_PATH.get(parts[1])
    if host.endswith("ettoday.net"):
        return _ETTODAY_SUB.get(host.split(".")[0])
    if host.endswith("cna.com.tw") and len(parts) >= 2 and parts[0] == "news":
        return _CNA_CODE.get(parts[1])
    return None


def topic_category(topic: dict) -> str:
    """feed 標記 > 網址分區 > 配圖關鍵詞分類 > 其他。結果快取在 topic['topic_cat']。"""
    if topic.get("topic_cat"):
        return topic["topic_cat"]
    cat = next((s["feed_cat"] for s in topic.get("sources", []) if s.get("feed_cat")), None)
    if not cat:
        cat = next((c for c in (_url_category(s.get("final_url") or s.get("url", ""))
                                for s in topic.get("sources", [])) if c), None)
    if not cat:
        tmp = {"keyword": topic.get("keyword", ""),
               "sources": [dict(s, ok=True) for s in topic.get("sources", [])]}
        cat = _SCENE_CAT.get(image.scene_for(tmp))
    topic["topic_cat"] = cat or OTHER
    return topic["topic_cat"]


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[^\w]", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _similar(a: set[str], b: set[str], th: float) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= th


def _published(topic: dict) -> datetime | None:
    from .collect import parse_date_tolerant
    best = None
    for raw in [s.get("rss_published") for s in topic.get("sources", [])] + [topic.get("pub")]:
        dt = parse_date_tolerant(raw, strict=True) if raw else None
        if dt and (best is None or dt > best):
            best = dt
    return best


def _publisher(topic: dict, pubs: dict) -> str | None:
    for s in topic.get("sources", []):
        p = sel.publisher_of(s.get("final_url") or s.get("url", ""), pubs)
        if p:
            return p
    return None


def today_posts(con, today: str, pubs: dict) -> list[tuple[str, str | None, str]]:
    """今天已排/已發的 live 貼文 (slot, 出版者, 類別)，依 slot 排序。類別從 post 目錄的 source.json 重算。"""
    rows = con.execute("SELECT slot, urls, path FROM posts WHERE mode='live' AND status IN ('pending','scheduled') "
                       "AND slot LIKE ? ORDER BY slot", (today + "%",)).fetchall()
    out = []
    for slot, urls, path in rows:
        pub = next((p for p in (sel.publisher_of(u, pubs) for u in (urls or "").split("\n") if u) if p), None)
        cat = OTHER
        try:
            cat = topic_category(json.loads((Path(path) / "source.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
        out.append((slot, pub, cat))
    return out


def _recent_posts(con, now: datetime, hours: float) -> list[tuple[str, str]]:
    """最近 hours 小時內已排/已發的 live 貼文 (話題詞, 實際貼文標題)；標題讀不到就空字串。"""
    cutoff = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    rows = con.execute("SELECT keyword, path FROM posts WHERE mode='live' AND status IN ('pending','scheduled') "
                       "AND slot >= ? ORDER BY slot", (cutoff,)).fetchall()
    out = []
    for kw, path in rows:
        title = ""
        try:
            title = json.loads((Path(path) / "draft.json").read_text(encoding="utf-8")).get("title", "")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        out.append((kw, title if isinstance(title, str) else ""))
    return out


def recent_titles(con, now: datetime, hours: float) -> list[str]:
    """給寫稿模型看的『最近已發標題』清單，用來判斷是不是同一起事件。"""
    return [f"{t}（{kw}）" if t and t != kw else kw for kw, t in _recent_posts(con, now, hours)]


def score(topic: dict, cfg: dict, now: datetime, history: list, coverage: int) -> tuple[float, dict]:
    rc, pubs = cfg["rank"], cfg["media"]["publishers"]
    cat = topic_category(topic)
    unlimited = cat in cfg["freshness"].get("unlimited_categories", [])
    trends = min(1.0, math.log10(topic["traffic"]) / 5) if topic.get("traffic", 0) > 0 else 0.0
    cov = min(1.0, (coverage - 1) / 3) if coverage > 1 else 0.0
    heat = max(trends, cov, rc.get("heat_base", {}).get(cat, 0.0))
    pub_dt = _published(topic)
    if unlimited:
        fresh = 1.0
    elif pub_dt is None:
        fresh = 0.3
    else:
        hours = (now - pub_dt).total_seconds() / 3600
        fresh = 1.0 if hours <= 2 else max(0.0, 1 - (hours - 2) / 22)
    cats = [c for _s, _p, c in history]
    catv = 1 / (1 + cats.count(cat))
    if cats and cats[-1] == cat:
        catv *= 0.5
    publisher = _publisher(topic, pubs)
    pubv = 1 / (1 + [p for _s, p, _c in history].count(publisher))
    trusted = any(sel.trusted_media(s.get("url", ""), pubs) for s in topic.get("sources", []))
    titles = [topic.get("keyword", "")] + [s.get("title") or "" for s in topic.get("sources", [])]
    chinese = max(cjk_ratio(t) for t in titles) >= 0.5
    write = 0.5 * trusted + 0.5 * chinese
    parts = {"heat": heat, "fresh": fresh, "category": catv, "publisher": pubv, "writable": write}
    total = sum(rc["weights"][k] * v for k, v in parts.items())
    return round(total, 1), dict(parts, cat=cat, pub_name=publisher, coverage=coverage)


def rank(cands: list[dict], con, cfg: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    """回 (依分數排序的候選, 跳過清單)。每個候選寫入 topic['rank'] = {score, 各分項}。"""
    pubs, rc = cfg["media"]["publishers"], cfg["rank"]
    skipped, pool = [], []
    th = rc.get("same_story_similarity", 0.35)
    # 2026-11-09 使用者回報同一事件（黃仁勳婚外情）一天發三篇：先用標題相似度擋掉最近已發過的同一則
    posted = [(kw, _bigrams(kw), _bigrams(title)) for kw, title in
              _recent_posts(con, now, rc.get("recent_post_hours", 48))]
    for t in cands:
        g = _bigrams(t["keyword"])
        dup = next((kw for kw, gk, gt in posted if _similar(g, gk, th) or _similar(g, gt, th)), None)
        if t.get("origin") == "trends" and not any(sel.trusted_media(s.get("url", ""), pubs)
                                                   for s in t.get("sources", [])):
            skipped.append({"keyword": t["keyword"], "reason": "排序：Trends 題目沒有可信媒體來源，跳過"})
        elif dup:
            skipped.append({"keyword": t["keyword"], "reason": f"排序：和最近已發的「{dup}」是同一則新聞，跳過"})
        else:
            pool.append(t)
    grams = [_bigrams(t["keyword"]) for t in pool]
    pubsets = [{_publisher(t, pubs)} - {None} for t in pool]
    cluster = list(range(len(pool)))  # 簡單 union-find：標題相似就同一則
    def root(i):
        while cluster[i] != i:
            cluster[i] = cluster[cluster[i]]
            i = cluster[i]
        return i
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if _similar(grams[i], grams[j], th):
                cluster[root(j)] = root(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(pool)):
        groups.setdefault(root(i), []).append(i)
    history = today_posts(con, now.strftime("%Y-%m-%d"), pubs)
    max_src = rc.get("max_merged_sources", 4)
    out = []
    for idx in groups.values():
        members = [pool[i] for i in idx]
        # 同一則新聞：任一篇判斷得出類別，整組沿用（例：鏡週刊看不出分區、中央社國際版看得出）
        cat = next((c for c in (topic_category(m) for m in members) if c != OTHER), OTHER)
        coverage = len(set().union(*(pubsets[i] for i in idx)))
        for m in members:
            m["topic_cat"] = cat
            s, parts = score(m, cfg, now, history, coverage)
            m["rank"] = dict(parts, score=s)
        best = max(members, key=lambda m: m["rank"]["score"])
        # 其他家的報導併進來源，讓寫稿時能整合多家說法（可信媒體優先，最多 max_src 篇）
        seen = {s["url"] for s in best["sources"]}
        extra = [s for m in members if m is not best for s in m["sources"] if s["url"] not in seen]
        extra.sort(key=lambda s: not sel.trusted_media(s.get("url", ""), pubs))
        best["sources"] = best["sources"] + extra[:max(0, max_src - len(best["sources"]))]
        for m in members:
            if m is not best:
                skipped.append({"keyword": m["keyword"], "reason": f"排序：同一則新聞，來源已併入「{best['keyword']}」"})
        out.append(best)
    out.sort(key=lambda x: x["rank"]["score"], reverse=True)
    return out, skipped
