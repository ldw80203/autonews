"""收集：Google Trends 台灣 RSS + 自由時報 / ETtoday RSS；trafilatura 抓內文。"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
import trafilatura

from .common import as_taipei, load_config

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AutoNews/0.1"}
HT = "{https://trends.google.com/trending/rss}"


def parse_traffic(s: str | None) -> int:
    """'2,000+' -> 2000；'1.5萬+' -> 15000；無法解析 -> 0。"""
    if not s:
        return 0
    s = s.strip().replace(",", "").replace("+", "")
    m = re.match(r"^([\d.]+)\s*([萬万KkMm]?)", s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    mult = {"萬": 10_000, "万": 10_000, "k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(num * mult)


def _parse_raw(s: str) -> datetime | None:
    """解析成 datetime，保留 naive（不補時區）。"""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        # 未知時區縮寫（XYZ）或 -0000 時，parsedate_to_datetime 回 naive；由呼叫端以 tzinfo is None 判斷
        return parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_date_tolerant(s: str | None, strict: bool = False) -> datetime | None:
    """容錯日期解析（ETtoday 格式不標準）。解析不了回 None，不丟例外。
    strict=False：無時區者視為台北時間（只給非時效用途）。
    strict=True：沒有明確、可辨識的時區（naive、未知縮寫、-0000）一律回 None——時效判斷一律用 strict。"""
    if not s or not isinstance(s, str):
        return None
    dt = _parse_raw(" ".join(s.split()))
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None if strict else as_taipei(dt)
    return as_taipei(dt)


_META_KEYS = (r"article:published_time|og:article:published_time|article:published|pubdate|publishdate|"
              r"datepublished|dc\.date\.issued|date")
_META_A = re.compile(r"<meta[^>]+(?:property|name|itemprop)=[\"'](?:%s)[\"'][^>]*?content=[\"']([^\"']+)" % _META_KEYS, re.I)
_META_B = re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*?(?:property|name|itemprop)=[\"'](?:%s)[\"']" % _META_KEYS, re.I)
_JSONLD = re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I)


def extract_published(html: str, strict: bool = False) -> datetime | None:
    """從 HTML 抓發佈時間：meta 標籤 > JSON-LD > trafilatura metadata（只有日期，視為當日 00:00，偏保守）。
    strict=True：只接受帶明確時區的時間；trafilatura 只給日期（無時區）不採用。"""
    for rx in (_META_A, _META_B, _JSONLD):
        for m in rx.finditer(html or ""):
            dt = parse_date_tolerant(m.group(1), strict=strict)
            if dt:
                return dt
    if strict:
        return None
    try:
        md = trafilatura.extract_metadata(html)
        if md is not None and md.date:
            return parse_date_tolerant(md.date)
    except Exception:  # noqa: BLE001
        pass
    return None


def parse_trends_xml(xml_bytes: bytes) -> list[dict]:
    """解析 Trends RSS：關鍵字、流量、news_item（title/url/source）。"""
    topics = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        kw = (item.findtext("title") or "").strip()
        if not kw:
            continue
        sources = []
        for ni in item.findall(f"{HT}news_item"):
            url = (ni.findtext(f"{HT}news_item_url") or "").strip()
            if not url:
                continue
            sources.append({
                "title": (ni.findtext(f"{HT}news_item_title") or "").strip(),
                "url": url,
                "source": (ni.findtext(f"{HT}news_item_source") or "").strip(),
            })
        topics.append({
            "keyword": kw,
            "traffic": parse_traffic(item.findtext(f"{HT}approx_traffic")),
            "pub": (item.findtext("pubDate") or "").strip(),
            "origin": "trends",
            "sources": sources,
        })
    return topics


def parse_rss_articles(content: bytes, source_name: str, feed_cat: str | None = None) -> list[dict]:
    """解析一般新聞 RSS（feedparser 容錯；日期壞掉照樣保留）。feed_cat：config 標記的類別（給 rank 用）。"""
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        dt = parse_date_tolerant(e.get("published") or e.get("updated"), strict=True)  # 無時區不補台北時間
        out.append({"title": title, "url": link, "source": source_name,
                    "published": dt.isoformat() if dt else None, "feed_cat": feed_cat})
    return out


def _get(url: str, timeout: int) -> bytes | None:
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        return r.content
    except requests.RequestException:
        return None


def fetch_article(url: str, timeout: int = 20) -> dict:
    """抓內文與發佈時間。回 {text, published(datetime|None，必帶時區), final_url, error}。任何失敗都不丟例外。
    final_url 是轉址後的實際網址：媒體身分以它為準。"""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        final = str(r.url or url)
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False, favor_precision=True)
        if not text:
            return {"text": None, "published": None, "final_url": final, "error": "extract_empty"}
        return {"text": text.strip(), "published": extract_published(r.text, strict=True), "final_url": final,
                "error": None}
    except Exception as e:  # noqa: BLE001 - 單一來源失敗不得讓流程崩潰
        return {"text": None, "published": None, "final_url": None, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def collect_topics(cfg: dict | None = None) -> tuple[list[dict], list[str]]:
    """回 (topics, errors)。RSS 文章補進 Trends 話題的 sources；未匹配者各自成為單來源話題。"""
    cfg = cfg or load_config()
    to = cfg["timeouts"]["http"]
    errors: list[str] = []

    raw = _get(cfg["feeds"]["trends"], to)
    topics: list[dict] = []
    if raw is None:
        errors.append("trends: 抓取失敗")
    else:
        try:
            topics = parse_trends_xml(raw)
        except ET.ParseError as e:
            errors.append(f"trends: XML 解析失敗 {e}")

    articles: list[dict] = []
    for feed in cfg["rss_feeds"]:
        raw = _get(feed["url"], to)
        if raw is None:
            errors.append(f"{feed['name']}: 抓取失敗 {feed['url']}")
            continue
        try:
            articles += parse_rss_articles(raw, feed["name"], feed.get("category"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{feed['name']}: 解析失敗 {e}")

    used = set()
    for t in topics:
        known = {s["url"] for s in t["sources"]}
        for a in articles:
            if t["keyword"] in a["title"] and a["url"] not in known:
                t["sources"].append({"title": a["title"], "url": a["url"], "source": a["source"],
                                     "rss_published": a.get("published"), "feed_cat": a.get("feed_cat")})
                known.add(a["url"])
                used.add(a["url"])
    for a in articles:
        if a["url"] not in used:
            topics.append({"keyword": a["title"], "traffic": 0, "pub": a.get("published") or "",
                           "origin": "rss",
                           "sources": [{"title": a["title"], "url": a["url"], "source": a["source"],
                                     "rss_published": a.get("published"), "feed_cat": a.get("feed_cat")}]})
    return topics, errors
