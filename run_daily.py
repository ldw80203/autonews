"""主流程：收集 -> 選題 -> 改寫 -> 稿件/原創關 -> 事實關 -> 配圖 -> 發佈（或 dry-run 預覽）。

  python run_daily.py --dry-run --max 2     # 預設，不打 FB API
  python run_daily.py --live                 # 排程發佈
任何一關沒過就丟棄、不發文。STOP 檔存在時直接退出（碼 0）；執行途中出現 STOP 也會停止。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from src import collect, db, gates, image, imagespec, photo, publish, rank, review, schedule, write
from src import select as sel
from src.common import ROOT, TZ, as_taipei, cjk_ratio, load_config, redact, slugify, write_json

STOP_FILE = ROOT / "STOP"
LOG_FILE = ROOT / "logs" / "run.log"


class StopRequested(Exception):
    pass


class DeadlineReached(Exception):
    """剩餘時間（已扣收尾預留）不足以跑完下一個步驟的最壞耗時。"""


_DEADLINE: float | None = None  # time.monotonic() 基準，已扣掉 reserve_minutes；main() 設定


def ensure_budget(seconds: float, step: str) -> None:
    """每個耗時步驟開始前呼叫：剩餘時間 < 該步驟最壞耗時就不開始（含 10 分鐘收尾預留）。"""
    if _DEADLINE is not None and time.monotonic() + seconds > _DEADLINE:
        raise DeadlineReached(f"剩餘時間不足以完成「{step}」（需 {int(seconds)} 秒，已預留收尾時間）")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {redact(msg)}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line)
    except (UnicodeEncodeError, OSError):
        pass


def stop_requested(stop_file: Path | None = None) -> bool:
    return (stop_file or STOP_FILE).exists()


def check_stop() -> None:
    if stop_requested():
        raise StopRequested()


def is_topic_defining_source(topic: dict, s: dict) -> bool:
    """RSS 來源的話題：關鍵詞就是該文標題，那篇文章本身就是定義話題的來源，不能拿標題全文去比對它自己的內文。
    （Trends 話題掛進來的其他來源仍要過相關性比對。）"""
    return topic.get("origin") == "rss" and (s.get("title") or "").strip() == (topic.get("keyword") or "").strip()


def fetch_sources(topic: dict, cfg: dict, now: datetime | None = None, limit: int = 4) -> None:
    """抓內文與發佈時間並標記 ok。不合格（ok=False，不計入來源數、不餵寫稿）：
    網域認不出、沒內文、太短、非中文、抓不到發佈時間、發佈時間異常（晚於現在太多）、內文與話題關鍵詞無關。"""
    now = as_taipei(now or datetime.now(TZ))
    min_chars = cfg["select"]["min_text_chars"]
    min_ratio = cfg["language"]["min_cjk_ratio_body"]
    max_future = timedelta(hours=cfg["freshness"]["max_future_hours"])
    for i, s in enumerate(topic["sources"]):
        s.setdefault("published", None)
        if i >= limit:
            s["ok"], s["text"], s["error"] = False, "", "skipped"
            continue
        if sel.domain_of(s["url"]) is None:
            s["ok"], s["text"], s["error"] = False, "", "unrecognized_domain"
            continue
        art = collect.fetch_article(s["url"], cfg["timeouts"]["http"])
        text = art["text"]
        s["ok"], s["text"], s["error"] = bool(text), text or "", art["error"]
        s["final_url"] = art.get("final_url") or s["url"]
        s["image_url"] = art.get("image_url")
        # 時效用時間一律 strict：沒有明確時區（naive、未知縮寫、-0000）視同沒有發佈時間
        pub = art["published"] or collect.parse_date_tolerant(s.get("rss_published"), strict=True)
        if pub is not None and (pub.tzinfo is None or pub.utcoffset() is None):
            pub = None
        if pub is not None:
            s["published"] = pub.isoformat()
            s["published_str"] = as_taipei(pub).strftime("%Y-%m-%d %H:%M")
        if not s["ok"]:
            continue
        if len(s["text"]) < min_chars:
            s["ok"], s["error"] = False, "text_too_short"
        elif cjk_ratio(s["text"]) < min_ratio:
            s["ok"], s["error"] = False, "non_chinese"
        elif pub is None:
            s["ok"], s["error"] = False, "no_published_time"
        elif as_taipei(pub) - now > max_future:
            s["ok"], s["error"] = False, "bad_published_time"
        elif not is_topic_defining_source(topic, s) and not sel.is_relevant(s["text"], topic["keyword"], cfg["relevance"], s.get("title", "")):
            s["ok"], s["error"] = False, "irrelevant_to_topic"


def max_age_hours(topic: dict, cfg: dict) -> float | None:
    """時效上限；[freshness] unlimited_categories（奇聞）回 None = 不檢查多舊；
    [rank] max_age_hours_by_category 有列的類別（人間故事、國際）用該上限，其餘用 max_age_hours。"""
    cat = rank.topic_category(topic)
    if cat in cfg["freshness"].get("unlimited_categories", []):
        return None
    return cfg.get("rank", {}).get("max_age_hours_by_category", {}).get(cat, cfg["freshness"]["max_age_hours"])


def source_age_problem(s: dict, slot: datetime, cfg: dict, max_age: float | None = -1) -> str | None:
    """單一來源時效：沒有時間或沒有時區 -> 拒絕；發佈時間晚於預定發佈（負年齡）-> 拒絕；超過 max_age -> 拒絕。
    max_age 預設 -1 = 用 config max_age_hours；None = 不限（仍要有時間、不可負年齡）。"""
    if max_age == -1:
        max_age = cfg["freshness"]["max_age_hours"]
    raw = s.get("published")
    if not raw:
        return "no_published_time"
    try:
        pub = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return "bad_published_time"
    if pub.tzinfo is None or pub.utcoffset() is None:
        return "unknown_timezone"
    age = as_taipei(slot) - as_taipei(pub)
    if age < timedelta(0):
        return "negative_age"
    if max_age is not None and age > timedelta(hours=max_age):
        return "stale"
    return None


def filter_stale_sources(topic: dict, slot: datetime, cfg: dict) -> None:
    """每篇合格來源各自檢查時效，不合格者剔除（ok=False），之後才重算來源數、重新分類。"""
    lim = max_age_hours(topic, cfg)
    for s in topic["sources"]:
        if s.get("ok"):
            why = source_age_problem(s, slot, cfg, lim)
            if why:
                s["ok"], s["error"] = False, why


def filter_untrusted_media(topic: dict, publishers: dict) -> None:
    """可信媒體：轉址後的實際網址網域必須在 config [media.publishers]；否則剔除。"""
    for s in topic["sources"]:
        if s.get("ok") and not sel.trusted_media(s.get("final_url") or s.get("url", ""), publishers):
            s["ok"], s["error"] = False, "untrusted_media"


def freshness_problem(topic: dict, slot: datetime, cfg: dict) -> str | None:
    """每一篇合格來源都必須通過時效（不是只看最新一篇）：無時區、負年齡、超過 max_age_hours 都不合格。"""
    oks = [s for s in topic["sources"] if s.get("ok")]
    if not oks:
        return "時效關：沒有可用的來源發佈時間"
    lim = max_age_hours(topic, cfg)
    for s in oks:
        why = source_age_problem(s, slot, cfg, lim)
        if why:
            return (f"時效關：來源 {sel.domain_of(s.get('url', '')) or s.get('url', '')} 發佈 {s.get('published')}，"
                    f"預定發佈 {as_taipei(slot):%m-%d %H:%M}：{why}（上限 {lim if lim is not None else '不限'} 小時）")
    return None


def cited_source_problem(topic: dict, cited: list[str], slot: datetime, cfg: dict) -> str | None:
    """被引用的來源：每一篇都必須是合格且未過時的來源；時事需 >= 2 個不同出版者，網友熱議 >= 1。"""
    pubs = cfg["media"]["publishers"]
    by_url = {s["url"]: s for s in topic["sources"] if s.get("ok")}
    names = set()
    for u in cited:
        s = by_url.get(u)
        if s is None:
            return f"引用關：被引用的來源不是合格來源 {u}"
        why = source_age_problem(s, slot, cfg, max_age_hours(topic, cfg))
        if why:
            return f"引用關：被引用的來源時效不合格 {u}：{why}"
        names.add(sel.publisher_of(s.get("final_url") or u, pubs))
    names.discard(None)
    news = topic.get("category") == "news"
    need = cfg["select"].get("min_news_publishers", 2) if news else 1
    if len(names) < need:
        return f"引用關：{'時事' if news else '網友熱議'}需被引用的出版者 ≥{need}，實際 {len(names)}（{'、'.join(sorted(names))}）"
    return None


def process_topic(topic: dict, cfg: dict, terms: list[str], post_root: Path, slot: datetime):
    """跑完所有閘門與生成。回 (post_dir, draft, caption, cited_urls, 丟棄原因)；成功時原因為空字串。
    每個耗時步驟前都檢查 STOP 與剩餘時間預算。"""
    check_stop()
    pubs = cfg["media"]["publishers"]
    # 可信媒體排前面，避免不可信來源佔掉抓取名額（只排序，真正剔除以轉址後網址為準）
    topic["sources"].sort(key=lambda s: not sel.trusted_media(s.get("url", ""), pubs))
    ensure_budget(cfg["timeouts"]["http"] * min(4, len(topic["sources"])), "抓來源內文")
    fetch_sources(topic, cfg)
    filter_untrusted_media(topic, pubs)
    filter_stale_sources(topic, slot, cfg)
    topic["category"] = sel.classify(topic, ok_only=True)  # 只用合格來源重新分類
    oks = [s for s in topic["sources"] if s["ok"]]
    texts = [s["text"] for s in oks]
    hit = sel.blocklist_hit(texts + [s.get("title") or "" for s in oks], terms)
    if hit:
        return None, None, "", [], f"黑名單命中（內文）「{hit}」"
    ok, why = sel.source_gate(topic, topic["category"], pubs, cfg["select"].get("min_news_publishers", 2))
    if not ok:
        return None, None, "", [], f"{why}［{source_error_summary(topic)}］"
    why = freshness_problem(topic, slot, cfg)
    if why:
        return None, None, "", [], why

    post_dir = post_root / slugify(topic["keyword"])
    post_dir.mkdir(parents=True, exist_ok=True)
    write_json(post_dir / "source.json", topic)
    urls = [s["url"] for s in oks]
    ptime = as_taipei(slot).strftime("%Y-%m-%d %H:%M")
    tag_texts = [topic["keyword"]] + [s.get("title") or "" for s in oks]
    img_prompt = image.scene_prompt(image.scene_for(topic))  # 固定模板，不含任何來源/模型文字

    # 2026-11-10：配圖來源 original = 來源頁 og:image 原圖。先下載（caption 的 AI 聲明取決於 image_kind，
    # 必須在稿件閘門前定案；沒原圖又不退回 AI 就不必浪費寫稿額度）。圖片與網址不會傳給 codex / claude。
    image_kind = "ai"
    if cfg["image"].get("source", "ai") == "original":
        fails: list[str] = []
        got = False
        for purl in photo.pick_photos(topic["sources"])[:cfg["image"].get("max_photo_tries", 4)]:  # 依序試，第一個成功的就用
            ensure_budget(2 * cfg["timeouts"]["http"], "下載原圖")  # 每次下載的總時限是 2 倍 http timeout
            got, w1 = photo.download_photo(purl, post_dir / "image.png", cfg)
            if got:
                break
            fails.append(f"{sel.domain_of(purl) or purl[:40]}：{w1}")
        pwhy = ("；".join(fails) if fails else "來源沒有 og:image")[:200]
        if got:
            image_kind = "original"
        elif not cfg["image"].get("fallback_ai", False):
            return None, None, "", [], f"無可用原圖：{pwhy}"
        else:
            log(f"無可用原圖（{pwhy}），退回 AI 生圖")

    feedback, draft, caption, cited = None, None, "", []
    attempts = 1 + cfg["run"].get("max_rewrites", 1)
    for attempt in range(1, attempts + 1):  # 被退回時帶著原因重寫；每一版稿件都完整重跑所有閘門
        check_stop()
        ensure_budget(cfg["timeouts"]["claude"], "寫稿")
        draft, err = write.write_draft(topic, cfg, feedback, ptime)
        if err:
            return None, None, "", [], f"改寫失敗：{err}"
        draft["hashtags"] = gates.sanitize_hashtags(draft["hashtags"], tag_texts, cfg["hashtags"]["whitelist"])
        draft["image_prompt"] = img_prompt
        draft["image_kind"] = image_kind
        if not isinstance(draft.get("title_highlight"), str):
            draft.pop("title_highlight", None)
        draft["category"] = topic["category"]
        write_json(post_dir / "draft.json", draft)
        ok, why, caption = gates.draft_gate(draft, urls, texts, cfg, terms, tag_texts, topic["keyword"])
        if ok:
            check_stop()
            if cfg["review"].get("ai_review", True):
                ensure_budget(max(cfg["timeouts"]["codex_review"], cfg["timeouts"].get("claude_review", 0)), "事實關")  # 雙審同時跑，取較大者
                img_path, img_bytes = post_dir / "image.png", None
                try:
                    if image_kind == "original":  # 原圖不准留在 codex 看得到的任何磁碟位置：審稿期間只存在記憶體
                        img_bytes = img_path.read_bytes()
                        img_path.unlink()
                    ok, why, raw = review.review(post_dir, draft, topic, caption, cfg, ptime)
                finally:
                    if img_bytes is not None:
                        img_path.write_bytes(img_bytes)
                        if _sha256(img_path) != hashlib.sha256(img_bytes).hexdigest():
                            raise RuntimeError("審稿後還原原圖的雜湊不符")
            else:  # 2026-11-09 使用者裁示：取消 AI 審稿，只留確定性閘門；來源行列出全部合格來源
                ok, why, raw = True, "AI 審稿已關閉", {"_cited_urls": urls}
            if ok:
                cited = raw["_cited_urls"]
                why = cited_source_problem(topic, cited, slot, cfg)
                if why:
                    return None, None, "", [], why
                # 最終 caption（來源行只列被引用來源）再完整重跑一次全部稿件閘門
                caption = publish.build_caption(draft, cited)
                ok, why, final_cap = gates.draft_gate(draft, cited, texts, cfg, terms, tag_texts, topic["keyword"])
                if not ok or final_cap != caption:
                    return None, None, "", [], f"最終閘門：{why or 'caption 不一致'}"
                break
            if raw and isinstance(raw.get("unsupported_claims"), list) and raw["unsupported_claims"]:
                feedback = ("以下句子在來源中找不到依據，請刪除或改成來源有寫的說法："
                            + "；".join(str(c) for c in raw["unsupported_claims"][:5]))
            else:
                feedback = why
        else:
            feedback = why + "。請修正（hashtag 只用關鍵詞/來源標題/通用標籤、至少 3 句事實句後才是唯一一段評論、評論不含數字人名專有名詞、不用相對時間詞與易變數據、用不同句式重寫）。"
        if attempt == attempts:
            return None, None, "", [], f"{why}（已重寫 {attempts - 1} 次仍不過）"

    check_stop()
    if not image.is_template_prompt(draft["image_prompt"]):
        return None, None, "", [], "配圖關：image_prompt 不是固定模板"
    content_ok = False
    if image_kind == "original":
        err = image.add_headline(post_dir / "image.png", draft["title"], cfg, highlight=draft.get("title_highlight"))
        if err:
            return None, None, "", [], f"配圖標題失敗：{err}"
        content_ok = True
    elif cfg["image"].get("content_image", True):
        try:  # 依每篇內容配圖（圖中含標題＋重點句）；任何一步不行就退回固定模板
            ensure_budget(2 * cfg["timeouts"]["codex_image"] + 2 * cfg["timeouts"]["claude"], "依內容配圖")
            img, info = imagespec.make_image(post_dir, draft, cfg)
            content_ok = img is not None
            if not content_ok:
                log(f"依內容配圖未成功，改用固定模板：{info.get('spec_error') or info.get('steps')}")
        except DeadlineReached:
            log("剩餘時間不足以依內容配圖，改用固定模板")
    if not content_ok:
        ensure_budget(cfg["timeouts"]["codex_image"], "配圖")
        _img, err = image.generate_image(post_dir, draft["image_prompt"], cfg)
        if err:
            return None, None, "", [], f"配圖失敗：{err}"
        err = image.add_headline(post_dir / "image.png", draft["title"], cfg, highlight=draft.get("title_highlight"))
        if err:
            return None, None, "", [], f"配圖標題失敗：{err}"
    write_json(post_dir / "artifact.json", {"image_sha256": _sha256(post_dir / "image.png"),
                                            "image_kind": image_kind,
                                            "caption_sha256": hashlib.sha256(caption.encode("utf-8")).hexdigest()})
    return post_dir, draft, caption, cited, ""


def write_report(path: Path, mode: str, posted: list[dict], dropped: list[dict], errors: list[str],
                 now: datetime, unresolved: list[str], notes: list[str]):
    L = [f"# autonews 報告 {now.strftime('%Y-%m-%d')}（{mode}，{now.strftime('%H:%M')} 執行）", ""]
    L.append(f"## {'已排程發佈' if mode == 'live' else '預備發佈（dry-run，未打 FB API）'}（{len(posted)}）")
    for p in posted:
        L.append(f"- [{p['slot']}] {p['title']}｜分類 {p['category']}｜流量 {p['traffic']}｜{p['dir']}"
                 + (f"｜FB id {p['fb_id']}" if p.get("fb_id") else ""))
    if unresolved:
        L += ["", f"## 結果不明，需人工對帳（{len(unresolved)}）",
              "以下貼文可能已被 FB 收下排程；請到 Meta Business Suite 確認。對帳前時段與話題保持鎖定（DB status=pending）。"]
        L += [f"- {u}" for u in unresolved]
    L += ["", f"## 丟棄（{len(dropped)}）"]
    for d in dropped:
        L.append(f"- {d['keyword']}：{d['reason']}")
    if errors:
        L += ["", "## 收集錯誤"] + [f"- {e}" for e in errors]
    if notes:
        L += ["", "## 備註"] + [f"- {n}" for n in notes]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def source_error_summary(topic: dict) -> str:
    """每個來源一行原因：網域｜ok 或錯誤碼｜發佈時間。"""
    rows = []
    for s in topic["sources"]:
        rows.append(f"{sel.domain_of(s.get('url', '')) or s.get('url', '')}:"
                    f"{'ok' if s.get('ok') else s.get('error')}@{s.get('published_str') or '無時間'}")
    return "；".join(rows)


def run_select_stage(cands: list[dict], cfg: dict, now: datetime, con, live: bool, root: Path) -> int:
    """診斷：對所有候選抓來源並套用來源關、時效關，列出每個來源的剔除原因。不寫稿、不 review、不生圖。"""
    slot = choose_slot(now, cfg, set(), live) or choose_slot(now, cfg, set(), False)
    L = [f"# select 診斷 {now:%Y-%m-%d %H:%M}（預設 slot {slot:%m-%d %H:%M}，max_age {cfg['freshness']['max_age_hours']}h）", ""]
    for t in cands:
        fetch_sources(t, cfg, now)
        filter_untrusted_media(t, cfg["media"]["publishers"])
        filter_stale_sources(t, slot, cfg)
        t["category"] = sel.classify(t, ok_only=True)
        ok, why = sel.source_gate(t, t["category"], cfg["media"]["publishers"],
                                  cfg["select"].get("min_news_publishers", 2))
        fresh = freshness_problem(t, slot, cfg) if ok else None
        verdict = "通過來源關+時效關" if ok and not fresh else (fresh or why)
        L.append(f"## {t['keyword']}（{t['category']}，流量 {t['traffic']}）→ {verdict}")
        for s in t["sources"]:
            L.append(f"- {s['url']} ｜ {'ok' if s.get('ok') else s.get('error')} ｜ 發佈 {s.get('published_str') or '無'}")
        log(f"select 診斷 {t['keyword']}：{verdict}")
    path = root / "reports" / f"select-{now:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"診斷報告：{path}")
    return 0


def choose_slot(now: datetime, cfg: dict, taken: set[str], live: bool):
    """送出前以『當下時間』重新選時段（>= min_lead）。dry-run 無時段時改用明日早班預覽。
    [schedule] publish_now（2026-11-09 使用者裁示）：live 時段就是現在，產出後立即公開。"""
    if live and cfg["schedule"].get("publish_now"):
        return now.replace(microsecond=0)
    s = schedule.pick_slots(now, cfg, taken, 1)
    if s or live:
        return s[0] if s else None
    tomorrow = (now.replace(hour=0, minute=0) + timedelta(days=1))
    s = schedule.pick_slots(tomorrow, cfg, taken, 1)
    return s[0] if s else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="預設")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--max", type=int, default=4)
    ap.add_argument("--stage", choices=["all", "select"], default="all",
                    help="select：只跑 collect+選題+來源抓取/來源關/時效關，寫診斷報告，不寫稿、不 review、不生圖")
    args = ap.parse_args(argv)
    live = bool(args.live)
    mode = "live" if live else "dry-run"

    if stop_requested():
        log("STOP 檔存在，什麼都不做，退出")
        return 0

    global _DEADLINE
    cfg = load_config()
    started = time.monotonic()
    deadline = started + (cfg["run"]["deadline_minutes"] - cfg["run"].get("reserve_minutes", 10)) * 60
    _DEADLINE = deadline
    terms = sel.load_blocklist()
    con = db.connect()
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    post_root = ROOT / "posts" / today / f"run-{now:%H%M%S}-{os.getpid()}"  # 每次執行獨立目錄，並行不互相覆寫
    posted: list[dict] = []
    dropped: list[dict] = []
    unresolved: list[str] = []
    notes: list[str] = []
    used_slots: set[str] = set()

    topics, errors = collect.collect_topics(cfg)
    log(f"收集 {len(topics)} 個話題，錯誤 {len(errors)}")
    cands, rejected = sel.prefilter(topics, terms, con, cfg)
    dropped += rejected
    rank_cfg = cfg
    try:
        ensure_budget(cfg["timeouts"]["claude"], "LLM 評分")
    except DeadlineReached as e:  # 預算不足：關掉 LLM 評分（退回關鍵詞分數），不中止整輪
        log(f"{e}；本輪排序不用 LLM 評分")
        rank_cfg = dict(cfg, rank=dict(cfg["rank"], llm_judge=False))
    cands, skipped = rank.rank(cands, con, rank_cfg, now)
    dropped += skipped
    log(f"預過濾＋排序後候選 {len(cands)}，丟棄 {len(rejected) + len(skipped)}")
    for t in cands[:5]:
        r = t["rank"]
        log(f"  排序 {r['score']:>5} [{r['cat']}] {t['keyword'][:30]}（熱度 {r['heat']:.2f} 新鮮 {r['fresh']:.2f} "
            f"類別 {r['category']:.2f} 媒體 {r['publisher']:.2f} 好寫 {r['writable']:.2f}）")

    if args.stage == "select":
        return run_select_stage(cands, cfg, now, con, live, ROOT)  # post_root 多了 run- 一層，不可再用 parent.parent

    publish_now = live and bool(cfg["schedule"].get("publish_now"))
    avail = args.max if publish_now else len(schedule.pick_slots(now, cfg, db.taken_slots(con), args.max))
    if avail == 0 and not live:
        avail = args.max
    if avail == 0:
        log("無可用時段（皆已過或已排），不發文")

    examined = 0
    claude_failures = 0
    for topic in cands:
        if len(posted) + len(unresolved) >= min(avail, args.max) or examined >= cfg["select"]["max_candidates_fetch"]:
            break
        if stop_requested():
            notes.append("執行途中偵測到 STOP，已停止")
            log("偵測到 STOP，停止處理後續話題")
            break
        if time.monotonic() > deadline:
            notes.append(f"已達整體截止時間（{cfg['run']['deadline_minutes']} 分鐘扣收尾預留），不再開新話題")
            log("達整體截止時間，停止開新話題")
            break
        examined += 1
        pid = None
        try:
            # 先選預定時段：寫稿 prompt、時效閘門、review 都以它為基準
            slot = choose_slot(datetime.now(TZ), cfg, db.taken_slots(con) | used_slots, live)
            if slot is None:
                dropped.append({"keyword": topic["keyword"], "reason": "已無可用時段（>=15 分鐘）"})
                log("無可用時段，停止")
                break
            # 寫稿模型用來判斷是否和最近已發的是同一起事件（每輪重算，含本輪剛發的）
            topic["recent_posts"] = rank.recent_titles(con, datetime.now(TZ), cfg["rank"].get("recent_post_hours", 48))
            post_dir, draft, caption, cited, why = process_topic(topic, cfg, terms, post_root, slot)
            if not draft:
                dropped.append({"keyword": topic["keyword"], "reason": why})
                log(f"丟棄 {topic['keyword']}：{why}")
                if "claude 失敗" in why:  # 多半是額度用完；不要把候選全部燒光，等下一輪
                    claude_failures += 1
                    if claude_failures >= cfg["run"].get("max_claude_failures", 3):
                        notes.append(f"claude 已失敗 {claude_failures} 次（可能額度用完），停止本輪")
                        log(f"claude 已失敗 {claude_failures} 次，停止本輪")
                        break
                continue
            img_hash = _sha256(post_dir / "image.png")  # 綁定已驗證的產物

            check_stop()
            # 送出前以『當下時間』重新驗證 slot（>= min_lead 且未被占用）；生成耗時可能讓它過期
            if publish_now:  # 立即公開：DB 時段記實際發佈時間（含秒，不佔整點時段；同秒加流水號）
                base = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S")
                slot_k, n = base, 1
                while slot_k in db.taken_slots(con) | used_slots:
                    n += 1
                    slot_k = f"{base}-{n}"
            else:
                slot_k = schedule.slot_key(slot)
                lead = timedelta(minutes=cfg["schedule"]["min_lead_minutes"])
                if live and (as_taipei(slot) - datetime.now(TZ) < lead or slot_k in db.taken_slots(con) | used_slots):
                    dropped.append({"keyword": topic["keyword"], "reason": f"送出前重新驗證：時段 {slot_k} 已不足 15 分鐘或被占用"})
                    log(f"時段 {slot_k} 失效，丟棄 {topic['keyword']}")
                    continue
            urls = [s["url"] for s in topic["sources"] if s.get("ok")]
            payload = publish.build_payload(draft, cited, None if publish_now else slot)
            if publish_now:
                payload["published"] = "true"
            bad = gates.final_caption_problem(payload["message"], cfg, terms)
            if bad or img_hash is None or _sha256(post_dir / "image.png") != img_hash:
                reason = bad or "產物雜湊不符（圖檔在驗證後被改動或遺失）"
                dropped.append({"keyword": topic["keyword"], "reason": f"送出前重新驗證：{reason}"})
                log(f"丟棄 {topic['keyword']}：{reason}")
                continue
            write_json(post_dir / "payload.json", payload)
            pid = db.reserve(con, topic["norm_key"], topic["keyword"], urls, slot_k, mode if live else "dry", str(post_dir))
            if pid is None:
                dropped.append({"keyword": topic["keyword"], "reason": "DB 唯一約束衝突（話題/網址/時段已被保留）"})
                continue
            used_slots.add(slot_k)

            fb_id = None
            if live:
                check_stop()  # 打 FB API 前最後一次
                if _sha256(post_dir / "image.png") != img_hash:
                    db.release(con, pid)
                    used_slots.discard(slot_k)
                    dropped.append({"keyword": topic["keyword"], "reason": "送出前重新驗證：產物雜湊不符"})
                    continue
                try:
                    fb_id = publish.post_photo(post_dir / "image.png", payload, cfg)
                except publish.PublishUnknown as e:
                    unresolved.append(f"[{slot_k}] {draft['title']}｜{post_dir}｜{e}")
                    log(f"發佈結果不明，保留 pending：{topic['keyword']}：{e}")
                    continue  # pending 保留，不釋出 slot
                except publish.PublishFailed as e:
                    db.release(con, pid)
                    used_slots.discard(slot_k)
                    dropped.append({"keyword": topic["keyword"], "reason": f"發佈失敗：{e}"})
                    log(f"發佈失敗 {topic['keyword']}：{e}")
                    continue
                db.mark(con, pid, "scheduled", fb_id)
            else:
                db.mark(con, pid, "dry")
            posted.append({"slot": slot_k, "title": draft["title"], "category": draft["category"],
                           "traffic": topic["traffic"], "dir": str(post_dir.relative_to(ROOT)), "fb_id": fb_id})
            log(f"完成 {topic['keyword']} -> {slot_k}")
        except DeadlineReached as e:
            if pid is not None and live:
                db.release(con, pid)  # 尚未打 API，安全釋出
            dropped.append({"keyword": topic["keyword"], "reason": f"時間預算：{e}"})
            notes.append(f"時間預算不足，停止處理後續話題：{e}")
            log(f"時間預算不足，停止：{e}")
            break
        except StopRequested:
            if pid is not None and live:
                db.release(con, pid)  # 尚未打 API，安全釋出
            notes.append("執行途中偵測到 STOP，已停止")
            log("執行途中偵測到 STOP，停止")
            break
        except Exception as e:  # noqa: BLE001 - 單篇失敗跳過
            traceback.print_exc()
            msg = redact(f"{type(e).__name__}: {e}")
            if pid is not None and live:
                notes.append(f"單篇例外發生在保留之後，DB 保持 pending，請人工對帳：{topic['keyword']}")
                unresolved.append(f"{topic['keyword']}｜例外：{msg}")
            dropped.append({"keyword": topic["keyword"], "reason": f"未預期錯誤：{msg}"})

    for pid_, kw, slot_, path_ in db.pending_posts(con):
        line = f"[{slot_}] {kw}｜{path_}（DB id {pid_}）"
        if not any(kw in u for u in unresolved):
            unresolved.append(line)
    report = ROOT / "reports" / f"{today}.md"
    write_report(report, mode, posted, dropped, errors, now, unresolved, notes)
    log(f"報告：{report}（{len(posted)} 篇）")
    _DEADLINE = None
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
