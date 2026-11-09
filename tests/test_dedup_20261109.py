"""2026-11-09 使用者回報黃仁勳婚外情一天發三篇：最近已發的同一事件要擋下（標題相似＋寫稿模型判斷）。"""
import json
from datetime import datetime, timedelta

import run_daily
from src import db, rank, write
from src.common import TZ, load_config

CFG = load_config()
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=TZ)
POSTED = [  # 今天實際發出的三篇（話題詞, 貼文標題）
    ("我的所作所為沒有藉口！　黃仁勳「給員工信內容曝光」：我辜負了家人…盼家庭能癒合", "黃仁勳發信給員工認錯"),
    ("黃仁勳認婚外情背叛妻兒！　馬斯克「1句話」表態：他不可替代是偉大企業家", "馬斯克力挺黃仁勳"),
]


def _post(con, tmp_path, kw, title, slot, url):
    d = tmp_path / f"p{abs(hash(kw))}"
    d.mkdir()
    (d / "draft.json").write_text(json.dumps({"title": title}, ensure_ascii=False), encoding="utf-8")
    pid = db.reserve(con, kw, kw, [url], slot, "live", str(d))
    db.mark(con, pid, "scheduled", "1")


def _t(kw, url):
    return {"keyword": kw, "traffic": 0, "origin": "rss", "pub": "",
            "sources": [{"title": kw, "url": url, "source": "x", "rss_published": NOW.isoformat()}]}


def test_config():
    assert CFG["rank"]["recent_post_hours"] == 48


def test_recent_titles_window_and_draft_title(tmp_path):
    con = db.connect(":memory:")
    _post(con, tmp_path, POSTED[0][0], POSTED[0][1], "2026-11-09T09:45:21", "https://a/1")
    _post(con, tmp_path, "三天前的舊聞", "舊標題", "2026-11-06T09:00", "https://a/2")
    titles = rank.recent_titles(con, NOW, 48)
    assert titles == [f"{POSTED[0][1]}（{POSTED[0][0]}）"]  # 48 小時外的不列


def test_rank_skips_similar_title_to_recent_post(tmp_path):
    con = db.connect(":memory:")
    _post(con, tmp_path, "黃仁勳承認婚外情 輝達董事會將展開調查", "黃仁勳承認婚外情", "2026-11-09T09:00", "https://a/1")
    same = _t("黃仁勳承認婚外情！輝達董事會將展開調查", "https://www.mirrormedia.mg/story/9")
    other = _t("颱風路徑北修明起風雨增", "https://www.cna.com.tw/news/ahel/1.aspx")
    out, skipped = rank.rank([same, other], con, CFG, NOW)
    assert [t["keyword"] for t in out] == [other["keyword"]]
    assert "最近已發" in skipped[0]["reason"]


def test_rank_keeps_old_post_outside_window(tmp_path):
    con = db.connect(":memory:")
    _post(con, tmp_path, "黃仁勳承認婚外情 輝達董事會將展開調查", "t", "2026-11-05T09:00", "https://a/1")
    out, _ = rank.rank([_t("黃仁勳承認婚外情！輝達董事會將展開調查", "https://x/9")], con, CFG, NOW)
    assert len(out) == 1


def test_prompt_lists_recent_posts_only_when_present():
    topic = {"keyword": "k", "sources": [{"ok": True, "text": "t", "title": "t", "source": "s"}]}
    assert "重複檢查" not in write.build_prompt(topic, "style")
    topic["recent_posts"] = ["馬斯克力挺黃仁勳（黃仁勳認婚外情）"]
    p = write.build_prompt(topic, "style")
    assert "重複檢查" in p and "- 馬斯克力挺黃仁勳（黃仁勳認婚外情）" in p
    assert p.index("重複檢查") < p.index("【來源資料")


def test_write_draft_duplicate_returns_error(monkeypatch):
    monkeypatch.setattr(write.shutil, "which", lambda n: "/x/claude")
    inner = json.dumps({"duplicate_of": "馬斯克力挺黃仁勳"}, ensure_ascii=False)
    monkeypatch.setattr(write, "run_cmd", lambda *a, **k: (0, json.dumps({"result": inner}), ""))
    topic = {"keyword": "k", "recent_posts": ["馬斯克力挺黃仁勳"],
             "sources": [{"ok": True, "text": "t", "title": "t", "source": "s"}]}
    draft, err = write.write_draft(topic, CFG)
    assert draft is None and err.startswith("同一事件已發過") and "claude 失敗" not in err


def test_main_passes_recent_posts_to_topic(tmp_path, monkeypatch):
    from tests.test_pipeline import _patch_env, _topics
    con = _patch_env(monkeypatch, tmp_path, _topics(1))
    _post(con, tmp_path, POSTED[1][0], POSTED[1][1], datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S"), "https://a/1")
    seen = []

    def fake(topic, cfg, terms, root, slot):
        seen.append(topic.get("recent_posts"))
        return None, None, "", [], "改寫失敗：同一事件已發過：「x」"
    monkeypatch.setattr(run_daily, "process_topic", fake)
    assert run_daily.main(["--live", "--max", "1"]) == 0
    assert seen and f"{POSTED[1][1]}（{POSTED[1][0]}）" in seen[0]
