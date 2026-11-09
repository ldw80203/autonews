"""2026-11-09 使用者裁示放寬標準：原創門檻 40、觀點段 6 字重疊、不合格 hashtag 剔除不丟稿。"""
import tomllib
from pathlib import Path

from src import gates

CFG = tomllib.loads(Path(__file__).resolve().parents[1].joinpath("config", "config.toml").read_text(encoding="utf-8"))
WL = CFG["hashtags"]["whitelist"]


def test_originality_threshold_loosened():
    assert CFG["originality"]["max_overlap_chars"] == 60


# ---------- 事實關：多段引用、相對時間換算 ----------
from datetime import datetime  # noqa: E402

from src import review, rules, write  # noqa: E402
from src.common import load_config  # noqa: E402

FULL = load_config()
OP = write.COMMENT_MARK + "追夢這件事從來不容易，願意再試一次的勇氣本身就很值得掌聲，希望一切順利。"
SRC_TEXT = ("凱基投顧董事長朱晏民今天出席論壇。\n"
            "朱晏民分析，美股回檔的源頭在於投資人擔憂高資本支出能否變現。\n"
            "馬斯克去年涉入政府效率部解僱聯邦員工。\n"
            "台北另一場活動照常舉行。")


def _ev(sentence, evidence, src_id="S1"):
    draft = {"title": "投顧看後市走勢分析", "category": "news", "hashtags": ["#財經"],
             "body": sentence + "。\n台北另一場活動照常舉行。\n朱晏民出席論壇。\n\n" + OP}
    srcs = [{"id": "S1", "published": "2026-11-09T03:00:00+08:00", "text": "投顧看後市走勢分析\n" + SRC_TEXT},
            {"id": "S2", "published": "2026-11-09T03:00:00+08:00", "text": "完全無關的另一篇報導內容。"}]
    req = review.factual_sentences(draft)
    evs = [["投顧看後市走勢分析"], evidence, ["台北另一場活動照常舉行"], ["朱晏民今天出席論壇"]]
    claims = [{"sentence_id": f"C{i + 1}", "source_id": src_id if i == 1 else "S1", "evidence": e, "supported": True}
              for i, e in enumerate(evs)]
    res = {"pass": True, "sensitive_category": "none", "claims": claims}
    res.update({k: [] for k in review.LIST_FIELDS})
    return review.evaluate(res, srcs, draft["title"] + "\n" + draft["body"], req, draft, FULL, "投顧")


def test_multi_quote_across_paragraphs_passes():
    ok, why = _ev("凱基投顧董事長分析，美股回檔的源頭在於投資人擔憂高資本支出能否變現",
                  ["凱基投顧董事長朱晏民", "美股回檔的源頭在於投資人擔憂高資本支出能否變現"])
    assert ok, why


def test_multi_quote_still_must_be_verbatim_and_same_source():
    assert not _ev("凱基投顧董事長分析，美股回檔的源頭在於投資人擔憂", ["凱基投顧董事長", "美股回檔是因為投資人擔憂"])[0]
    assert not _ev("凱基投顧董事長分析，美股回檔的源頭在於投資人擔憂",
                   ["凱基投顧董事長朱晏民", "美股回檔的源頭在於投資人擔憂"], src_id="S2")[0]
    assert not _ev("凱基投顧董事長分析", ["凱基", "投顧", "董事", "長朱"])[0]  # 超過 3 段


def test_relative_year_converted_from_published():
    pub = datetime.fromisoformat("2026-11-09T03:00:00+08:00")
    assert rules.number_problem("馬斯克在2025年涉入政府效率部", "馬斯克去年涉入政府效率部", pub) is None
    assert rules.number_problem("民主黨2027年1月掌控國會", "民主黨明年1月掌控國會", pub) is None
    assert rules.number_problem("馬斯克在2024年涉入政府效率部", "馬斯克去年涉入政府效率部", pub)
    assert rules.number_problem("球賽11月8日開打", "球賽昨天開打", pub) is None
    assert rules.number_problem("球賽11月7日開打", "球賽昨天開打", pub)


def test_sanitize_drops_bad_tags_keeps_good():
    allowed = ["南海爭端", "南海爭端延燒！菲外交部罕見重申「文人領軍」"]
    out = gates.sanitize_hashtags(["#南海爭端", "#菲律賓", "#國際", "#國際", "#有 空白"], allowed, WL)
    assert out == ["#南海爭端", "#國際"]


def test_sanitize_all_bad_falls_back():
    assert gates.sanitize_hashtags(["#菲律賓", "壞"], ["南海"], WL) == ["#新聞"]
    assert gates.sanitize_hashtags([], ["南海"], WL) == ["#新聞"]


def test_sanitize_caps_and_passes_gate():
    out = gates.sanitize_hashtags(["#新聞", "#時事", "#熱門", "#國際", "#社會", "#生活"], [], WL)
    assert len(out) == gates.MAX_HASHTAGS
    assert gates.check_hashtags(out) is None
