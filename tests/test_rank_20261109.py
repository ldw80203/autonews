"""2026-11-09 選文權重：熱度 50／新鮮 25／類別 10／媒體 5／好寫 10；奇聞時效不限。"""
from datetime import datetime, timedelta

import run_daily
from src import db, rank
from src.common import TZ, load_config

CFG = load_config()
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=TZ)


def _t(kw, url, origin="rss", traffic=0, hours_ago=1.0, feed_cat=None, title=None):
    pub = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"keyword": kw, "traffic": traffic, "origin": origin, "pub": "",
            "sources": [{"title": title or kw, "url": url, "source": "x", "rss_published": pub, "feed_cat": feed_cat}]}


def test_config():
    assert CFG["rank"]["weights"] == {"heat": 50, "fresh": 25, "category": 10, "publisher": 5, "writable": 10}
    assert CFG["freshness"]["unlimited_categories"] == ["奇聞"]


def test_topic_category_sources():
    assert rank.topic_category(_t("a", "https://news.ltn.com.tw/news/politics/breakingnews/1")) == "政治"
    assert rank.topic_category(_t("a", "https://news.ltn.com.tw/news/novelty/breakingnews/1")) == "奇聞"
    assert rank.topic_category(_t("a", "https://ec.ltn.com.tw/article/breakingnews/1")) == "財經"
    assert rank.topic_category(_t("a", "https://star.ettoday.net/news/1")) == "娛樂"
    assert rank.topic_category(_t("a", "https://pets.ettoday.net/news/1")) == "奇聞"
    assert rank.topic_category(_t("a", "https://www.cna.com.tw/news/aopl/2026.aspx")) == "國際"
    assert rank.topic_category(_t("a", "https://www.ettoday.net/news/1.htm", feed_cat="奇聞")) == "奇聞"
    assert rank.topic_category(_t("中職總冠軍賽", "https://www.ettoday.net/news/1.htm")) == "體育"  # 關鍵詞
    assert rank.topic_category(_t("某某某", "https://www.ettoday.net/news/1.htm")) == rank.OTHER


def test_trends_without_trusted_source_skipped():
    con = db.connect(":memory:")
    t1 = _t("公鹿 對 塞爾提克", "https://www.celticsblog.com/x", origin="trends", traffic=20000)
    t2 = _t("日經大漲", "https://ec.ltn.com.tw/article/breakingnews/1", origin="trends", traffic=200)
    out, skipped = rank.rank([t1, t2], con, CFG, NOW)
    assert [t["keyword"] for t in out] == ["日經大漲"]
    assert "可信媒體" in skipped[0]["reason"]


def test_heat_and_freshness_order():
    con = db.connect(":memory:")
    hot = _t("黃仁勳婚外情風波延燒", "https://news.ltn.com.tw/news/world/breakingnews/1", origin="trends", traffic=50000)
    fresh = _t("颱風路徑北修明起風雨增", "https://www.cna.com.tw/news/ahel/1.aspx", hours_ago=0.5)
    stale = _t("央行理監事會維持利率不變", "https://www.cna.com.tw/news/aie/1.aspx", hours_ago=20)
    out, _ = rank.rank([stale, fresh, hot], con, CFG, NOW)
    assert [t["keyword"] for t in out] == [hot["keyword"], fresh["keyword"], stale["keyword"]]
    assert out[1]["rank"]["fresh"] == 1.0 and out[2]["rank"]["fresh"] < 0.2


def test_same_story_merged_and_coverage_counts_publishers():
    con = db.connect(":memory:")
    a = _t("黃仁勳承認婚外情 輝達董事會將展開調查", "https://www.cna.com.tw/news/aopl/1.aspx")
    b = _t("黃仁勳承認婚外情！輝達董事會將展開調查", "https://www.mirrormedia.mg/story/1")
    c = _t("颱風路徑北修明起風雨增", "https://www.cna.com.tw/news/ahel/1.aspx")
    out, skipped = rank.rank([a, b, c], con, CFG, NOW)
    assert len(out) == 2 and any("同一則新聞" in s["reason"] for s in skipped)
    merged = next(t for t in out if "黃仁勳" in t["keyword"])
    assert merged["rank"]["coverage"] == 2 and merged["rank"]["heat"] > 0
    # 另一家的報導併進來源；鏡週刊看不出分區，整組沿用中央社國際版的類別
    assert {s["url"] for s in merged["sources"]} == {a["sources"][0]["url"], b["sources"][0]["url"]}
    assert merged["rank"]["cat"] == "國際"


def test_novelty_old_is_not_penalised_and_has_base_heat():
    con = db.connect(":memory:")
    old = _t("平均73歲南韓最年長女團出道", "https://news.ltn.com.tw/news/novelty/breakingnews/1", hours_ago=24 * 30)
    out, _ = rank.rank([old], con, CFG, NOW)
    r = out[0]["rank"]
    assert r["cat"] == "奇聞" and r["fresh"] == 1.0 and r["heat"] == 0.3


def test_category_and_publisher_balance_from_today_posts(tmp_path):
    con = db.connect(":memory:")
    import json
    d = tmp_path / "p1"
    d.mkdir()
    (d / "source.json").write_text(json.dumps(_t("x", "https://ec.ltn.com.tw/article/breakingnews/9"),
                                              ensure_ascii=False), encoding="utf-8")
    db.reserve(con, "k1", "x", ["https://ec.ltn.com.tw/article/breakingnews/9"], "2026-11-09T08:00", "live", str(d))
    fin = _t("台股收盤大漲創新高", "https://ec.ltn.com.tw/article/breakingnews/2")
    soc = _t("颱風路徑北修明起風雨增", "https://www.cna.com.tw/news/asoc/1.aspx")
    out, _ = rank.rank([fin, soc], con, CFG, NOW)
    by = {t["keyword"]: t["rank"] for t in out}
    assert by[fin["keyword"]]["category"] == 0.25  # 今天已有 1 篇財經且是上一篇 -> 1/2 * 0.5
    assert by[soc["keyword"]]["category"] == 1.0
    assert by[fin["keyword"]]["publisher"] == 0.5 and by[soc["keyword"]]["publisher"] == 1.0


def test_freshness_gate_unlimited_for_novelty():
    slot = NOW + timedelta(hours=1)
    src = {"published": (NOW - timedelta(days=40)).isoformat(), "ok": True,
           "url": "https://news.ltn.com.tw/news/novelty/breakingnews/1"}
    novelty = {"keyword": "怪事", "sources": [dict(src)]}
    normal = {"keyword": "台股", "sources": [dict(src, url="https://ec.ltn.com.tw/article/breakingnews/1")]}
    assert run_daily.max_age_hours(novelty, CFG) is None
    assert run_daily.freshness_problem(novelty, slot, CFG) is None
    assert "stale" in run_daily.freshness_problem(normal, slot, CFG)
    no_time = {"keyword": "怪事", "sources": [dict(src, published=None)]}
    assert "no_published_time" in run_daily.freshness_problem(no_time, slot, CFG)
