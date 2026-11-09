"""2026-11-09 使用者要求：附圖要有大標題（程式疊字，不經生圖模型）。"""
from PIL import Image, ImageDraw, ImageFont

from src import image
from src.common import load_config

CFG = load_config()


def _img(path, size=(1122, 1402), color=(200, 220, 255)):
    Image.new("RGB", size, color).save(path)
    return path


def test_headline_draws_on_bottom_and_keeps_size(tmp_path):
    p = _img(tmp_path / "image.png")
    assert image.add_headline(p, "Labubu 熱潮降溫？美國營收減至少 70％", CFG) is None
    with Image.open(p) as im:
        assert im.size == (1122, 1402)
        assert im.getpixel((561, 50)) == (200, 220, 255)  # 上方不動
        bottom = im.crop((0, 1100, 1122, 1402)).convert("L")
        lo, hi = bottom.getextrema()
        assert hi >= 250 and lo < 60  # 白字＋暗底


def test_headline_breaks_at_space_or_punctuation():
    font = ImageFont.truetype(CFG["image"]["headline_font"], 90)
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = image._wrap(draw, image._phrases("國安基金啟動第10次護盤 前兩次買了哪些股票？"), font, 1000)
    assert lines == ["國安基金啟動第10次護盤", "前兩次買了哪些股票？"]
    assert image._phrases("Labubu 熱潮降溫？美國營收減至少 70％") == ["Labubu ", "熱潮降溫？", "美國營收減至少 ", "70％"]


def test_headline_long_title_max_three_lines(tmp_path):
    p = _img(tmp_path / "image.png")
    title = "這是一個非常非常長而且完全沒有任何空白或標點可以斷行的新聞標題用來測試自動縮字與逐字斷行功能是否正常運作"
    assert image.add_headline(p, title, CFG) is None
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    max_w = 1122 - 2 * round(1122 * 0.06)
    font, lines, size = image._layout(draw, title, max_w, 1122, CFG["image"]["headline_font"])
    assert len(lines) <= 3 and "".join(lines) == title
    assert all(draw.textlength(x, font=font) <= max_w for x in lines)
    _f, lines, big = image._layout(draw, "國安基金啟動第10次護盤 前兩次買了哪些股票？", max_w, 1122,
                                   CFG["image"]["headline_font"])
    assert lines == ["國安基金啟動第10次護盤", "前兩次買了哪些股票？"] and big > size


def test_headline_errors(tmp_path):
    p = _img(tmp_path / "image.png")
    assert "空" in image.add_headline(p, "   ", CFG)
    bad = dict(CFG, image=dict(CFG["image"], headline_font=str(tmp_path / "nope.ttc")))
    assert "字型" in image.add_headline(p, "標題", bad)
    assert "讀圖" in image.add_headline(tmp_path / "missing.png", "標題", CFG)


def test_headline_tiny_image_does_not_crash(tmp_path):
    p = _img(tmp_path / "image.png", size=(4, 5))
    assert image.add_headline(p, "曾峻岳挑戰大聯盟", CFG) is None
