"""第二輪：時效、來源相關性、句子全覆蓋、引用來源、相對時間詞。"""
from datetime import datetime, timedelta, timezone

import run_daily
from src import collect, gates, publish, review, write
from src import select as sel
from src.common import TZ, as_taipei, load_config

CFG = load_config()
MARK = write.COMMENT_MARK
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=TZ)
COMMENT = MARK + "這種故事讓人相信善意真的會循環，也提醒我們租屋市場不是只有對立與算計。"
ZH = "曾峻岳挑戰大聯盟，這是一段很長的中文新聞內文。" * 20


def _art(text, pub="2026-11-08T08:00:00+08:00"):
    return {"text": text, "published": collect.parse_date_tolerant(pub) if pub else None, "error": None}


def _topic(urls, keyword="曾峻岳", **extra):
    return {"keyword": keyword, "sources": [dict({"url": u, "title": "", "source": ""}, **extra) for u in urls]}


# ---- 來源發佈時間 ----
def test_source_without_published_time_rejected(monkeypatch):
    monkeypatch.setattr(collect, "fetch_article", lambda url, timeout=20: _art(ZH, pub=None))
    t = _topic(["https://a.com.tw/1"])
    run_daily.fetch_sources(t, CFG, NOW)
    assert t["sources"][0]["ok"] is False and t["sources"][0]["error"] == "no_published_time"
    # RSS 提供的時間可作為備援
    t = _topic(["https://a.com.tw/1"], rss_published="Sun, 08 Nov 2026 07:00:00 +0800")
    run_daily.fetch_sources(t, CFG, NOW)
    assert t["sources"][0]["ok"] is True and t["sources"][0]["published_str"] == "2026-11-08 07:00"


def test_future_published_time_rejected(monkeypatch):
    monkeypatch.setattr(collect, "fetch_article", lambda url, timeout=20: _art(ZH, pub="2026-11-09T08:00:00+08:00"))
    t = _topic(["https://a.com.tw/1"])
    run_daily.fetch_sources(t, CFG, NOW)
    assert t["sources"][0]["error"] == "bad_published_time"


def test_extract_published_meta_and_jsonld():
    h1 = '<meta property="article:published_time" content="2026-11-06T18:30:00+08:00">'
    assert collect.extract_published(h1).isoformat() == "2026-11-06T18:30:00+08:00"
    h2 = '<script>{"datePublished":"2026-11-07T10:00:00Z"}</script>'
    assert as_taipei(collect.extract_published(h2)).hour == 18
    assert collect.extract_published("<html></html>") is None


# ---- 時效閘門 ----
def _fresh_topic(hours_before):
    pub = datetime(2026, 11, 8, 12, 0, tzinfo=TZ) - timedelta(hours=hours_before)
    return {"sources": [{"ok": True, "published": pub.isoformat()}]}


def test_freshness_gate():
    slot = datetime(2026, 11, 8, 12, 0)  # naive 視為台北時間
    assert "時效關" in run_daily.freshness_problem(_fresh_topic(30), slot, CFG)
    assert run_daily.freshness_problem(_fresh_topic(18.5), slot, CFG)
    assert run_daily.freshness_problem(_fresh_topic(10), slot, CFG) is None
    # 第五輪：不再只看最新來源——每一篇合格來源都要在時效內（舊行為「取最新」正是被 Codex 指出的繞過路徑）
    mixed = {"sources": [_fresh_topic(40)["sources"][0], _fresh_topic(5)["sources"][0]]}
    assert run_daily.freshness_problem(mixed, slot, CFG)
    assert run_daily.freshness_problem({"sources": [{"ok": True, "published": None}]}, slot, CFG)


def test_u23_style_stale_source_dropped():
    """11/6 的稿子、11/8 12:00 發 -> 超過 18 小時 -> 丟棄。"""
    t = {"sources": [{"ok": True, "published": datetime(2026, 11, 6, 20, 0, tzinfo=TZ).isoformat()}]}
    assert run_daily.freshness_problem(t, datetime(2026, 11, 8, 12, 0), CFG)


def test_taipei_timezone_independent_of_local():
    assert as_taipei(datetime(2026, 11, 8, 12, 0)).utcoffset().total_seconds() == 8 * 3600
    assert as_taipei(datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc)).hour == 12


# ---- 來源相關性 ----
def test_irrelevant_source_dropped(monkeypatch):
    off = "網友刷負評，這是一段很長但完全沒提到那位球員的中文內文。" * 20
    arts = {"https://a.com.tw/1": _art(ZH), "https://b.com.tw/2": _art(off)}
    monkeypatch.setattr(collect, "fetch_article", lambda url, timeout=20: arts[url])
    t = _topic(list(arts))
    run_daily.fetch_sources(t, CFG, NOW)
    assert [s["ok"] for s in t["sources"]] == [True, False]
    assert t["sources"][1]["error"] == "irrelevant_to_topic"
    assert [s["url"] for s in t["sources"] if s["ok"]] == ["https://a.com.tw/1"]  # 只有相關來源進入來源數與寫稿


# ---- 句子全覆蓋 ----
DRAFT = {"title": "曼城輸球", "hashtags": ["#足球", "#英超"],
         "body": "曼城今天在客場輸掉比賽。球迷感到失望。\n\n" + COMMENT}
SRC = ["曼城今天在客場輸掉比賽，球迷感到失望。"]


def _res(claims):
    return {"pass": True, "sensitive_category": "none", "unsupported_claims": [], "policy_violations": [],
            "volatile_claims": [], "attribution_dropped": [], "named_entities": [], "opinion_factual_statements": [],
            "claims": claims}


def _claim(sid, evidence):
    """第五輪 schema：以 sentence_id（必查句 C1..Cn）與 source_id 綁定。"""
    return {"sentence_id": sid, "source_id": "S1" if evidence is not None else None,
            "supported": evidence is not None, "evidence": evidence}


def _eval(claims, draft=DRAFT):
    cap = draft["title"] + "\n\n" + draft["body"]
    return review.evaluate(_res(claims), SRC, cap, review.factual_sentences(draft))


FULL = [_claim("C1", "曼城今天在客場輸掉比賽"), _claim("C2", "曼城今天在客場輸掉比賽"),
        _claim("C3", "球迷感到失望")]


def test_factual_sentences_exclude_commentary_include_title():
    sents = review.factual_sentences(DRAFT)
    assert "曼城輸球" in sents and "曼城今天在客場輸掉比賽" in sents and "球迷感到失望" in sents
    assert not any("善意" in x for x in sents)


def test_full_coverage_accepts():
    ok, why = _eval(FULL)
    assert ok, why


def test_missing_sentence_rejected():
    ok, why = _eval(FULL[:2])  # 漏列「球迷感到失望」
    assert not ok and "未被逐句查核" in why


def test_sentence_without_evidence_rejected():
    ok, _why = _eval(FULL[:2] + [_claim("C3", None)])
    assert not ok


def test_commentary_mixed_in_facts_rejected():
    d = dict(DRAFT, body="曼城今天在客場輸掉比賽。這個畫面還真有點漫畫感。\n\n" + COMMENT)
    ok, why = _eval(FULL[:2], draft=d)  # 夾帶的評論句沒有 claim -> 拒絕
    assert not ok and "漫畫感" in why


def test_speculation_rejected_when_codex_marks_unsupported():
    d = dict(DRAFT, body="曼城今天在客場輸掉比賽。這次不是試水溫。\n\n" + COMMENT)
    res = _res(FULL[:2] + [_claim("C3", None)])
    res["unsupported_claims"] = ["這次不是試水溫。"]
    ok, why = review.evaluate(res, SRC, d["title"] + "\n\n" + d["body"], review.factual_sentences(d))
    assert not ok


# ---- 引用來源 ----
def test_cited_urls_only_cited_sources():
    srcs = [{"url": "https://a.com/1", "text": "曼城今天在客場輸掉比賽"}, {"url": "https://b.com/2", "text": "完全不同的內容在這裡"}]
    res = {"claims": [{"sentence_id": "C1", "source_id": "S1", "supported": True, "evidence": "曼城今天在客場輸掉比賽"}]}
    assert review.cited_urls(res, srcs) == ["https://a.com/1"]
    cap = publish.build_caption(DRAFT, review.cited_urls(res, srcs))
    assert "https://" not in cap and "來源：" not in cap  # 2026-11-09：貼文不再列來源網址


# ---- 相對時間詞 ----
def test_relative_time_word_rejected_in_caption():
    terms = sel.load_blocklist()

    def d(text):
        # 第五輪：觀點段前需 >= 3 句事實句（結構關），故補兩句中性事實句
        return {"title": "標題", "body": "兩隊交手過很多次。球迷都很關心。" + text + "\n\n" + COMMENT,
                "hashtags": ["#好笑", "#新聞"],
                "category": "news", "image_prompt": "a calm illustration"}
    ok, why, _c = gates.draft_gate(d("明天就是決賽，大家拭目以待。"), ["https://a.com/1"], ["別的"], CFG, terms)
    assert not ok and "相對時間詞" in why and "明天" in why
    for w in ("下一場", "今晚", "稍後", "即將"):
        assert not gates.draft_gate(d(f"{w}要比賽。"), ["https://a.com/1"], ["別的"], CFG, terms)[0]
    assert gates.draft_gate(d("11 月 6 日的比賽結果如下。"), ["https://a.com/1"], ["別的"], CFG, terms)[0]


def test_write_prompt_includes_publish_time_and_bans_relative_words():
    topic = {"keyword": "k", "category": "news",
             "sources": [{"ok": True, "text": "內文", "source": "s", "title": "t", "published_str": "2026-11-06 18:30"}]}
    p = write.build_prompt(topic, "style", None, 600, "2026-11-08 12:00", ["明天", "下一場"])
    assert "2026-11-08 12:00" in p and "明天" in p and "下一場" in p and "發佈時間=2026-11-06 18:30" in p
