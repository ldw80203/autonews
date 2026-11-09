"""2026-11-10 使用者裁示：配圖改用新聞原文 og:image 原圖＋疊字版型。測試一律不連網。"""
import copy
import io

import pytest
from PIL import Image

from src import collect, image, photo, publish
from src.common import load_config
from tests.test_round5 import CFG as CFG_AI, TERMS, _setup_topic
import run_daily

CFG = load_config()
FONT = CFG["image"]["headline_font"]


# ---------- og:image 解析 ----------
def test_og_image_attribute_order_and_quotes():
    a = '<meta property="og:image" content="https://x.com/a.jpg">'
    b = "<meta content='https://x.com/b.jpg' property='og:image' />"
    c = '<META CONTENT="https://x.com/c.jpg" PROPERTY="og:image">'
    assert collect.extract_image_url(a, "https://x.com/n") == "https://x.com/a.jpg"
    assert collect.extract_image_url(b, "https://x.com/n") == "https://x.com/b.jpg"
    assert collect.extract_image_url(c, "https://x.com/n") == "https://x.com/c.jpg"


def test_og_image_relative_and_entities():
    h = '<meta property="og:image" content="/img/a.jpg?x=1&amp;y=2">'
    assert collect.extract_image_url(h, "https://news.example.com/a/b") == "https://news.example.com/img/a.jpg?x=1&y=2"
    h2 = '<meta property="og:image" content="//cdn.example.com/p.jpg">'
    assert collect.extract_image_url(h2, "https://news.example.com/a") == "https://cdn.example.com/p.jpg"


def test_twitter_image_fallback_and_og_preferred():
    tw = '<meta name="twitter:image" content="https://x.com/t.jpg">'
    assert collect.extract_image_url(tw, "https://x.com/") == "https://x.com/t.jpg"
    both = tw + '<meta property="og:image" content="https://x.com/o.jpg">'
    assert collect.extract_image_url(both, "https://x.com/") == "https://x.com/o.jpg"


def test_no_image_tag_or_bad_scheme():
    assert collect.extract_image_url("<html><meta charset='utf-8'></html>", "https://x.com/") is None
    assert collect.extract_image_url("", "https://x.com/") is None
    assert collect.extract_image_url('<meta property="og:image" content="javascript:alert(1)">', "https://x.com/") is None
    assert collect.extract_image_url('<meta property="og:image" content="">', "https://x.com/") is None


def test_fetch_article_returns_image_url(monkeypatch):
    html = ('<html><head><meta property="og:image" content="/p.jpg"></head><body><article><p>'
            + "這是一段很長的中文新聞內文。" * 30 + "</p></article></body></html>")

    class R:
        url = "https://news.example.com/a"
        encoding = "utf-8"
        text = html
        def raise_for_status(self): pass
    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: R())
    art = collect.fetch_article("https://news.example.com/a")
    assert art["image_url"] == "https://news.example.com/p.jpg"


def test_pick_photo():
    srcs = [{"ok": False, "image_url": "https://a/1.jpg"}, {"ok": True, "image_url": None},
            {"ok": True, "image_url": "https://b/2.jpg"}, {"ok": True, "image_url": "https://c/3.jpg"}]
    assert photo.pick_photo(srcs) == "https://b/2.jpg"
    assert photo.pick_photo([{"ok": True}]) is None
    assert photo.pick_photo([]) is None


# ---------- download_photo：假 DNS 與假 requests ----------
def _png(size=(1600, 900), color=(30, 120, 200)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="PNG")
    return b.getvalue()


class Resp:
    def __init__(self, status=200, headers=None, body=b"", chunks=None):
        self.status_code, self.headers, self._body, self._chunks = status, headers or {}, body, chunks
        self.closed = False
    def iter_content(self, n):
        if self._chunks is not None:
            yield from self._chunks
        else:
            for i in range(0, len(self._body), n):
                yield self._body[i:i + n]
    def close(self):
        self.closed = True


def _net(monkeypatch, routes, dns=None):
    """routes: url -> Resp；dns: host -> ip（預設全部 8.8.8.8）。回請求網址清單。"""
    seen = []
    dns = dns or {}

    def gai(host, port, *a, **k):
        return [(2, 1, 6, "", (dns.get(host, "8.8.8.8"), port))]

    def opener(scheme, ip, port, host, target, timeout):
        url = f"{scheme}://{host}{target}"
        seen.append(url)
        assert timeout
        return routes[url]
    monkeypatch.setattr(photo.socket, "getaddrinfo", gai)
    monkeypatch.setattr(photo, "_open", opener)
    return seen


def _img_resp(body=None, ctype="image/png"):
    return Resp(200, {"Content-Type": ctype}, body if body is not None else _png())


def test_download_ok_outputs_1080x1350_png_no_exif(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://img.example.com/a.png": _img_resp()})
    dest = tmp_path / "image.png"
    ok, why = photo.download_photo("https://img.example.com/a.png", dest, CFG)
    assert ok and why == ""
    with Image.open(dest) as im:
        assert im.size == (1080, 1350) and im.format == "PNG" and not im.getexif()


def test_download_portrait_and_cover_crop_center(tmp_path, monkeypatch):
    # 左半紅、右半藍的寬圖：置中裁 4:5 後中間切線應落在水平中央
    im = Image.new("RGB", (2000, 1000), (255, 0, 0))
    im.paste((0, 0, 255), (1000, 0, 2000, 1000))
    b = io.BytesIO()
    im.save(b, format="PNG")
    _net(monkeypatch, {"https://img.example.com/w.png": _img_resp(b.getvalue())})
    dest = tmp_path / "w.png"
    assert photo.download_photo("https://img.example.com/w.png", dest, CFG)[0]
    with Image.open(dest) as o:
        assert o.getpixel((100, 600))[0] > 200 and o.getpixel((980, 600))[2] > 200


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.jpg", "http://192.168.1.1/a.jpg", "http://10.0.0.5/a.jpg", "http://172.16.0.1/a.jpg",
    "http://169.254.169.254/latest/meta-data", "http://0.0.0.0/a.jpg", "http://[::1]/a.jpg",
    "http://224.0.0.1/a.jpg", "http://localhost/a.jpg", "http://[::ffff:127.0.0.1]/a.jpg", "http://[fe80::1]/a.jpg",
    "http://[fc00::1]/a.jpg", "http://100.64.0.1/a.jpg", "http://[::ffff:10.0.0.1]/a.jpg"])
def test_ssrf_private_hosts_rejected(tmp_path, monkeypatch, url):
    # 這一組不 patch getaddrinfo：用真的解析（只解析字面 IP / localhost，不出網）
    monkeypatch.setattr(photo, "_open", lambda *a, **k: pytest.fail("不該發出請求"))
    ok, why = photo.download_photo(url, tmp_path / "x.png", CFG)
    assert not ok and ("非公網" in why or "解析" in why), why
    assert not (tmp_path / "x.png").exists()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a.jpg", "gopher://example.com/", "data:image/png;base64,AAAA", "//example.com/a.jpg", ""])
def test_non_http_schemes_rejected(tmp_path, monkeypatch, url):
    monkeypatch.setattr(photo, "_open", lambda *a, **k: pytest.fail("不該發出請求"))
    ok, why = photo.download_photo(url, tmp_path / "x.png", CFG)
    assert not ok and why


def test_dns_resolving_to_private_rejected(tmp_path, monkeypatch):
    _net(monkeypatch, {}, dns={"evil.example.com": "10.1.2.3"})
    ok, why = photo.download_photo("https://evil.example.com/a.jpg", tmp_path / "x.png", CFG)
    assert not ok and "非公網" in why


def test_dns_any_private_ip_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(photo.socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(2, 1, 6, "", ("8.8.8.8", p)), (2, 1, 6, "", ("127.0.0.1", p))])
    monkeypatch.setattr(photo, "_open", lambda *a, **k: pytest.fail("不該發出請求"))
    ok, why = photo.download_photo("https://mixed.example.com/a.jpg", tmp_path / "x.png", CFG)
    assert not ok and "非公網" in why


def test_redirect_to_internal_rejected(tmp_path, monkeypatch):
    seen = _net(monkeypatch, {"https://img.example.com/r": Resp(302, {"Location": "http://169.254.169.254/x"})},
                dns={})
    # 169.254.169.254 是字面 IP，getaddrinfo 假的會回 8.8.8.8；改假 DNS 對該主機回內網
    monkeypatch.setattr(photo.socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(2, 1, 6, "", ("169.254.169.254" if h == "169.254.169.254" else "8.8.8.8", p))])
    ok, why = photo.download_photo("https://img.example.com/r", tmp_path / "x.png", CFG)
    assert not ok and "非公網" in why
    assert seen == ["https://img.example.com/r"]  # 內網那跳沒有發出請求


def test_redirect_to_file_scheme_rejected(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://img.example.com/r": Resp(302, {"Location": "file:///c:/windows/win.ini"})})
    ok, why = photo.download_photo("https://img.example.com/r", tmp_path / "x.png", CFG)
    assert not ok and "協定" in why


def test_redirect_follow_ok_and_max_three_hops(tmp_path, monkeypatch):
    routes = {"https://a.example.com/1": Resp(301, {"Location": "/2"}),
              "https://a.example.com/2": Resp(302, {"Location": "https://b.example.com/3"}),
              "https://b.example.com/3": _img_resp()}
    seen = _net(monkeypatch, routes)
    assert photo.download_photo("https://a.example.com/1", tmp_path / "ok.png", CFG)[0]
    assert seen == ["https://a.example.com/1", "https://a.example.com/2", "https://b.example.com/3"]
    loop = {f"https://a.example.com/{i}": Resp(302, {"Location": f"/{i + 1}"}) for i in range(1, 10)}
    seen = _net(monkeypatch, loop)
    ok, why = photo.download_photo("https://a.example.com/1", tmp_path / "loop.png", CFG)
    assert not ok and "轉址" in why and len(seen) == 4  # 原網址 + 3 跳


def test_oversize_stream_aborted_at_10mb(tmp_path, monkeypatch):
    chunk = b"\0" * (1024 * 1024)
    resp = Resp(200, {"Content-Type": "image/jpeg"}, chunks=iter([chunk] * 100))
    _net(monkeypatch, {"https://img.example.com/big": resp})
    ok, why = photo.download_photo("https://img.example.com/big", tmp_path / "x.png", CFG)
    assert not ok and "10MB" in why and resp.closed
    # 沒讀完整個串流（11 塊就該停）
    assert sum(1 for _ in resp._chunks) > 80


def test_content_length_over_limit_rejected(tmp_path, monkeypatch):
    resp = Resp(200, {"Content-Type": "image/png", "Content-Length": str(11 * 1024 * 1024)}, chunks=iter([]))
    _net(monkeypatch, {"https://img.example.com/b": resp})
    ok, why = photo.download_photo("https://img.example.com/b", tmp_path / "x.png", CFG)
    assert not ok and "10MB" in why


def test_non_image_content_type_rejected(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://img.example.com/h": _img_resp(b"<html></html>", "text/html; charset=utf-8")})
    ok, why = photo.download_photo("https://img.example.com/h", tmp_path / "x.png", CFG)
    assert not ok and "Content-Type" in why


def test_http_error_status_rejected(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://img.example.com/e": Resp(404, {})})
    ok, why = photo.download_photo("https://img.example.com/e", tmp_path / "x.png", CFG)
    assert not ok and "404" in why


def test_too_small_rejected_and_min_side_configurable(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://img.example.com/s": _img_resp(_png((300, 900)))})
    ok, why = photo.download_photo("https://img.example.com/s", tmp_path / "x.png", CFG)
    assert not ok and "太小" in why and not (tmp_path / "x.png").exists()
    cfg = copy.deepcopy(CFG)
    cfg["image"]["min_photo_side"] = 200
    assert photo.download_photo("https://img.example.com/s", tmp_path / "y.png", cfg)[0]


def test_corrupt_image_rejected_no_exception(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://img.example.com/c": _img_resp(b"not an image at all")})
    ok, why = photo.download_photo("https://img.example.com/c", tmp_path / "x.png", CFG)
    assert not ok and "無法解碼" in why
    trunc = _png((1600, 900))[:200]
    _net(monkeypatch, {"https://img.example.com/c": _img_resp(trunc)})
    assert photo.download_photo("https://img.example.com/c", tmp_path / "x.png", CFG)[0] is False


def test_pixel_bomb_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)  # 讓 photo 設的全域上限在測試後還原
    monkeypatch.setattr(photo, "MAX_PIXELS", 1_000_000)
    _net(monkeypatch, {"https://img.example.com/p": _img_resp(_png((1600, 900)))})
    ok, why = photo.download_photo("https://img.example.com/p", tmp_path / "x.png", CFG)
    assert not ok and "像素" in why


def test_request_exception_does_not_raise(tmp_path, monkeypatch):
    from urllib3.exceptions import ConnectTimeoutError
    monkeypatch.setattr(photo.socket, "getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("8.8.8.8", p))])

    def boom(*a, **k):
        raise ConnectTimeoutError(None, "t/o")
    monkeypatch.setattr(photo, "_open", boom)
    ok, why = photo.download_photo("https://img.example.com/a.jpg", tmp_path / "x.png", CFG)
    assert not ok and "ConnectTimeoutError" in why


# ---------- 疊字版型 ----------
def _canvas(path, size=(1080, 1350), color=(200, 220, 255)):
    Image.new("RGB", size, color).save(path)
    return path


def _yellow_box(im):
    px = im.load()
    ys, xs = [], []
    for y in range(im.size[1]):
        for x in range(im.size[0]):
            r, g, b = px[x, y]
            if r > 240 and 190 < g < 235 and b < 80:
                ys.append(y)
                xs.append(x)
    return (min(xs), min(ys), max(xs), max(ys), len(xs)) if xs else None


TWO_LINE = "國安基金啟動第10次護盤 前兩次買了哪些股票？"


def _geometry(title, w=1080, h=1350):
    from PIL import ImageDraw
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    margin = round(w * 0.06)
    font, lines, size = image._layout(draw, title, w - 2 * margin, w, FONT)
    line_h = round(size * 1.3)
    top = h - margin - line_h * len(lines)
    return lines, size, line_h, top


def test_headline_at_most_two_lines_shrinks_before_third():
    from PIL import ImageDraw
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    max_w = 1080 - 2 * round(1080 * 0.06)
    _f, lines, big = image._layout(draw, TWO_LINE, max_w, 1080, FONT)
    assert len(lines) == 2
    t30 = "這是一個很長的新聞標題測試用文字共三十個字左右的內容會縮字嗎"
    _f, lines, size = image._layout(draw, t30, max_w, 1080, FONT)
    assert len(lines) <= 2 and size < big
    t_long = "這是一個非常非常長而且完全沒有任何空白或標點可以斷行的新聞標題用來測試自動縮字與逐字斷行功能是否正常運作"
    _f, lines, size = image._layout(draw, t_long, max_w, 1080, FONT)
    assert len(lines) == 3 and size <= round(1080 * 0.055)  # 最小字級仍放不下才第 3 行


def test_headline_gradient_covers_bottom_40_percent_only(tmp_path):
    p = _canvas(tmp_path / "i.png")
    assert image.add_headline(p, "短標題", CFG) is None
    with Image.open(p) as im:
        assert im.size == (1080, 1350)
        assert im.getpixel((540, 600)) == (200, 220, 255)  # 上 55% 不動
        assert im.getpixel((5, 1349))[0] < 80 and im.getpixel((5, 1349))[2] < 90  # 底部深紫黑
        assert im.getpixel((5, 1349))[2] > im.getpixel((5, 1349))[0]  # 偏紫


def test_headline_yellow_only_in_second_line_rows(tmp_path):
    p = _canvas(tmp_path / "i.png")
    assert image.add_headline(p, TWO_LINE, CFG, highlight="股票") is None
    lines, size, line_h, top = _geometry(TWO_LINE)
    assert len(lines) == 2
    with Image.open(p) as im:
        box = _yellow_box(im.convert("RGB"))
    assert box and box[4] > 200
    pad = size // 2  # 描邊與字形上下伸出
    assert top + line_h - pad <= box[1] and box[3] <= top + 2 * line_h + pad
    # 第一行區域沒有黃色
    with Image.open(p) as im:
        first = im.convert("RGB").crop((0, top - pad, 1080, top + line_h - pad))
        assert _yellow_box(first) is None


def test_headline_highlight_substring_vs_whole_second_line(tmp_path):
    p1, p2, p3 = (_canvas(tmp_path / f"{n}.png") for n in "abc")
    image.add_headline(p1, TWO_LINE, CFG, highlight="股票")      # 第二行內的片段
    image.add_headline(p2, TWO_LINE, CFG, highlight="護盤")      # 在第一行 -> 整個第二行黃
    image.add_headline(p3, TWO_LINE, CFG, highlight="不存在的字")  # 不是子字串 -> 整個第二行黃
    boxes = [_yellow_box(Image.open(p).convert("RGB")) for p in (p1, p2, p3)]
    w1, w2, w3 = (b[2] - b[0] for b in boxes)
    assert w1 < w2 * 0.5 and abs(w2 - w3) < 10
    # 長度不合格（1 字、超過 8 字）也退回整行
    p4, p5 = _canvas(tmp_path / "d.png"), _canvas(tmp_path / "e.png")
    image.add_headline(p4, TWO_LINE, CFG, highlight="股")
    image.add_headline(p5, TWO_LINE, CFG, highlight="前兩次買了哪些股票？你")
    for p in (p4, p5):
        b = _yellow_box(Image.open(p).convert("RGB"))
        assert abs((b[2] - b[0]) - w2) < 10


def test_headline_single_line_has_no_yellow(tmp_path):
    p = _canvas(tmp_path / "i.png")
    assert image.add_headline(p, "台股大漲", CFG, highlight="大漲") is None
    with Image.open(p) as im:
        assert _yellow_box(im.convert("RGB")) is None
        bottom = im.crop((0, 1100, 1080, 1350)).convert("L")
        assert bottom.getextrema()[1] >= 250  # 白字


# ---------- process_topic：original 模式 ----------
def _orig_cfg(fallback=False):
    c = copy.deepcopy(CFG_AI)
    c["image"]["source"] = "original"
    c["image"]["fallback_ai"] = fallback
    return c


def _setup_original(monkeypatch, calls, image_url="https://news.example.com/p.jpg", download_ok=True):
    topic, slot = _setup_topic(monkeypatch, 2, calls)
    real = collect.fetch_article
    monkeypatch.setattr(collect, "fetch_article", lambda url, timeout=20: dict(real(url, timeout), image_url=image_url))
    dl = []

    def fake_dl(url, dest, cfg):
        dl.append(url)
        if not download_ok:
            return False, "HTTP 404"
        Image.new("RGB", (1080, 1350), (90, 140, 90)).save(dest)
        return True, ""
    monkeypatch.setattr(run_daily.photo, "download_photo", fake_dl)
    monkeypatch.setattr(run_daily.imagespec, "make_image", lambda *a, **k: pytest.fail("original 模式不該呼叫 imagespec"))

    def no_ai(*a, **k):
        pytest.fail("original 模式不該呼叫生圖（codex）")
    monkeypatch.setattr(run_daily.image, "generate_image", no_ai)
    monkeypatch.setattr(run_daily.write, "write_draft", _wrap_write(run_daily.write.write_draft))
    return topic, slot, dl


def _wrap_write(orig):
    def w(*a, **k):
        d, e = orig(*a, **k)
        if d is not None:
            d["title_highlight"] = "挑戰大聯盟"
        return d, e
    return w


def test_original_mode_uses_photo_never_ai(tmp_path, monkeypatch):
    calls = []
    topic, slot, dl = _setup_original(monkeypatch, calls)
    post_dir, draft, cap, cited, why = run_daily.process_topic(topic, _orig_cfg(), TERMS, tmp_path, slot)
    assert draft is not None, why
    assert dl == ["https://news.example.com/p.jpg"]
    assert draft["image_kind"] == "original" and publish.DISCLAIMER not in cap
    import json
    art = json.loads((post_dir / "artifact.json").read_text(encoding="utf-8"))
    assert art["image_kind"] == "original" and len(art["image_sha256"]) == 64
    with Image.open(post_dir / "image.png") as im:
        assert im.size == (1080, 1350)
        assert im.getpixel((540, 100)) == (90, 140, 90)  # 疊字只在下方
    assert json.loads((post_dir / "draft.json").read_text(encoding="utf-8"))["image_kind"] == "original"


def test_original_mode_without_photo_skips_topic(tmp_path, monkeypatch):
    calls = []
    topic, slot, dl = _setup_original(monkeypatch, calls, image_url=None)
    writes = []
    orig = run_daily.write.write_draft
    monkeypatch.setattr(run_daily.write, "write_draft", lambda *a, **k: writes.append(1) or orig(*a, **k))
    _d, draft, _c, _ci, why = run_daily.process_topic(topic, _orig_cfg(), TERMS, tmp_path, slot)
    assert draft is None and why.startswith("無可用原圖：") and not dl and not writes


def test_original_mode_download_failure_skips_topic(tmp_path, monkeypatch):
    calls = []
    topic, slot, dl = _setup_original(monkeypatch, calls, download_ok=False)
    _d, draft, _c, _ci, why = run_daily.process_topic(topic, _orig_cfg(), TERMS, tmp_path, slot)
    assert draft is None and "HTTP 404" in why and why.startswith("無可用原圖：") and len(dl) == 1


def test_fallback_ai_true_uses_ai_path_with_disclaimer(tmp_path, monkeypatch):
    calls = []
    topic, slot, dl = _setup_original(monkeypatch, calls, download_ok=False)

    def fake_img(post_dir, prompt, cfg=None, constraints=None):
        calls.append(prompt)
        Image.new("RGB", (400, 500)).save(post_dir / "image.png")
        return post_dir / "image.png", None
    monkeypatch.setattr(run_daily.image, "generate_image", fake_img)
    monkeypatch.setattr(run_daily.imagespec, "make_image", lambda *a, **k: (None, {"spec_error": "test"}))
    post_dir, draft, cap, _ci, why = run_daily.process_topic(topic, _orig_cfg(fallback=True), TERMS, tmp_path, slot)
    assert draft is not None, why
    assert draft["image_kind"] == "ai" and publish.DISCLAIMER in cap
    assert any(c != "gate" for c in calls)


def test_ai_mode_unchanged(tmp_path, monkeypatch):
    calls = []
    topic, slot = _setup_topic(monkeypatch, 2, calls)
    monkeypatch.setattr(run_daily.photo, "download_photo", lambda *a, **k: pytest.fail("ai 模式不該下載原圖"))
    post_dir, draft, cap, _ci, why = run_daily.process_topic(topic, CFG_AI, TERMS, tmp_path, slot)
    assert CFG_AI["image"]["source"] == "ai"
    assert draft is not None, why
    assert draft["image_kind"] == "ai" and publish.DISCLAIMER in cap
    assert any(c != "gate" for c in calls)


def test_config_defaults():
    assert CFG["image"]["source"] == "original"
    assert CFG["image"]["fallback_ai"] is False
    assert CFG["image"]["min_photo_side"] == 400


# ---------- caption / write ----------
DRAFT = {"title": "標題", "body": "正文", "hashtags": ["#新聞"]}


def test_caption_disclaimer_by_image_kind():
    assert publish.DISCLAIMER not in publish.build_caption(dict(DRAFT, image_kind="original"), [])
    assert publish.DISCLAIMER in publish.build_caption(dict(DRAFT, image_kind="ai"), [])
    assert publish.DISCLAIMER in publish.build_caption(DRAFT, [])  # 舊稿沒標 image_kind -> 照舊
    cap = publish.build_caption(dict(DRAFT, image_kind="original"), [])
    assert cap == "標題\n\n正文\n\n#新聞"
    assert "正文" in publish.build_payload(dict(DRAFT, image_kind="original"), [], None)["message"]


def test_write_schema_has_title_highlight_and_validate_not_strict():
    from src import write
    assert "title_highlight" in write.RULES
    d = {"title": "t", "body": "字" * 250, "hashtags": ["#a"], "category": "news", "title_highlight": "任意"}
    assert write.validate_draft(d) is None
    d.pop("title_highlight")
    assert write.validate_draft(d) is None


# ---------- 站方預設圖、多來源逐一嘗試 ----------
@pytest.mark.parametrize("url", [
    "https://imgcdn.cna.com.tw/www/images/pic_fb.jpg", "https://news.ltn.com.tw/assets/images/all/250_ltn.png",
    "https://x.com/IMG/Logo.png", "https://x.com/a/default.jpg", "https://x.com/a/No_Image.jpg"])
def test_placeholder_rejected_before_download(tmp_path, monkeypatch, url):
    monkeypatch.setattr(photo, "_open", lambda *a, **k: pytest.fail("不該發出請求"))
    ok, why = photo.download_photo(url, tmp_path / "x.png", CFG)
    assert not ok and why == "站方預設圖"


def test_placeholder_only_checks_path(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://logo.example.com/news/a.png?x=default": _img_resp()})
    assert photo.download_photo("https://logo.example.com/news/a.png?x=default", tmp_path / "x.png", CFG)[0]


def test_pick_photos_ordered_dedup():
    srcs = [{"ok": True, "image_url": "https://a/1.jpg"}, {"ok": False, "image_url": "https://z/9.jpg"},
            {"ok": True, "image_url": "https://a/1.jpg"}, {"ok": True, "image_url": "https://b/2.jpg"}]
    assert photo.pick_photos(srcs) == ["https://a/1.jpg", "https://b/2.jpg"]


def _two_source_topic(monkeypatch, calls, results):
    topic, slot = _setup_topic(monkeypatch, 2, calls)
    urls = {"https://sports.ltn.com.tw/1": "https://img.ltn.com.tw/a.jpg", "https://www.cna.com.tw/2": "https://imgcdn.cna.com.tw/b.jpg"}
    real = collect.fetch_article
    monkeypatch.setattr(collect, "fetch_article", lambda url, timeout=20: dict(real(url, timeout), image_url=urls[url]))
    tried = []

    def fake_dl(url, dest, cfg):
        tried.append(url)
        ok, why = results[url]
        if ok:
            Image.new("RGB", (1080, 1350), (90, 140, 90)).save(dest)
        return ok, why
    monkeypatch.setattr(run_daily.photo, "download_photo", fake_dl)
    monkeypatch.setattr(run_daily.imagespec, "make_image", lambda *a, **k: pytest.fail("不該呼叫"))
    monkeypatch.setattr(run_daily.image, "generate_image", lambda *a, **k: pytest.fail("不該呼叫"))
    return topic, slot, tried


def test_first_source_fails_uses_second(tmp_path, monkeypatch):
    calls = []
    topic, slot, tried = _two_source_topic(monkeypatch, calls, {
        "https://img.ltn.com.tw/a.jpg": (False, "圖片太小"), "https://imgcdn.cna.com.tw/b.jpg": (True, "")})
    _d, draft, _c, _ci, why = run_daily.process_topic(topic, _orig_cfg(), TERMS, tmp_path, slot)
    assert draft is not None, why
    assert tried == ["https://img.ltn.com.tw/a.jpg", "https://imgcdn.cna.com.tw/b.jpg"]
    assert draft["image_kind"] == "original"


def test_all_sources_fail_reason_lists_each_and_capped(tmp_path, monkeypatch):
    calls = []
    topic, slot, tried = _two_source_topic(monkeypatch, calls, {
        "https://img.ltn.com.tw/a.jpg": (False, "站方預設圖"), "https://imgcdn.cna.com.tw/b.jpg": (False, "HTTP 404")})
    _d, draft, _c, _ci, why = run_daily.process_topic(topic, _orig_cfg(), TERMS, tmp_path, slot)
    assert draft is None and why.startswith("無可用原圖：")
    assert "ltn.com.tw：站方預設圖" in why and "cna.com.tw：HTTP 404" in why
    assert len(why) <= len("無可用原圖：") + 200


# ---------- 行尾標點、底條、智慧裁切 ----------
def _tex(im, box):
    import random
    rnd = random.Random(1)
    px = im.load()
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            px[x, y] = (255, 255, 255) if rnd.random() < 0.5 else (0, 0, 0)


def test_trailing_comma_stripped_and_highlight_ok(tmp_path):
    t = "明明擔心貓咪獨自在家， 打開鏡頭竟然看到牠這樣做"
    lines, size, line_h, top = _geometry(t)
    assert len(lines) == 2 and lines[0].endswith("，")
    p, q = _canvas(tmp_path / "a.png"), _canvas(tmp_path / "b.png")
    assert image.add_headline(p, t, CFG, highlight="竟然看到牠") is None
    assert image.add_headline(q, t.replace("， ", " "), CFG, highlight="竟然看到牠") is None
    # 逗號被拿掉：第一行的繪製範圍與「本來就沒有逗號」的版本相同
    a, b = Image.open(p).convert("L"), Image.open(q).convert("L")
    row = (0, top - 5, 1080, top + line_h - 5)
    assert a.crop(row).getbbox() == b.crop(row).getbbox()
    f = _canvas(tmp_path / "f.png")
    image.add_headline(f, t, CFG, highlight="不存在")
    box, full = _yellow_box(Image.open(p).convert("RGB")), _yellow_box(Image.open(f).convert("RGB"))
    assert box and box[2] - box[0] < (full[2] - full[0]) * 0.8  # 只有片段上黃色


def test_question_mark_kept_on_line_end(tmp_path):
    p, q = _canvas(tmp_path / "a.png"), _canvas(tmp_path / "b.png")
    image.add_headline(p, TWO_LINE, CFG)
    image.add_headline(q, TWO_LINE.rstrip("？"), CFG)
    assert Image.open(p).convert("RGB").tobytes() != Image.open(q).convert("RGB").tobytes()


def test_shade_alpha_at_least_230_in_text_zone(tmp_path):
    p = _canvas(tmp_path / "w.png", color=(255, 255, 255))
    image.add_headline(p, "短標題", CFG)
    with Image.open(p) as im:
        shade_top = 1350 - round(1350 * 0.4)
        for y in range(shade_top + round(0.3 * 540) + 5, 1350, 40):
            r = im.getpixel((3, y))[0]  # 白底：out = 255 - a*(255-20)
            assert (255 - r) / 235 * 255 >= 230, (y, r)
        assert im.getpixel((3, shade_top + 2))[0] > 240  # 上緣仍是柔和過渡


def test_smart_crop_prefers_subject_on_left():
    im = Image.new("RGB", (2000, 1000), (120, 120, 120))
    _tex(im, (50, 300, 350, 700))
    x0, y0, x1, y1 = photo._crop_box(im)
    assert (y0, y1) == (0, 1000) and x1 - x0 == 800 and x0 <= 100 and x1 >= 350
    right = Image.new("RGB", (2000, 1000), (120, 120, 120))
    _tex(right, (1650, 300, 1950, 700))
    assert photo._crop_box(right)[0] >= 1100


def test_smart_crop_portrait_prefers_top_and_flat_is_centered():
    im = Image.new("RGB", (1000, 2000), (120, 120, 120))
    _tex(im, (350, 100, 650, 400))
    assert photo._crop_box(im)[1] <= 100
    both = Image.new("RGB", (1000, 2400), (120, 120, 120))
    _tex(both, (350, 100, 650, 400))
    _tex(both, (350, 2000, 650, 2300))
    assert photo._crop_box(both)[1] < 600  # 同樣大的主體，取上方
    flat = Image.new("RGB", (1600, 900), (10, 200, 30))
    assert photo._crop_box(flat) == (440, 0, 1160, 900)
    flatp = Image.new("RGB", (1000, 1800), (10, 200, 30))
    assert photo._crop_box(flatp) == (0, 275, 1000, 1525)


def test_download_left_subject_end_to_end(tmp_path, monkeypatch):
    im = Image.new("RGB", (2000, 1000), (120, 120, 120))
    _tex(im, (50, 300, 350, 700))
    b = io.BytesIO()
    im.save(b, format="PNG")
    _net(monkeypatch, {"https://img.example.com/l.png": _img_resp(b.getvalue())})
    dest = tmp_path / "l.png"
    assert photo.download_photo("https://img.example.com/l.png", dest, CFG)[0]
    with Image.open(dest) as o:
        assert o.convert("L").crop((0, 0, 540, 1350)).getextrema() == (0, 255)  # 左半有主體紋理


# ---------- 審查後補強：URL 歧義、DNS rebinding、CGNAT、總時限、格式白名單 ----------
@pytest.mark.parametrize("url", [
    "http://127.0.0.1\\@8.8.8.8/a.png", "http://8.8.8.8\\.evil.com/a.png", "http://user:pw@8.8.8.8/a.png",
    "http://8.8.8.8@127.0.0.1/a.png", "http://8.8.8.8/a b.png", "http://8.8.8.8/a\nb.png", "http://8.8.8.8/a\x00.png"])
def test_ambiguous_urls_rejected_without_connecting(tmp_path, monkeypatch, url):
    monkeypatch.setattr(photo.socket, "getaddrinfo", lambda *a, **k: pytest.fail("不該解析"))
    monkeypatch.setattr(photo, "_open", lambda *a, **k: pytest.fail("不該發出請求"))
    ok, why = photo.download_photo(url, tmp_path / "x.png", CFG)
    assert not ok and "不允許的字元" in why


def test_redirect_location_with_backslash_rejected(tmp_path, monkeypatch):
    _net(monkeypatch, {"https://img.example.com/r": Resp(302, {"Location": "http://127.0.0.1\\@8.8.8.8/a.png"})})
    ok, why = photo.download_photo("https://img.example.com/r", tmp_path / "x.png", CFG)
    assert not ok and "不允許的字元" in why


def test_cgnat_and_mapped_rejected_via_dns(tmp_path, monkeypatch):
    for ip in ("100.64.0.1", "100.127.255.254", "::ffff:127.0.0.1", "fe80::1", "fc00::1", "198.18.0.1"):
        _net(monkeypatch, {}, dns={"h.example.com": ip})
        ok, why = photo.download_photo("https://h.example.com/a.jpg", tmp_path / "x.png", CFG)
        assert not ok and "非公網" in why, ip


def test_dns_rebinding_connection_pinned_to_validated_ip(tmp_path, monkeypatch):
    answers = iter(["8.8.8.8", "127.0.0.1", "127.0.0.1", "127.0.0.1"])
    resolved = []

    def gai(host, port, *a, **k):
        ip = next(answers)
        resolved.append(ip)
        return [(2, 1, 6, "", (ip, port))]
    connects = []

    def opener(scheme, ip, port, host, target, timeout):
        connects.append((ip, host))
        return _img_resp()
    monkeypatch.setattr(photo.socket, "getaddrinfo", gai)
    monkeypatch.setattr(photo, "_open", opener)
    assert photo.download_photo("https://rebind.example.com/a.png", tmp_path / "x.png", CFG)[0]
    assert connects == [("8.8.8.8", "rebind.example.com")]  # 連的是第一次驗證過的 IP，不是 127.0.0.1
    assert resolved == ["8.8.8.8"]  # 每跳只解析一次，連線不再讓 OS 重新解析


def test_redirect_hop_revalidated_and_pinned(tmp_path, monkeypatch):
    answers = {"a.example.com": "8.8.8.8", "b.example.com": "9.9.9.9"}
    connects = []

    def opener(scheme, ip, port, host, target, timeout):
        connects.append((ip, host))
        return Resp(302, {"Location": "https://b.example.com/x"}) if host == "a.example.com" else _img_resp()
    monkeypatch.setattr(photo.socket, "getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", (answers[h], p))])
    monkeypatch.setattr(photo, "_open", opener)
    assert photo.download_photo("https://a.example.com/1", tmp_path / "x.png", CFG)[0]
    assert connects == [("8.8.8.8", "a.example.com"), ("9.9.9.9", "b.example.com")]


def test_open_pins_ip_and_sends_host_header(monkeypatch):
    seen = {}

    class FakePool:
        def __init__(self, host, port, **kw):
            seen.update(host=host, port=port, kw=kw)

        def urlopen(self, method, target, **kw):
            seen.update(method=method, target=target, kw2=kw)
            raise RuntimeError("stop")

        def close(self):
            pass
    monkeypatch.setattr(photo.urllib3, "HTTPSConnectionPool", FakePool)
    with pytest.raises(RuntimeError):
        photo._open("https", "8.8.8.8", 443, "img.example.com", "/a.png?x=1", 20)
    assert seen["host"] == "8.8.8.8" and seen["target"] == "/a.png?x=1"
    assert seen["kw"]["server_hostname"] == "img.example.com" and seen["kw"]["assert_hostname"] == "img.example.com"
    assert seen["kw"]["cert_reqs"] == "CERT_REQUIRED" and seen["kw"]["retries"] is False and seen["kw"]["ca_certs"]
    assert seen["kw2"]["headers"]["Host"] == "img.example.com" and seen["kw2"]["redirect"] is False
    assert seen["kw2"]["preload_content"] is False and seen["kw2"]["retries"] is False


def test_slow_stream_times_out(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(photo.time, "monotonic", lambda: clock["t"])

    def slow():
        for _ in range(100):
            clock["t"] += 10.0  # 每塊慢 10 秒；timeout=20 -> 總時限 40 秒
            yield b"\0" * 10
    resp = Resp(200, {"Content-Type": "image/png"}, chunks=slow())
    _net(monkeypatch, {"https://img.example.com/s": resp})
    ok, why = photo.download_photo("https://img.example.com/s", tmp_path / "x.png", CFG)
    assert not ok and why == "下載逾時" and resp.closed and clock["t"] <= 60


def test_format_whitelist_rejects_eps_accepts_jpeg_webp(tmp_path, monkeypatch):
    b = io.BytesIO()
    Image.new("RGB", (800, 800), (5, 5, 5)).save(b, format="EPS")
    _net(monkeypatch, {"https://img.example.com/e": _img_resp(b.getvalue())})
    ok, why = photo.download_photo("https://img.example.com/e", tmp_path / "x.png", CFG)
    assert not ok and "無法解碼" in why
    for fmt in ("JPEG", "WEBP"):
        b = io.BytesIO()
        Image.new("RGB", (800, 800), (90, 90, 90)).save(b, format=fmt)
        _net(monkeypatch, {"https://img.example.com/f": _img_resp(b.getvalue(), "image/" + fmt.lower())})
        assert photo.download_photo("https://img.example.com/f", tmp_path / (fmt + ".png"), CFG)[0]


# ---------- humanjudge、rank 預算、審稿隔離、發佈 payload ----------
def test_judge_never_raises(monkeypatch):
    from src import humanjudge
    monkeypatch.setattr(humanjudge.shutil, "which", lambda n: "claude")
    deep = "[" * 5000 + "]" * 5000
    monkeypatch.setattr(humanjudge, "run_cmd", lambda *a, **k: (0, deep, ""))
    assert humanjudge.judge(["a", "b"], CFG) is None  # RecursionError 也只回 None

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(humanjudge, "run_cmd", boom)
    assert humanjudge.judge(["a"], CFG) is None
    monkeypatch.setattr(humanjudge, "run_cmd", lambda *a, **k: (0, "9" * 20001, ""))
    assert humanjudge.judge(["a"], CFG) is None
    monkeypatch.setattr(humanjudge, "run_cmd", lambda *a, **k: (0, '{"scores":[1]}', ""))
    monkeypatch.setattr(humanjudge, "parse_scores", lambda *a: (_ for _ in ()).throw(RecursionError()))
    assert humanjudge.judge(["a"], CFG) is None


def test_conftest_stub_is_at_run_cmd_level(monkeypatch):
    from src import humanjudge
    monkeypatch.setattr(humanjudge.shutil, "which", lambda n: "claude")
    assert humanjudge.judge(["a"], CFG) is None  # stub 的 run_cmd 回失敗，沒有真的開 claude


def _review_cfg():
    cfg = _orig_cfg()
    cfg["review"]["ai_review"] = True
    return cfg


def _all_files(root):
    return [x for x in root.rglob("*") if x.is_file()]


def test_review_runs_without_any_image_copy_on_disk_then_restores(tmp_path, monkeypatch):
    import hashlib
    calls = []
    topic, slot, dl = _setup_original(monkeypatch, calls)
    seen = {}

    def fake_review(post_dir, *a, **k):
        seen["post"] = sorted(x.name for x in post_dir.iterdir())
        seen["all"] = [str(x) for x in _all_files(tmp_path)]
        seen["pngs"] = [x for x in _all_files(tmp_path) if x.read_bytes()[:4] == b"\x89PNG"]
        return True, "pass", {"_cited_urls": [s["url"] for s in topic["sources"]]}
    monkeypatch.setattr(run_daily.review, "review", fake_review)
    post_dir, draft, cap, _ci, why = run_daily.process_topic(topic, _review_cfg(), TERMS, tmp_path, slot)
    assert draft is not None, why
    assert "image.png" not in seen["post"] and not seen["pngs"]  # post_dir 與其父目錄（tmp_path）都沒有圖
    assert not [f for f in seen["all"] if "hold" in f]
    import json
    art = json.loads((post_dir / "artifact.json").read_text(encoding="utf-8"))
    assert hashlib.sha256((post_dir / "image.png").read_bytes()).hexdigest() == art["image_sha256"]


def test_review_exception_still_restores_image_with_same_hash(tmp_path, monkeypatch):
    import hashlib
    calls = []
    topic, slot, dl = _setup_original(monkeypatch, calls)
    holder = {}

    def boom(post_dir, *a, **k):
        holder["dir"] = post_dir
        assert not (post_dir / "image.png").exists()
        raise RuntimeError("審稿炸了")
    monkeypatch.setattr(run_daily.review, "review", boom)
    before = {}
    real_unlink = run_daily.Path.unlink

    def spy_unlink(self, *a, **k):
        if self.name == "image.png" and "bytes" not in before:
            before["bytes"] = self.read_bytes()
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(run_daily.Path, "unlink", spy_unlink)
    with pytest.raises(RuntimeError, match="審稿炸了"):
        run_daily.process_topic(topic, _review_cfg(), TERMS, tmp_path, slot)
    img = holder["dir"] / "image.png"
    assert img.exists()
    assert hashlib.sha256(img.read_bytes()).hexdigest() == hashlib.sha256(before["bytes"]).hexdigest()
    assert not [f for f in _all_files(tmp_path) if "hold" in f.name]


def test_photo_tries_budget_checked_each_try_and_capped(tmp_path, monkeypatch):
    calls = []
    topic, slot, dl = _setup_original(monkeypatch, calls, download_ok=False)
    cfg = _orig_cfg()
    cfg["image"]["max_photo_tries"] = 1
    budgets = []
    real = run_daily.ensure_budget

    def spy(sec, step):
        if step == "下載原圖":
            budgets.append(sec)
        return real(sec, step)
    monkeypatch.setattr(run_daily, "ensure_budget", spy)
    run_daily.process_topic(topic, cfg, TERMS, tmp_path, slot)
    assert len(dl) == 1 and budgets == [2 * cfg["timeouts"]["http"]]


def test_rank_budget_shortage_disables_llm_judge_not_run(tmp_path, monkeypatch):
    from tests.test_pipeline import _patch_env, _topics
    _patch_env(monkeypatch, tmp_path, _topics(1))
    got = {}
    real = run_daily.rank.rank

    def spy(cands, con, cfg, now):
        got["judge"] = cfg["rank"].get("llm_judge")
        return real(cands, con, cfg, now)
    monkeypatch.setattr(run_daily.rank, "rank", spy)

    def short(seconds, step):
        if step == "LLM 評分":
            raise run_daily.DeadlineReached("x")
    monkeypatch.setattr(run_daily, "ensure_budget", short)
    assert run_daily.main(["--dry-run", "--max", "1"]) == 0
    assert got["judge"] is False


def test_main_original_post_payload_has_no_disclaimer(tmp_path, monkeypatch):
    from tests.test_pipeline import _fake_process, _patch_env, _topics
    _patch_env(monkeypatch, tmp_path, _topics(1))
    fake = _fake_process(tmp_path, [])

    def proc(t, c, te, root, s):
        r = fake(t, c, te, root, s)
        r[1]["image_kind"] = "original"
        return r
    monkeypatch.setattr(run_daily, "process_topic", proc)
    sent = []
    real = run_daily.publish.build_payload

    def spy(d, c, s):
        p = real(d, c, s)
        sent.append(p)
        return p
    monkeypatch.setattr(run_daily.publish, "build_payload", spy)
    assert run_daily.main(["--dry-run", "--max", "1"]) == 0
    assert sent and publish.DISCLAIMER not in sent[0]["message"]


# ---------- 真 socket 的總時限、IP 轉換位址、多 IP 依序嘗試 ----------
def test_total_deadline_enforced_on_real_socket(monkeypatch):
    import socket as sk
    import threading
    import time as t
    srv = sk.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        c.recv(4096)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n")
        try:
            for _ in range(60):
                c.sendall(b"x")
                t.sleep(0.3)
        except Exception:
            pass
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(photo, "_resolve", lambda host, port: (["127.0.0.1"], ""))  # 測試用：視為已驗證
    t0 = t.monotonic()
    data, why = photo._fetch_bytes(f"http://127.0.0.1:{port}/a.png", 1)
    elapsed = t.monotonic() - t0
    srv.close()
    assert data is None and why == "下載逾時"
    assert elapsed <= 2 + 0.5, elapsed


@pytest.mark.parametrize("ip", ["64:ff9b::7f00:1", "64:ff9b:1::1", "::7f00:1", "2002:7f00:1::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2"])
def test_ipv6_transition_addresses_rejected(ip):
    assert photo._ip_problem(ip)
    assert photo._ip_problem("8.8.8.8") is None and photo._ip_problem("2606:4700:4700::1111") is None


def test_ips_tried_in_order_and_any_bad_ip_rejects_host(tmp_path, monkeypatch):
    monkeypatch.setattr(photo.socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(2, 1, 6, "", ("8.8.8.8", p)), (2, 1, 6, "", ("9.9.9.9", p))])
    tried = []

    def opener(scheme, ip, port, host, target, timeout):
        tried.append(ip)
        if ip == "8.8.8.8":
            raise OSError("connect refused")
        return _img_resp()
    monkeypatch.setattr(photo, "_open", opener)
    assert photo.download_photo("https://multi.example.com/a.png", tmp_path / "x.png", CFG)[0]
    assert tried == ["8.8.8.8", "9.9.9.9"]
    monkeypatch.setattr(photo.socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(2, 1, 6, "", ("8.8.8.8", p)), (2, 1, 6, "", ("10.0.0.1", p))])
    tried.clear()
    ok, why = photo.download_photo("https://multi.example.com/a.png", tmp_path / "y.png", CFG)
    assert not ok and "非公網" in why and not tried
    monkeypatch.setattr(photo.socket, "getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("8.8.8.8", p))])
    monkeypatch.setattr(photo, "_open", lambda *a, **k: (_ for _ in ()).throw(OSError("all down")))
    ok, why = photo.download_photo("https://multi.example.com/a.png", tmp_path / "z.png", CFG)
    assert not ok and "OSError" in why


def test_open_closes_pool_on_base_exception(monkeypatch):
    closed = []

    class FakePool:
        def __init__(self, *a, **k):
            pass

        def urlopen(self, *a, **k):
            raise KeyboardInterrupt()

        def close(self):
            closed.append(1)
    monkeypatch.setattr(photo.urllib3, "HTTPConnectionPool", FakePool)
    with pytest.raises(KeyboardInterrupt):
        photo._open("http", "8.8.8.8", 80, "img.example.com", "/a", 5)
    assert closed == [1]
