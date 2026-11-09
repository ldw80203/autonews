"""第六輪：確定性語意檢查（否定、數字綁單位、字元換位）、誤殺放寬、Claude+Codex 雙審。測試不呼叫任何真實模型。"""
import json

import pytest

from src import review, rules, semantic, write
from src import select as sel
from src.common import load_config

CFG = load_config()
MARK = write.COMMENT_MARK
OPINION = MARK + "追夢這件事從來不容易，願意再試一次的勇氣本身就很值得掌聲，希望一切順利。"
URL = "https://news.ltn.com.tw/culture/r6"


# ---------- A1 否定一致 ----------
def test_a1_negation_flip_rejected():
    assert semantic.negation_problem("補助方案獲審查通過", "補助方案未獲審查通過")
    assert semantic.negation_problem("補助方案未獲審查通過", "補助方案獲審查通過")
    assert semantic.negation_problem("市府沒有說明原因", "市府說明原因")


def test_a1_same_polarity_and_non_negation_words_pass():
    assert semantic.negation_problem("補助方案未獲審查通過", "補助方案未獲審查通過") is None
    for s, e in [("現場有不少民眾排隊", "現場有不少民眾排隊"), ("活動非常熱鬧", "活動非常熱鬧"),
                 ("無論晴雨活動照常舉行", "無論晴雨活動照常舉行"), ("球團表示不過仍在評估", "球團表示不過仍在評估"),
                 ("活動非常熱鬧", "活動熱鬧"), ("現場有民眾排隊", "現場有不少民眾排隊")]:
        assert semantic.negation_problem(s, e) is None, (s, e)
    # evidence 另一子句有否定，不影響配對到的子句
    assert semantic.negation_problem("乙隊晉級決賽", "甲隊未能晉級，乙隊晉級決賽") is None


# ---------- A2 數字綁單位 / B1 正規化 ----------
def test_a2_unit_swap_rejected():
    assert rules.number_problem("活動補助額度為5億元", "活動補助額度為5萬元")
    assert rules.number_problem("報名人數為5萬人", "報名人數為5萬元")
    assert rules.number_problem("共有三千人到場", "共有三千元")
    assert rules.number_problem("活動補助額度為5萬元", "活動補助額度為5萬元") is None


def test_b1_chinese_arabic_and_decimal_equivalence():
    assert rules.number_problem("參觀費用為3000元", "參觀費用為三千元") is None
    assert rules.number_problem("參觀費用為三千元", "參觀費用為3000元") is None
    assert rules.number_problem("參觀費用為3,000元", "參觀費用為三千元") is None
    assert rules.number_problem("補助為5萬元", "補助為五萬元") is None
    assert rules.number_problem("參觀費用為10.0元", "參觀費用為10元") is None
    assert rules.number_problem("參觀費用為10元", "參觀費用為10.0元") is None
    # 小數點與正負號語意仍保留
    assert rules.number_problem("成長3.5%", "成長35%")
    assert rules.number_problem("參觀費用為四千元", "參觀費用為3000元")


# ---------- A3 字元換位 ----------
def test_a3_transposition_rejected():
    assert semantic.transposition_problem("林明志負責活動導覽", "林志明負責活動導覽")
    assert semantic.transposition_problem("林志明負責活動導覽", "林志明負責活動導覽") is None
    # 不做編輯距離：換字（非換位）不在此規則範圍
    assert semantic.transposition_problem("林志豪負責活動導覽", "林志明負責活動導覽") is None


# ---------- B 放寬誤殺 ----------
def test_b2_exclude_possibility_is_not_hedge():
    m, s = CFG["attribution"]["source_markers"], CFG["attribution"]["sentence_markers"]
    assert rules.attribution_problem("展覽確定如期舉辦", "主辦單位排除延期可能，展覽確定如期舉辦", m, s) is None
    assert rules.attribution_problem("展覽確定如期舉辦", "主辦單位不排除延期可能，展覽確定如期舉辦", m, s)
    assert rules.attribution_problem("展覽確定如期舉辦", "展覽可能如期舉辦", m, s)


def test_b3_time_word_boundary():
    w = CFG["time"]["banned_relative_words"]
    assert rules.time_word_hit(["圖樣出現在海報上"], w) is None
    assert rules.time_word_hit(["球員表現在場上"], w) is None
    assert rules.time_word_hit(["現在開放報名"], w) == "現在"
    assert rules.time_word_hit(["出現在海報上，現在開放"], w) == "現在"


def test_b5_opinion_overlap_only_4plus_or_proper_noun():
    banned = CFG["opinion"]["banned_words"]
    facts = "文化展開放參觀\n評審表示作品值得掌聲"
    assert rules.opinion_problem(OPINION, [], banned, facts) is None  # 慣用語「值得掌聲」不算
    # 3 字一般詞重疊不擋
    assert rules.opinion_problem(MARK + "希望大家都能抽出時間，看看這些用心的作品。", [], banned,
                                 "展覽公布參觀安排\n用心製作的作品共有三十件") is None
    # 2026-11-09 放寬：4 字重疊不擋，6 字以上才擋
    assert rules.opinion_problem(MARK + "這份熱忱令人感動", [], banned, "標題\n負責人的熱忱令人敬佩") is None  # 4 字
    assert rules.opinion_problem(MARK + "這份熱忱令人敬佩不已", [], banned, "標題\n負責人的熱忱令人敬佩") is not None  # 6 字
    assert rules.opinion_problem(MARK + "能站上大聯盟的舞台不容易", [], banned, "曾峻岳遞出入札申請\n向大聯盟遞出申請") is not None


def test_b_end_to_end_five_strict_cases():
    def ev(sentence, evidence, prefix=""):
        draft = {"title": "文化展開放參觀", "category": "news", "hashtags": ["#文化"],
                 "body": sentence + "。\n展場設於文化中心。\n入場採用預約方式。\n\n" + OPINION}
        text = "\n".join(["文化展開放參觀，展出內容涵蓋不同類型的創作。", prefix + evidence + "。",
                          "展場設於文化中心。", "入場採用預約方式。"])
        srcs = [{"id": "S1", "published": "2026-11-08T09:00:00+08:00", "text": text}]
        req = review.factual_sentences(draft)
        evs = ["文化展開放參觀", evidence, "展場設於文化中心", "入場採用預約方式"]
        claims = [{"sentence_id": f"C{i + 1}", "source_id": "S1", "evidence": e, "supported": True} for i, e in enumerate(evs)]
        res = {"pass": True, "sensitive_category": "none", "claims": claims}
        res.update({k: [] for k in review.LIST_FIELDS})
        return review.evaluate(res, srcs, draft["title"] + "\n" + draft["body"], req, draft, CFG, "文化展")

    assert ev("參觀費用為3000元", "參觀費用為三千元")[0]
    assert ev("參觀費用為10.0元", "參觀費用為10元")[0]
    assert ev("展覽確定如期舉辦", "展覽確定如期舉辦", "主辦單位排除延期可能，")[0]
    assert ev("圖樣出現在海報上", "海報採用展覽圖樣，圖樣出現在海報上")[0]
    # 相反方向仍擋
    assert not ev("參觀費用為3000元", "參觀費用為三百元")[0]
    assert not ev("展覽確定如期舉辦", "展覽可能如期舉辦")[0]


# ---------- C 雙審 ----------
SRC = [{"id": "S1", "published": "2026-11-08T09:00:00+08:00", "url": URL, "ok": True, "source": "自由時報", "title": "曾峻岳遞出入札申請",
        "text": "富邦悍將終結者曾峻岳已在8日向大聯盟遞出入札申請，交涉期為45天。\n球團表示會全力支持他挑戰旅外。"}]
DRAFT = {"title": "曾峻岳遞出入札申請", "hashtags": ["#棒球"], "category": "news",
         "body": "曾峻岳在 11 月 8 日向大聯盟遞出入札申請。交涉期為 45 天。球團表示會全力支持他挑戰旅外。\n\n" + OPINION}
CAPTION = DRAFT["title"] + "\n\n" + DRAFT["body"]


def _good_result():
    ev = "曾峻岳已在8日向大聯盟遞出入札申請"
    claims = [{"sentence_id": "C1", "source_id": "S1", "evidence": ev, "supported": True},
              {"sentence_id": "C2", "source_id": "S1", "evidence": ev, "supported": True},
              {"sentence_id": "C3", "source_id": "S1", "evidence": "交涉期為45天", "supported": True},
              {"sentence_id": "C4", "source_id": "S1", "evidence": "球團表示會全力支持他挑戰旅外", "supported": True}]
    res = {"pass": True, "sensitive_category": "none", "claims": claims}
    res.update({k: [] for k in review.LIST_FIELDS})
    return res


def _fake_runner(codex, claude):
    """codex / claude：dict 結果、'timeout'、或 'garbage'。"""
    def fake(args, timeout, input_text="", cwd=None):
        if "-p" in args:  # claude
            assert "--tools" in args and args[args.index("--tools") + 1] == ""
            assert "--safe-mode" in args  # 不載入使用者全域 CLAUDE.md/skills/MCP（省額度）
            assert args[args.index("--model") + 1] == CFG["review"]["claude_model"]
            assert "曾峻岳" in input_text and "<<<SOURCES_JSON>>>" in input_text
            if claude == "timeout":
                return -9, "", "timeout after 300s"
            if claude == "garbage":
                return 0, "not json", ""
            return 0, json.dumps({"type": "result", "is_error": False, "result": json.dumps(claude, ensure_ascii=False)}), ""
        out = args[args.index("-o") + 1]
        if codex == "timeout":
            return -9, "", "timeout"
        open(out, "w", encoding="utf-8").write("not json" if codex == "garbage" else json.dumps(codex, ensure_ascii=False))
        return 0, "", ""
    return fake


def _run(monkeypatch, tmp_path, codex, claude):
    monkeypatch.setattr(review.shutil, "which", lambda n: "/x/" + n)
    monkeypatch.setattr(review, "run_cmd", _fake_runner(codex, claude))
    return review.review(tmp_path, DRAFT, {"keyword": "曾峻岳", "sources": SRC}, CAPTION, CFG, "2026-11-08 12:00")


def test_c_both_pass_is_pass(monkeypatch, tmp_path):
    ok, why, res = _run(monkeypatch, tmp_path, _good_result(), _good_result())
    assert ok, why
    assert res["_cited_urls"] == [URL]
    saved = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    assert saved["reviewers"]["codex"]["ok"] and saved["reviewers"]["claude"]["ok"]
    assert "seconds" in saved["reviewers"]["claude"]


# 2026-11-09 使用者裁示：任一位審查員通過（claim 已過確定性逐字驗證）即算通過；兩位都失敗才擋
def test_c_codex_pass_claude_fail_is_pass(monkeypatch, tmp_path):
    bad = _good_result()
    bad["pass"] = False
    bad["unsupported_claims"] = ["球團表示會全力支持他挑戰旅外"]
    ok, why, res = _run(monkeypatch, tmp_path, _good_result(), bad)
    assert ok and "Codex" in why, why
    assert res["_cited_urls"] == [URL]
    saved = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    assert saved["reviewers"]["codex"]["ok"] and not saved["reviewers"]["claude"]["ok"]


def test_c_both_fail_is_fail_with_feedback(monkeypatch, tmp_path):
    bad = _good_result()
    bad["pass"] = False
    bad["unsupported_claims"] = ["球團表示會全力支持他挑戰旅外"]
    ok, why, res = _run(monkeypatch, tmp_path, bad, "garbage")
    assert not ok and "Claude" in why and "Codex" in why
    assert res["unsupported_claims"] == ["球團表示會全力支持他挑戰旅外"]  # 退稿回饋仍拿得到


def test_c_claim_fails_deterministic_check_not_counted_as_pass(monkeypatch, tmp_path):
    bad = _good_result()
    bad["claims"][2]["evidence"] = "球團表示會全力支持他挑戰旅外"  # 與 C3「交涉期為 45 天」不符
    ok, why, _ = _run(monkeypatch, tmp_path, bad, bad)
    assert not ok and "Claude" in why and "Codex" in why
    ok, why, _ = _run(monkeypatch, tmp_path, _good_result(), bad)
    assert ok and "Codex" in why


def test_c_one_reviewer_timeout_or_garbage_other_pass(monkeypatch, tmp_path):
    for mode in ("timeout", "garbage"):
        assert _run(monkeypatch, tmp_path, _good_result(), mode)[0], mode
        assert _run(monkeypatch, tmp_path, mode, _good_result())[0], mode
        ok, why, _ = _run(monkeypatch, tmp_path, mode, mode)
        assert not ok, mode


def test_c_missing_claude_binary_codex_still_decides(monkeypatch, tmp_path):
    monkeypatch.setattr(review.shutil, "which", lambda n: None if n == "claude" else "/x/" + n)
    monkeypatch.setattr(review, "run_cmd", _fake_runner(_good_result(), _good_result()))
    ok, why, _ = review.review(tmp_path, DRAFT, {"keyword": "曾峻岳", "sources": SRC}, CAPTION, CFG, "")
    assert ok and "Codex" in why
    monkeypatch.setattr(review.shutil, "which", lambda n: None)
    ok, why, _ = review.review(tmp_path, DRAFT, {"keyword": "曾峻岳", "sources": SRC}, CAPTION, CFG, "")
    assert not ok and "找不到 claude" in why


def test_c_claude_review_config_and_budget():
    assert CFG["review"]["claude_model"] == "sonnet"  # 2026-11-09 省額度：opus → sonnet
    assert CFG["timeouts"]["claude_review"] > 0
