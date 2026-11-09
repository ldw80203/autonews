"""稿件內容閘門：hashtag 格式與內容、image_prompt、黑名單（含最終 caption）、時間詞、易變數據、正文結構、
觀點段、原創。每一版稿件都要重跑；review 通過後以最終 caption（只含被引用來源）再完整重跑一次。"""
from __future__ import annotations

import re

from . import originality, rules
from . import select as sel
from .publish import build_caption

HASHTAG_RE = re.compile(r"#[0-9A-Za-z㐀-䶿一-鿿]{2,15}")
MAX_HASHTAGS = 5


def check_hashtags(tags: list) -> str | None:
    if not isinstance(tags, list) or not tags:
        return "hashtags 必須是非空陣列"
    if len(tags) > MAX_HASHTAGS:
        return f"hashtags 超過 {MAX_HASHTAGS} 個"
    for t in tags:
        if not isinstance(t, str) or not HASHTAG_RE.fullmatch(t):
            return f"hashtag 格式不合（需 # + 2–15 個中英數字，不可換行/空白）：{str(t)[:20]!r}"
    if len(set(tags)) != len(tags):
        return "hashtags 重複"
    return None


def sanitize_hashtags(tags: list, allowed_tag_texts: list[str] | None, whitelist: list[str]) -> list[str]:
    """剔除格式或內容不合格的 hashtag（不整篇丟稿）；去重、最多 MAX_HASHTAGS 個；剔光就補 #新聞。"""
    out: list[str] = []
    for t in tags if isinstance(tags, list) else []:
        if (isinstance(t, str) and HASHTAG_RE.fullmatch(t) and t not in out
                and rules.hashtag_content_problem([t], allowed_tag_texts, whitelist) is None):
            out.append(t)
    return out[:MAX_HASHTAGS] or ["#新聞"]


def check_image_prompt(p: str, max_chars: int) -> str | None:
    if not isinstance(p, str) or not p.strip():
        return "image_prompt 空"
    if len(p) > max_chars:
        return f"image_prompt 過長 {len(p)} > {max_chars}"
    if re.search(r"[$`\x00-\x08\x0b-\x1f]|https?://|\$imagegen", p, flags=re.I):
        return "image_prompt 含不允許的字元或網址"
    return None


def draft_gate(draft: dict, source_urls: list[str], source_texts: list[str], cfg: dict,
               terms: list[str], allowed_tag_texts: list[str] | None = None,
               keyword: str = "") -> tuple[bool, str, str]:
    """回 (通過?, 原因, 最終 caption)。
    allowed_tag_texts：話題關鍵詞 + 合格來源標題；None 時 hashtag 只認 config 白名單（fail-closed）。"""
    bad = check_hashtags(draft.get("hashtags"))
    if bad:
        return False, f"稿件關：{bad}", ""
    bad = rules.hashtag_content_problem(draft["hashtags"], allowed_tag_texts, cfg["hashtags"]["whitelist"])
    if bad:
        return False, f"稿件關：{bad}", ""
    bad = check_image_prompt(draft.get("image_prompt"), cfg["image"]["max_prompt_chars"])
    if bad:
        return False, f"稿件關：{bad}", ""
    caption = build_caption(draft, source_urls)
    hit = sel.blocklist_hit([draft["title"], draft["body"], " ".join(draft["hashtags"]),
                             draft["image_prompt"], caption], terms)
    if hit:
        return False, f"稿件關：稿件命中黑名單「{hit}」", caption
    texts = [draft["title"], draft["body"], " ".join(draft["hashtags"])]
    w = rules.time_word_hit(texts, cfg["time"]["banned_relative_words"])
    if w:
        return False, f"時效關：稿件含相對時間詞「{w}」（請用具體日期，不寫戰績/排名/累計等易變敘述）", caption
    v = rules.volatile_hit(texts, cfg["time"]["volatile_patterns"])
    if v:
        return False, f"時效關：稿件含易變數據「{v}」", caption
    bad = rules.structure_problem(draft, cfg["structure"]["min_fact_sentences"])
    if bad:
        return False, f"結構關：{bad}", caption
    facts, op = rules.split_body(draft["body"])
    ents = rules.keyword_entities(keyword) + rules.quoted_terms([draft["title"]] + facts)
    bad = rules.opinion_problem(op, ents, cfg["opinion"]["banned_words"], "\n".join([draft["title"]] + facts))
    if bad:
        return False, f"觀點關：{bad}", caption
    ok, why = originality.check(draft, source_texts, cfg["originality"]["max_overlap_chars"],
                                cfg["originality"]["min_commentary_chars"])
    return ok, why, caption


def final_caption_problem(message: str, cfg: dict, terms: list[str]) -> str | None:
    """發佈前對實際要送出的 message 字串再查一次黑名單、時間詞、易變數據（不依賴 draft 物件）。"""
    hit = sel.blocklist_hit([message], terms)
    if hit:
        return f"最終 caption 命中黑名單「{hit}」"
    w = rules.time_word_hit([message], cfg["time"]["banned_relative_words"])
    if w:
        return f"最終 caption 含相對時間詞「{w}」"
    v = rules.volatile_hit([message], cfg["time"]["volatile_patterns"])
    if v:
        return f"最終 caption 含易變數據「{v}」"
    return None
