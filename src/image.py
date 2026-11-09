"""配圖：codex exec 的 `$imagegen`（2026-11-08 實測可用）。

安全：prompt 走 stdin；cwd 是只放本次生圖的隔離暫存目錄；必須退出碼 0 且該目錄內有有效 image.png
才算成功，成功後才複製回 post 目錄。不再掃全域 ~/.codex/generated_images（可能撿到別的工作的圖）。
"""
from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from .common import load_config, run_cmd

CONSTRAINTS = ("Illustration constraints: do NOT depict any real person's face (no recognizable people, "
               "stylized faceless figures or silhouettes only), no brand logos or trademarks, "
               "absolutely no text, letters, numbers or captions in the image. Aspect ratio 4:5 portrait.")


# 固定場景模板：image_prompt 只能是這裡的字串之一，不含任何來源或寫稿模型產生的文字（防止不可信文字進到
# workspace-write 權限的 codex）。來源標題只用來『選』模板，不會被放進 prompt。
SCENES: list[tuple[str, tuple[str, ...], str]] = [
    ("baseball", ("棒球", "職棒", "中職", "mlb", "大聯盟", "投手", "打者", "全壘打", "經典賽", "12強", "u23", "u18"),
     "A stylized flat illustration of an empty baseball stadium at golden hour, green outfield grass, "
     "bright floodlights and a scoreboard silhouette, warm and energetic mood."),
    ("basketball", ("籃球", "nba", "plg", "tpbl", "灌籃", "三分球"),
     "A stylized flat illustration of an indoor basketball court with a hoop and a ball resting on the "
     "polished wooden floor, dramatic arena lighting."),
    ("football", ("足球", "英超", "西甲", "世足", "歐冠"),
     "A stylized flat illustration of a football pitch at night under stadium lights, a ball on the center circle."),
    ("racket", ("網球", "羽球", "桌球", "匹克球"),
     "A stylized flat illustration of an empty indoor racket sports court with a net and a shuttle-shaped light trail."),
    ("sports", ("運動", "賽事", "奧運", "馬拉松", "選手", "冠軍", "體育"),
     "A stylized flat illustration of a running track and stadium seats in morning light, sporty and calm."),
    ("weather", ("颱風", "天氣", "氣象", "降雨", "豪雨", "寒流", "低溫", "高溫", "鋒面", "冷氣團"),
     "A stylized flat illustration of a city skyline under dramatic layered clouds with rain streaks and an umbrella."),
    ("traffic", ("捷運", "高鐵", "台鐵", "交通", "公車", "機場", "航班", "道路", "國道", "車站"),
     "A stylized flat illustration of a modern train platform and city transit lines at dusk, soft motion blur."),
    ("environment", ("空氣", "環保", "污染", "汙染", "減碳", "回收", "水庫", "生態", "排放"),
     "A calm watercolor illustration of a reservoir surrounded by green hills and clear sky."),
    ("finance", ("股", "台股", "匯率", "財經", "央行", "通膨", "房價", "房市", "經濟", "營收", "基金"),
     "A stylized flat illustration of abstract rising and falling chart lines over a city skyline, blue tones."),
    ("tech", ("ai", "科技", "手機", "半導體", "晶片", "電腦", "軟體", "網路"),
     "A stylized flat illustration of glowing circuit patterns and an abstract chip on a dark blue background."),
    ("politics", ("立法院", "選舉", "政黨", "行政院", "議會", "政府", "政策", "部長", "市長", "縣長"),
     "A stylized flat illustration of a classical government building facade under a clear sky, neutral and calm."),
    ("entertainment", ("演唱會", "藝人", "電影", "歌手", "偶像", "戲劇", "綜藝", "影集", "專輯"),
     "A stylized flat illustration of an empty concert stage with colorful spotlights and confetti in the air."),
    ("food", ("美食", "餐廳", "小吃", "料理", "飲料", "夜市"),
     "A stylized flat illustration of a cozy night market food stall with warm lanterns and steam rising."),
    ("travel", ("旅遊", "景點", "觀光", "連假", "出國", "飯店"),
     "A stylized flat illustration of a scenic mountain road and a small backpack, bright travel mood."),
]
DEFAULT_SCENE = ("news", "A stylized flat illustration of a calm city street corner with a newspaper stand and "
                         "soft morning light, neutral news mood.")


def scene_for(topic: dict) -> str:
    """依話題關鍵詞 + 合格來源標題挑場景類別（只做分類，不取用任何文字）。英數詞以完整 token 比對。"""
    import re
    import unicodedata
    blob = unicodedata.normalize("NFKC", " ".join(
        [topic.get("keyword") or ""] + [s.get("title") or "" for s in topic.get("sources", []) if s.get("ok")])).lower()
    tokens = set(re.findall(r"[a-z0-9]+", blob))
    for name, keys, _p in SCENES:
        for k in keys:
            if (k in tokens) if re.fullmatch(r"[a-z0-9]+", k) else (k in blob):
                return name
    return DEFAULT_SCENE[0]


def scene_prompt(scene: str) -> str:
    for name, _k, p in SCENES:
        if name == scene:
            return p
    return DEFAULT_SCENE[1]


def is_template_prompt(p: str) -> bool:
    return p == DEFAULT_SCENE[1] or any(p == x for _n, _k, x in SCENES)


def build_prompt(image_prompt: str, constraints: str | None = None) -> str:
    """constraints=None 用預設 CONSTRAINTS（固定模板：無真人臉、無文字）；imagespec 傳 "" 並在 prompt 內自帶條件。"""
    c = CONSTRAINTS if constraints is None else constraints
    return (f"$imagegen {image_prompt.strip()} {c} "
            "Save the generated image into the current working directory as image.png (overwrite if exists).")


def is_valid_image(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


_NO_LINE_START = set("，。、；：？！）」』》〉％%,.;:?!)]}")
_MAX_HEADLINE_LINES = 2


def _headline_tokens(title: str) -> list[str]:
    """英數字串整段不拆；CJK 逐字；行首禁則標點黏到前一個 token。"""
    import re
    out: list[str] = []
    for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.'’\-]*|\s+|.", title):
        if out and (t in _NO_LINE_START):
            out[-1] += t
        else:
            out.append(t)
    return out


def _phrases(title: str) -> list[str]:
    """依空白與句中標點切成不可拆的詞組；標點與後面的空白留在前一段。"""
    import re
    p = r"？！，、：；?!,:;"
    return re.findall(rf"[^\s{p}]*[{p}]+\s*|[^\s{p}]+\s*", title)


def _wrap(draw, tokens: list[str], font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for t in tokens:
        cand = cur + t
        if cur and draw.textlength(cand.strip(), font=font) > max_w:
            lines.append(cur.strip())
            cur = t.lstrip()
        else:
            cur = cand
    if cur.strip():
        lines.append(cur.strip())
    return lines


def _layout(draw, title: str, max_w: float, img_w: int, font_path: str):
    """回 (font, lines, size)。最多 2 行；先試只在空白/標點斷行（字級由大到小），再試逐字斷行（字級由大到小）；
    縮到最小字級仍放不下 2 行，才排第 3 行（必要時繼續縮字，保證 <= 3 行）。"""
    from PIL import ImageFont
    phrases = _phrases(title)
    big, small = max(8, round(img_w * 0.085)), max(8, round(img_w * 0.055))
    for size in range(big, small - 1, -2):
        f = ImageFont.truetype(font_path, size)
        if all(draw.textlength(p.strip(), font=f) <= max_w for p in phrases):
            lines = _wrap(draw, phrases, f, max_w)
            if len(lines) <= _MAX_HEADLINE_LINES:
                return f, lines, size
    tokens = _headline_tokens(title)
    for size in range(big, small - 1, -2):
        f = ImageFont.truetype(font_path, size)
        lines = _wrap(draw, tokens, f, max_w)
        if len(lines) <= _MAX_HEADLINE_LINES:
            return f, lines, size
    size = small
    while True:
        f = ImageFont.truetype(font_path, size)
        lines = _wrap(draw, tokens, f, max_w)
        if len(lines) <= _MAX_HEADLINE_LINES + 1 or size <= 8:
            return f, lines, size
        size = max(8, round(size * 0.9))


YELLOW = (255, 212, 0)


def _highlight_span(title: str, lines: list[str], highlight: str | None) -> tuple[int, int] | None:
    """回第二行內要上黃色的 (起, 迄) 字元範圍；沒有第二行回 None；highlight 不合格就整行。"""
    if len(lines) < 2:
        return None
    line = lines[1]
    hl = (highlight or "").strip()
    if 2 <= len(hl) <= 8 and hl in title:
        i = line.find(hl)
        if i >= 0:
            return i, i + len(hl)
    return 0, len(line)


def add_headline(path: Path, title: str, cfg: dict | None = None, highlight: str | None = None) -> str | None:
    """把稿件標題疊在圖片下方（深紫黑漸層底條＋白色粗體黑描邊），覆寫原檔。成功回 None，失敗回原因。
    最多 2 行（放不下才縮字，最小字級仍放不下才第 3 行）；第 2 行（或其中的 highlight 片段）為黃色。
    文字由程式繪製，不經生圖模型，所以不會有錯字。"""
    from PIL import Image, ImageDraw
    cfg = cfg or load_config()
    title = " ".join((title or "").split())
    if not title:
        return "標題是空的"
    font_path = cfg["image"].get("headline_font", r"C:\Windows\Fonts\msjhbd.ttc")
    try:
        im = Image.open(path).convert("RGB")
    except Exception as e:  # noqa: BLE001
        return f"讀圖失敗：{e}"
    w, h = im.size
    margin = max(1, round(w * 0.06))
    draw = ImageDraw.Draw(im)
    try:
        font, lines, size = _layout(draw, title, w - 2 * margin, w, font_path)
    except OSError as e:
        return f"字型載入失敗（{font_path}）：{e}"
    lines = [(ln.rstrip("，、；：,;:").rstrip() or ln) for ln in lines]  # 行尾逗號等不顯示；「？！」與引號保留
    line_h = round(size * 1.3)
    block_h = line_h * len(lines)
    shade_h = min(h, max(round(h * 0.4), block_h + margin * 3))
    ramp = Image.new("RGBA", (1, shade_h))
    for y in range(shade_h):
        t = min(1.0, y / max(1.0, shade_h * 0.3))  # 上緣 30% 柔和過渡，其餘（文字區）近乎不透明
        ramp.putpixel((0, y), (20, 10, 35, round(240 * t * t * (3 - 2 * t))))
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    overlay.paste(ramp.resize((w, shade_h)), (0, h - shade_h))
    im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(im)
    span = _highlight_span(title, lines, highlight)
    stroke = max(1, size // 18)
    y = h - margin - block_h
    for n, line in enumerate(lines):
        if n == 1 and span:
            parts = [(line[:span[0]], (255, 255, 255)), (line[span[0]:span[1]], YELLOW), (line[span[1]:], (255, 255, 255))]
        else:
            parts = [(line, (255, 255, 255))]
        x = (w - sum(draw.textlength(t, font=font) for t, _c in parts)) / 2  # 置中
        for t, col in parts:
            if t:
                draw.text((x, y), t, font=font, fill=col, stroke_width=stroke, stroke_fill=(0, 0, 0))
                x += draw.textlength(t, font=font)
        y += line_h
    im.save(path, format="PNG")
    return None


def generate_image(post_dir: Path, image_prompt: str, cfg: dict | None = None,
                   constraints: str | None = None) -> tuple[Path | None, str | None]:
    cfg = cfg or load_config()
    post_dir = Path(post_dir)
    post_dir.mkdir(parents=True, exist_ok=True)
    target = post_dir / "image.png"
    target.unlink(missing_ok=True)
    if len(image_prompt or "") > cfg["image"]["max_prompt_chars"]:
        return None, "image_prompt 超過長度上限"
    exe = shutil.which("codex")
    if not exe:
        return None, "找不到 codex"
    # 不用 mkdtemp：它在 Windows 會設成只有擁有者可存取，codex 沙盒帳號會被拒絕寫入
    tmp = Path(tempfile.gettempdir()) / f"autonews_img_{uuid.uuid4().hex[:12]}"
    tmp.mkdir()
    try:
        # '-' 讓 codex 從 stdin 讀指令
        code, _o, err = run_cmd(
            [exe, "exec", "--skip-git-repo-check", "-C", str(tmp), "-s", "workspace-write",
             "-o", str(tmp / "image_log.txt"), "-"],
            timeout=cfg["timeouts"]["codex_image"], input_text=build_prompt(image_prompt, constraints))
        produced = tmp / "image.png"
        if code != 0:
            return None, f"生圖失敗 exit={code} {err[:150]}"
        if not (produced.exists() and is_valid_image(produced)):
            reply = ""
            if (tmp / "image_log.txt").exists():  # codex 拒絕時會在這裡說明原因
                reply = (tmp / "image_log.txt").read_text(encoding="utf-8", errors="replace").strip()[:150]
            return None, f"生圖失敗：隔離目錄內沒有有效的 image.png（codex：{reply}）"
        shutil.copyfile(produced, target)
        log = tmp / "image_log.txt"
        if log.exists():
            shutil.copyfile(log, post_dir / "image_log.txt")
        return target, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
