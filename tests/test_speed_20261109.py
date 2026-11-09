"""2026-11-09 使用者裁示「規則太嚴、太慢」：取消 AI 審稿、重寫只給一次、claude 連續失敗就停。"""
import copy

import run_daily
from tests.test_pipeline import _patch_env, _topics
from tests.test_round5 import CFG, TERMS, _setup_topic


def test_safe_env_drops_anthropic_api_key():
    from src.common import safe_env
    env = safe_env({"ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_BASE_URL": "u", "CLAUDE_FORCE_OAUTH": "1",
                    "PATH": "p", "FB_PAGE_TOKEN": "t"})
    assert env == {"ANTHROPIC_BASE_URL": "u", "CLAUDE_FORCE_OAUTH": "1", "PATH": "p"}


def test_claude_error_reason():
    import json
    from src.common import claude_error
    assert claude_error(json.dumps({"is_error": True, "result": "Credit balance is too low"})) == \
        "Credit balance is too low"
    assert claude_error(json.dumps({"is_error": False, "result": "ok"})) == ""
    assert claude_error("not json") == ""


def test_missing_hashtags_or_category_not_fatal(monkeypatch):
    import json
    from src import write
    body = "正文" * 120
    inner = json.dumps({"title": "標題", "body": body}, ensure_ascii=False)
    monkeypatch.setattr(write.shutil, "which", lambda n: "/x/claude")
    monkeypatch.setattr(write, "run_cmd", lambda *a, **k: (0, json.dumps({"result": inner}), ""))
    topic = {"keyword": "k", "sources": [{"ok": True, "text": "t", "title": "t", "source": "s"}]}
    draft, err = write.write_draft(topic, CFG)
    assert err is None and draft["hashtags"] == [] and draft["category"] == "news"


def test_config_values():
    assert CFG["review"]["ai_review"] is False
    assert CFG["run"]["max_rewrites"] == 1
    assert CFG["run"]["max_claude_failures"] == 3
    assert CFG["select"]["max_candidates_fetch"] == 10  # 每輪最多處理 10 個候選


def _capture_posts(monkeypatch, payloads):
    def post(img, payload, cfg=None):
        payloads.append(dict(payload))
        return str(1000 + len(payloads))
    monkeypatch.setattr(run_daily.publish, "post_photo", post)


def test_publish_now_posts_immediately(tmp_path, monkeypatch):
    from tests.test_pipeline import _fake_process
    assert CFG["schedule"]["publish_now"] is True
    con = _patch_env(monkeypatch, tmp_path, _topics(3))
    monkeypatch.setattr(run_daily, "process_topic", _fake_process(tmp_path, []))
    payloads = []
    _capture_posts(monkeypatch, payloads)
    assert run_daily.main(["--live", "--max", "3"]) == 0
    assert len(payloads) == 3
    assert all(p["published"] == "true" and "scheduled_publish_time" not in p for p in payloads)
    slots = [r[0] for r in con.execute("SELECT slot FROM posts WHERE status='scheduled'")]
    assert len(set(slots)) == 3  # 同一秒發多篇也不撞唯一鍵


def test_publish_now_off_keeps_scheduling(tmp_path, monkeypatch):
    import copy
    from tests.test_pipeline import _fake_process
    cfg2 = copy.deepcopy(CFG)
    cfg2["schedule"]["publish_now"] = False
    monkeypatch.setattr(run_daily, "load_config", lambda *a, **k: cfg2)
    _patch_env(monkeypatch, tmp_path, _topics(1))
    monkeypatch.setattr(run_daily, "process_topic", _fake_process(tmp_path, []))
    payloads = []
    _capture_posts(monkeypatch, payloads)
    assert run_daily.main(["--live", "--max", "1"]) == 0
    assert payloads[0]["published"] == "false" and "scheduled_publish_time" in payloads[0]


def test_stops_after_max_candidates(tmp_path, monkeypatch):
    import copy
    cfg2 = copy.deepcopy(CFG)
    cfg2["select"]["max_candidates_fetch"] = 3
    monkeypatch.setattr(run_daily, "load_config", lambda *a, **k: cfg2)
    _patch_env(monkeypatch, tmp_path, _topics(6))
    calls = []

    def fail(topic, *a, **k):
        calls.append(topic["keyword"])
        return None, None, "", [], "來源關：測試"
    monkeypatch.setattr(run_daily, "process_topic", fail)
    assert run_daily.main(["--live", "--max", "1"]) == 0
    assert len(calls) == 3


def test_ai_review_off_skips_review_and_cites_all_ok_sources(tmp_path, monkeypatch):
    calls = []
    topic, slot = _setup_topic(monkeypatch, 1, calls)
    monkeypatch.setattr(run_daily.review, "review",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("AI 審稿關閉時不應呼叫")))
    _d, draft, _cap, cited, why = run_daily.process_topic(topic, CFG, TERMS, tmp_path, slot)
    assert draft is not None, why
    assert cited == [s["url"] for s in topic["sources"] if s.get("ok")]


def test_ai_review_on_still_calls_review(tmp_path, monkeypatch):
    cfg2 = copy.deepcopy(CFG)
    cfg2["review"]["ai_review"] = True
    calls, reviewed = [], []
    topic, slot = _setup_topic(monkeypatch, 1, calls)
    urls = [s["url"] for s in topic["sources"]]
    monkeypatch.setattr(run_daily.review, "review",
                        lambda *a, **k: reviewed.append(1) or (True, "pass", {"_cited_urls": urls[:1]}))
    _d, draft, _cap, cited, why = run_daily.process_topic(topic, cfg2, TERMS, tmp_path, slot)
    assert reviewed == [1] and draft is not None and cited == urls[:1], why


def test_only_one_rewrite(tmp_path, monkeypatch):
    calls, writes = [], []
    topic, slot = _setup_topic(monkeypatch, 1, calls)
    real_write = run_daily.write.write_draft
    monkeypatch.setattr(run_daily.write, "write_draft", lambda *a, **k: writes.append(1) or real_write(*a, **k))
    monkeypatch.setattr(run_daily.gates, "draft_gate", lambda *a, **k: (False, "原創關：測試", ""))
    _d, draft, _cap, _cited, why = run_daily.process_topic(topic, CFG, TERMS, tmp_path, slot)
    assert draft is None and len(writes) == 2 and "已重寫 1 次" in why


def test_stops_after_three_claude_failures(tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path, _topics(6))
    calls = []

    def fail(topic, *a, **k):
        calls.append(topic["keyword"])
        return None, None, "", [], "改寫失敗：claude 失敗 exit=1 "
    monkeypatch.setattr(run_daily, "process_topic", fail)
    assert run_daily.main(["--live", "--max", "1"]) == 0
    assert len(calls) == 3
    assert "claude 已失敗 3 次" in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_other_failures_do_not_stop_run(tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path, _topics(6))
    calls = []

    def fail(topic, *a, **k):
        calls.append(topic["keyword"])
        return None, None, "", [], "來源關：測試"
    monkeypatch.setattr(run_daily, "process_topic", fail)
    assert run_daily.main(["--live", "--max", "1"]) == 0
    assert len(calls) == 6
