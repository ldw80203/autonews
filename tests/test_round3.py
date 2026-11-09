"""第三輪：正規化（NFKC、小寫、去空白）與子句串接覆蓋。"""
from src import review
from src import select as sel
from src.common import load_config

R = load_config()["relevance"]


def test_relevance_case_width_space_normalization():
    assert sel.is_relevant("今天 U23 世界盃棒球錦標賽 開打", "u23 世界盃棒球錦標賽", R)  # 大小寫 + 空白
    assert sel.is_relevant("Ｕ２３世界盃開打", "U23 世界盃", R)  # 全形英數
    assert sel.is_relevant("u23世界盃", "U23　世界盃", R)  # 全形空白
    assert sel.is_relevant("老鷹 擊敗 籃網", "老鷹 對 籃網", R)  # 中文之間有空白
    assert sel.keyword_units("Ｕ23 世界盃") == ["世界", "界盃", "u23"]


def test_bigram_relevance_u23_wording_difference():
    """Trends 寫「U23 世界盃棒球錦標賽」，來源寫「U23世界盃棒球賽」：整段片段對不上，但 bigram 命中 5/8 >= 0.6。"""
    kw = "u23 世界盃棒球錦標賽"
    body = "第六屆U23世界盃棒球賽今天開打，中華隊首戰擊敗捷克。" * 3
    assert sel.match_ratio(kw, "U23世界盃／中華隊開門紅", body, 1500) == 5 / 8
    assert sel.is_relevant(body, kw, R, "U23世界盃／中華隊開門紅")
    assert sel.keyword_units("世界盃棒球錦標賽") == ["世界", "界盃", "盃棒", "棒球", "球錦", "錦標", "標賽"]


def test_bigram_relevance_rejects_different_matchup():
    """「老鷹 對 籃網」不能對上只寫老鷹對巫師的內文（bigram 1/2 < 0.6）。"""
    text = "NBA／楊恩重返老鷹主場遭噓聲伺候，老鷹對巫師一役，老鷹主場取勝。" * 5
    assert sel.match_ratio("老鷹 對 籃網", "老鷹擊退巫師", text, 1500) == 0.5
    assert not sel.is_relevant(text, "老鷹 對 籃網", R, "老鷹擊退巫師")
    assert not sel.is_relevant("完全無關的內容", "曾峻岳", R)
    assert sel.is_relevant("曾峻岳挑戰大聯盟", "曾峻岳", R)


def test_relevance_only_counts_title_and_first_chars():
    far = "無關的字。" * 400 + "曾峻岳挑戰大聯盟"  # 關鍵詞在 1500 字之後
    assert not sel.is_relevant(far, "曾峻岳", R)
    assert sel.is_relevant(far, "曾峻岳", R, "曾峻岳申請入札")  # 標題算在內


def _res(claims):
    return {"pass": True, "sensitive_category": "none", "unsupported_claims": [], "policy_violations": [],
            "volatile_claims": [], "attribution_dropped": [], "named_entities": [], "opinion_factual_statements": [],
            "claims": claims}


def _c(sid, s, ev):
    """第五輪 schema：claim 以 sentence_id 綁定必查句；附 sentence 時必須與必查句核心完全相等。"""
    return {"sentence_id": sid, "sentence": s, "source_id": "S1", "supported": True, "evidence": ev}


SRC = ["曼城今天在客場輸掉比賽，球迷感到失望。"]
EV = "曼城今天在客場輸掉比賽"


def test_subclause_concatenation_counts_as_covered():
    """第五輪改為『拒絕』：spec 要求每個必查句都要有一筆 claim 與它完全相等，子句串接不再算覆蓋
    （舊行為允許用子句湊句，是 Codex 第 2 次審查列出的覆蓋繞過路徑之一）。"""
    cap = "曼城輸球\n\n曼城今天在客場輸掉比賽，球迷感到失望。"
    req = ["曼城輸球", "曼城今天在客場輸掉比賽，球迷感到失望"]
    split = [_c("C1", "曼城輸球", EV), _c("C2", "曼城今天在客場輸掉比賽，", EV), _c("C2", "球迷感到失望。", "球迷感到失望")]
    ok, why = review.evaluate(_res(split), SRC, cap, req)
    assert not ok and "不相等" in why


def test_subclause_with_missing_piece_still_rejected():
    cap = "曼城輸球\n\n曼城今天在客場輸掉比賽，球迷感到失望。"
    req = ["曼城輸球", "曼城今天在客場輸掉比賽，球迷感到失望"]
    ok, why = review.evaluate(_res([_c("C1", "曼城輸球", EV)]), SRC, cap, req)
    assert not ok and "未被逐句查核" in why


def test_repeated_fragment_cannot_fake_coverage():
    """第五輪：覆蓋改為完全相等，包含與串接都不算。"""
    assert review._is_covered("abcd", ["abcd"])
    assert not review._is_covered("abcd", ["ab", "cd"])  # 串接不算（舊行為為 True）
    assert not review._is_covered("abab", ["ab"])
    assert not review._is_covered("abab", ["ab", "ab"])  # 串接不算（舊行為為 True）
    assert not review._is_covered("abcd", ["cd", "ab"])
    assert not review._is_covered("ab", ["xxabxx"])  # 包含不算


def test_sentence_width_punctuation_normalized():
    cap = "曼城輸球\n\n曼城今天在客場輸掉比賽,球迷感到失望。"  # caption 用半形逗號
    req = ["曼城輸球", "曼城今天在客場輸掉比賽,球迷感到失望"]
    claims = [_c("C1", "曼城輸球", EV), _c("C2", "曼城今天在客場輸掉比賽，球迷感到失望。", EV)]  # Codex 回報用全形逗號
    ok, why = review.evaluate(_res(claims), SRC, cap, req)
    assert ok, why


def test_rss_topic_defining_source_skips_self_relevance(monkeypatch):
    import run_daily
    from datetime import datetime
    from src import collect
    zh = "台股近期受到國際政經局勢變化，為穩定金融市場召開臨時委員會議。" * 15
    monkeypatch.setattr(collect, "fetch_article", lambda url, timeout=20: {
        "text": zh, "published": collect.parse_date_tolerant("2026-11-08T08:00:00+08:00"), "error": None})
    now = datetime(2026, 11, 8, 9, 0, tzinfo=run_daily.TZ)
    title = "國安基金第10次啟動護盤！進場條件、歷次戰績一次看"
    t = {"keyword": title, "origin": "rss", "sources": [{"url": "https://ec.ltn.com.tw/a", "title": title, "source": "x"}]}
    run_daily.fetch_sources(t, load_config(), now)
    assert t["sources"][0]["ok"] is True
    # Trends 話題掛上的來源仍須相關
    t2 = {"keyword": "曾峻岳", "origin": "trends", "sources": [{"url": "https://ec.ltn.com.tw/a", "title": "別的標題", "source": "x"}]}
    run_daily.fetch_sources(t2, load_config(), now)
    assert t2["sources"][0]["error"] == "irrelevant_to_topic"


def test_rss_feeds_configured_and_all_fetched(monkeypatch):
    from src import collect
    cfg = load_config()
    names = {f["name"] for f in cfg["rss_feeds"]}
    assert {"自由時報", "ETtoday", "公視新聞網", "鏡週刊", "中央社"} <= names
    seen = []
    xml = (b'<?xml version="1.0"?><rss version="2.0"><channel><item><title>T</title><link>https://x.com.tw/1</link>'
           b'<pubDate>Sun, 08 Nov 2026 07:00:00 +0800</pubDate></item></channel></rss>')

    def fake_get(url, timeout):
        seen.append(url)
        return None if "trends" in url else xml
    monkeypatch.setattr(collect, "_get", fake_get)
    topics, errors = collect.collect_topics(cfg)
    assert {f["url"] for f in cfg["rss_feeds"]} <= set(seen)
    assert any("trends" in e for e in errors) and topics  # trends 失敗不影響 RSS


def test_write_prompt_politics_and_wording_rules():
    from src import write
    p = write.build_prompt({"keyword": "k", "category": "news", "sources": []}, "style")
    # 2026-11-09 使用者裁示：拿掉「貼近來源、不加形容」的避險寫法，改成整合多來源、通順好讀
    assert "貼近來源" not in p and "整合成一篇完整、通順的敘事" in p
    assert "不要一句一段" in p and "不要為了避險" in p
    assert "不可編造" in p  # 人名、數字、日期、引言仍不可編造
    assert "各方說法" in p and "不准支持或攻擊特定政黨或人物" in p
