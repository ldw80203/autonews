"""依每篇內容生成配圖（2026-11-09 使用者裁示，grilling 定案）。

流程：寫稿通過閘門後 ->
1. 另呼叫一次 claude 讀貼文，輸出配圖規格 JSON：mode（portrait 寫實人像／illustration 插畫）、person、
   scene（英文畫面描述）、fallback_scene（不畫人的插畫版）、highlight（正文原樣摘的重點句）。
2. 程式檢查：scene 只准英文字母數字與基本標點、擋指令字眼、長度上限；person 只准英文姓名；
   highlight 必須是正文一字不差的片段、20 字內、只准中文數字與基本標點。圖中中文只有「標題（程式原樣放入）」
   與「highlight」兩個入口。不合格 -> 回 None，由呼叫端退回固定模板。
3. 生圖（圖中含標題＋重點句）。portrait 被拒 -> 改 fallback_scene 插畫再生一次。
4. claude 看圖比對文字；不一致 -> 生一張無字版本，由程式疊標題（image.add_headline）。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .common import CLAUDE_SYSTEM_PROMPT, claude_args, fold_width, parse_single_json, run_cmd, write_json
from . import image

SCENE_RE = re.compile(r"^[A-Za-z0-9 ,.'\-;:/()%&]+$")
PATHLIKE = re.compile(r"[A-Za-z]:[\\/]|//|\.\.|/[A-Za-z0-9_.-]+/")  # 擋檔案路徑、網址、上層目錄
NO_TEXT = re.compile(r"\s*\b(no|without)\s+(any\s+)?(text|words|letters|captions?)\b[.,;]?", re.I)
PERSON_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,58}$")
HIGHLIGHT_RE = re.compile(r"^[㐀-鿿0-9０-９%％.．,，、:：!！?？\-－ ]+$")
BANNED = re.compile(r"\b(files?|save|saved|read|write|ignore|instructions?|https?|www|env|path|folders?|"
                    r"director(y|ies)|commands?|execute|run|delete|upload|download|scripts?|system|prompts?|"
                    r"passwords?|tokens?|secrets?|credentials?|api|shell|powershell|codex|imagegen)\b", re.I)
MAX_SCENE = 600  # 給 AI 的指示寫 400，程式上限留緩衝（實測 AI 會寫超過 400）
MAX_HIGHLIGHT = 20

SPEC_PROMPT = """你是台灣新聞粉專的配圖設計師。讀下面這篇貼文，設計最能幫讀者理解這則新聞的配圖。
只輸出一個 JSON 物件，不要任何其他文字：
{"mode": "portrait 或 illustration", "person": "", "scene": "", "fallback_scene": "", "highlight": ""}

規則：
1. mode=portrait：只在新聞主角是一位公眾人物（政治人物、藝人、企業負責人、運動員）時使用。person 填該人物的英文姓名。
   scene 只描述這個人物本身的寫實人像（表情中性、背景單純），不可畫事件情境、其他人物、或與事件相關的物品。
   一般民眾、未成年人、被害人、嫌犯一律不可用 portrait。
2. mode=illustration：其他情況，person 留空。scene 描述最能幫讀者理解這則新聞的畫面，畫風依題材自選並寫進 scene：
   財經用圖表（漲用紅色、跌用綠色，台灣習慣）、奇聞用可愛插畫、體育用動感畫面、政治用中性畫面。不可畫可辨識的真實人物。
3. fallback_scene：同一則新聞「不畫任何真實人物」的插畫版描述（portrait 被拒時使用）。
4. scene、fallback_scene：只能用英文字母、數字、空格與基本標點（, . ' - ; : / ( ) % &），各 400 字元內。
   不要描述畫面上的文字（文字由系統另外加），不要寫任何與檔案、指令、程式有關的字。
5. highlight：從正文原樣摘一段最重要的重點（最好含關鍵數字），20 字以內，必須是正文中一字不差的連續片段；
   沒有合適的就填空字串。

<<<POST_BEGIN>>>
"""


def _norm(s: str) -> str:
    return re.sub(r"[\s　]", "", fold_width(s or ""))


def check_scene(s) -> str | None:
    if not isinstance(s, str) or not (20 <= len(s.strip()) <= MAX_SCENE):
        return "scene 長度不合"
    if not SCENE_RE.match(s.strip()):
        bad = sorted({c for c in s.strip() if not SCENE_RE.match(c)})
        return f"scene 含不允許的字元 {''.join(bad)!r}"
    if BANNED.search(s):
        return f"scene 含禁用字「{BANNED.search(s).group(0)}」"
    if PATHLIKE.search(s):
        return f"scene 含路徑或網址寫法「{PATHLIKE.search(s).group(0)}」"
    return None


def check_highlight(h, body: str) -> str:
    """合格回 highlight，不合格回空字串（只放標題）。"""
    h = (h or "").strip() if isinstance(h, str) else ""
    if not h or len(re.sub(r"\s", "", h)) > MAX_HIGHLIGHT:  # 20 字不算空格
        return ""
    # 全形/半形差異（「，」vs「,」）不算不同；回傳正文裡的原樣寫法。fold_width 逐字轉換、長度不變
    i = fold_width(body or "").find(fold_width(h))
    if i < 0:
        return ""
    h = (body or "")[i:i + len(h)]
    return h if HIGHLIGHT_RE.match(h) else ""


def validate_spec(spec, draft: dict) -> tuple[dict | None, str]:
    if not isinstance(spec, dict):
        return None, "配圖規格不是 JSON 物件"
    mode = spec.get("mode")
    if mode not in ("portrait", "illustration"):
        return None, "mode 不合法"
    for k in ("scene", "fallback_scene"):
        bad = check_scene(spec.get(k))
        if bad:
            return None, f"{k}：{bad}"
    person = (spec.get("person") or "").strip()
    if mode == "portrait" and (not PERSON_RE.match(person) or BANNED.search(person)):
        return None, "portrait 的 person 不合格"
    return {"mode": mode, "person": person if mode == "portrait" else "",
            "scene": spec["scene"].strip(), "fallback_scene": spec["fallback_scene"].strip(),
            "highlight": check_highlight(spec.get("highlight"), draft.get("body", ""))}, ""


def build_image_prompt(spec: dict, title: str, with_text: bool, use_fallback: bool = False) -> str:
    title = " ".join((title or "").split())[:40]
    if spec["mode"] == "portrait" and not use_fallback:
        parts = [spec["scene"], f"Photorealistic portrait photo of {spec['person']}. Only this one person, neutral "
                 "expression, simple background. Do not depict any event, any other person, or any related objects."]
    else:
        parts = [spec["fallback_scene"] if use_fallback else spec["scene"],
                 "Illustration. Do not depict any real, recognizable person."]
    if with_text:  # AI 有時在 scene 寫「no text」，跟加標題互相矛盾，拿掉
        parts[0] = NO_TEXT.sub("", parts[0]).strip()
        parts.append(f"Typography: render this Traditional Chinese headline in large bold type, exactly character "
                     f"for character: {title}")
        if spec.get("highlight"):
            parts.append(f"Below it, render this smaller Traditional Chinese line exactly: {spec['highlight']}")
        parts.append("Place the text at the top or the bottom of the image; the text must not cover any face or "
                     "the main chart. No other text anywhere.")
    else:
        parts.append("Absolutely no text, letters or numbers anywhere in the image.")
    parts.append("No brand logos or trademarks. Aspect ratio 4:5 portrait.")
    return " ".join(parts)


def _claude(prompt: str, cfg: dict, extra: list[str] | None = None, cwd: str | None = None):
    exe = shutil.which("claude")
    if not exe:
        return None, "找不到 claude"
    args = claude_args(exe, "sonnet")
    if extra:
        i = args.index("--tools")
        args = args[:i] + extra + args[i + 2:]
    code, out, err = run_cmd(args, timeout=cfg["timeouts"]["claude"], input_text=prompt, cwd=cwd)
    if code != 0:
        return None, f"claude 失敗 exit={code} {err[:150]}"
    outer = parse_single_json(out)
    inner = outer.get("result") if isinstance(outer, dict) else None
    return (parse_single_json(inner) if isinstance(inner, str) else None), None


def get_spec(draft: dict, cfg: dict) -> tuple[dict | None, str]:
    post = f"標題：{draft.get('title', '')}\n\n{draft.get('body', '')}\n<<<POST_END>>>"
    raw, err = _claude(SPEC_PROMPT + post, cfg)
    if err:
        return None, err
    return validate_spec(raw, draft)


def read_texts(img: Path, cfg: dict) -> list[str] | None:
    """claude 看圖，回圖中所有中文字串；失敗回 None。"""
    img = Path(img).resolve()
    prompt = (f"Read the image file at {img} . Transcribe every piece of Chinese text visible in the image exactly, "
              'character for character. Output only JSON: {"texts": ["..."]}')
    extra = ["--tools", "Read", "--allowedTools", "Read", "--add-dir", str(img.parent)]
    j, err = _claude(prompt, cfg, extra, cwd=str(img.parent))
    if err or not isinstance(j, dict) or not isinstance(j.get("texts"), list):
        return None
    return [str(t) for t in j["texts"]]


def text_matches(texts: list[str] | None, title: str, highlight: str) -> bool:
    if texts is None:
        return False
    seen = _norm("".join(texts))
    return _norm(title) in seen and (not highlight or _norm(highlight) in seen)


def make_image(post_dir: Path, draft: dict, cfg: dict) -> tuple[Path | None, dict]:
    """回 (image.png 路徑或 None, 紀錄)。None = 呼叫端退回固定模板＋程式疊字。紀錄寫入 image_spec.json。"""
    post_dir = Path(post_dir)
    log: dict = {"steps": []}
    spec, why = get_spec(draft, cfg)
    log["spec"], log["spec_error"] = spec, why
    if spec is None:
        write_json(post_dir / "image_spec.json", log)
        return None, log
    title = draft["title"]
    # 每個版本：先試帶字；被拒（實測 codex 會拒絕把涉及真人爭議的「標題字」做成圖）或字不對 -> 同版本無字＋程式疊標題。
    # 人像題先把人像版試完（含無字），才退回不畫人的插畫版。
    versions = [False, True] if spec["mode"] == "portrait" else [False]
    for use_fallback in versions:
        name = "illustration" if (use_fallback or spec["mode"] != "portrait") else "portrait"
        p, err = image.generate_image(post_dir, build_image_prompt(spec, title, True, use_fallback), cfg, constraints="")
        log["steps"].append({"try": f"{name}+text", "error": err})
        if not err:
            texts = read_texts(p, cfg)
            log["texts_seen"] = texts
            if text_matches(texts, title, spec["highlight"]):
                log["result"] = f"{name} ai_text"
                write_json(post_dir / "image_spec.json", log)
                return p, log
        p, err = image.generate_image(post_dir, build_image_prompt(spec, title, False, use_fallback), cfg, constraints="")
        log["steps"].append({"try": f"{name} no-text+overlay", "error": err})
        if not err:
            herr = image.add_headline(p, title, cfg)
            log["result"] = f"{name} overlay" if herr is None else f"overlay 失敗：{herr}"
            write_json(post_dir / "image_spec.json", log)
            return (p if herr is None else None), log
    write_json(post_dir / "image_spec.json", log)
    return None, log
