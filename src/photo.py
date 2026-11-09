"""新聞原圖：取來源頁 og:image 並下載。2026-11-10 使用者裁示：配圖改用原文照片（不標示來源）。

安全：圖片網址來自不可信的網頁，所以下載前一律做 SSRF 防護——只允許 http/https、主機解析出的每個 IP 都必須
是公網位址、不自動跟轉址（自己跟、最多 3 跳、每跳重檢）、限制大小與 Content-Type、Pillow 解碼有像素上限。
圖片與網址都不會傳給 codex 或 claude。
"""
from __future__ import annotations

import io
import ipaddress
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import urllib3
from PIL import Image

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AutoNews/0.1"}
MAX_BYTES = 10 * 1024 * 1024
MAX_HOPS = 3
MAX_PIXELS = 40_000_000
_FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]  # 白名單：不解 EPS、PDF、TIFF 等複雜格式
OUT_W, OUT_H = 1080, 1350  # 4:5


def pick_photo(sources: list[dict]) -> str | None:
    """依序回第一個合格（ok）且有 image_url 的來源圖片網址。"""
    for s in sources:
        if s.get("ok") and s.get("image_url"):
            return s["image_url"]
    return None


def pick_photos(sources: list[dict]) -> list[str]:
    """依序回所有合格（ok）來源的 image_url（去重），逐一嘗試下載用。"""
    out: list[str] = []
    for s in sources:
        u = s.get("image_url")
        if s.get("ok") and u and u not in out:
            out.append(u)
    return out


def placeholder_problem(url: str, cfg: dict) -> str | None:
    """URL path 含站方預設圖特徵字串（logo、pic_fb…）就拒用。只看 path，不分大小寫。"""
    path = urlparse(url).path.lower()
    for pat in cfg["image"].get("photo_placeholder_patterns", []):
        if pat and pat.lower() in path:
            return "站方預設圖"
    return None


_BLOCKED_V6 = [ipaddress.ip_network(n) for n in
               ("64:ff9b::/96", "64:ff9b:1::/48", "::/96", "2002::/16", "2001::/32")]


def _ip_problem(addr: str) -> str | None:
    """白名單式：只有全球可路由的公網位址才放行（擋 CGNAT 100.64/10、私網、loopback、link-local、保留、多播）。
    IPv4-mapped IPv6 先轉回 IPv4 再判斷。"""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return "主機位址格式異常"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast or ip.is_unspecified:
        return f"主機解析到非公網位址：{ip}"
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in n for n in _BLOCKED_V6):
        return f"主機解析到可內嵌 IPv4 的轉換位址：{ip}"  # NAT64、IPv4-compatible、6to4、Teredo
    return None


def _parse(url: str):
    """檢查和連線共用同一份解析結果（urllib3 的解析器，與 requests 實際使用的一致）。
    回 (Url, 原因)。含反斜線、userinfo（@）、控制字元或空白的網址一律拒絕，避免解析器之間的歧義。"""
    if not isinstance(url, str) or not url:
        return None, "網址是空的"
    if any(c in url for c in ("\\", "@")) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return None, "網址含不允許的字元（反斜線、@、控制字元或空白）"
    try:
        p = urllib3.util.parse_url(url)
    except Exception:  # noqa: BLE001
        return None, "網址無法解析"
    if (p.scheme or "").lower() not in ("http", "https"):
        return None, f"不允許的網址協定：{p.scheme or '空'}"
    if not p.host:
        return None, "網址沒有主機名稱"
    return p, None


def _resolve(host: str, port: int) -> tuple[list[str], str]:
    """解析並驗證：所有位址都必須是公網，任何一個不合格整個主機就拒絕；回已驗證 IP 清單（依序嘗試連線）。"""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        infos = socket.getaddrinfo(bare, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return [], f"主機解析失敗：{e}"
    if not infos:
        return [], "主機解析不到位址"
    ips: list[str] = []
    for info in infos:
        bad = _ip_problem(info[4][0])
        if bad:
            return [], bad
        ip = info[4][0].split("%")[0]
        if ip not in ips:
            ips.append(ip)
    return ips, ""


class _Resp:
    def __init__(self, r, pool):
        self._r, self._pool = r, pool
        self.status_code, self.headers = r.status, r.headers

    def iter_content(self, n):
        yield from self._r.stream(n)

    def abort(self):
        """從別的執行緒強制中斷：shutdown socket 讓阻塞中的讀取立刻結束（watchdog 用）。"""
        try:
            sock = getattr(getattr(self._r, "connection", None), "sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass
        self.close()

    def close(self):
        try:
            self._r.close()
        finally:
            self._pool.close()


def _open(scheme: str, ip: str, port: int, host: str, target: str, timeout: float):
    """連到『已驗證的 IP』（不再讓 OS 重新解析主機名稱，擋 DNS rebinding）。HTTPS 仍以原主機名稱做 SNI 與憑證驗證。"""
    import certifi
    default = 443 if scheme == "https" else 80
    headers = dict(UA, Host=host if port == default else f"{host}:{port}")
    t = urllib3.Timeout(connect=timeout, read=timeout)
    if scheme == "https":
        bare = host[1:-1] if host.startswith("[") else host
        pool = urllib3.HTTPSConnectionPool(ip, port, timeout=t, retries=False, cert_reqs="CERT_REQUIRED",
                                           ca_certs=certifi.where(), server_hostname=bare, assert_hostname=bare)
    else:
        pool = urllib3.HTTPConnectionPool(ip, port, timeout=t, retries=False)
    try:
        r = pool.urlopen("GET", target, headers=headers, redirect=False, retries=False, preload_content=False,
                         assert_same_host=False)
    except BaseException:
        pool.close()
        raise
    return _Resp(r, pool)


def _fetch_bytes(url: str, timeout: int) -> tuple[bytes | None, str]:
    """手動跟轉址、每跳重新解析＋驗證＋釘住 IP、串流限量。回 (bytes, "") 或 (None, 原因)。
    總時限 2 倍 timeout（從解析算起）：每跳都有 watchdog 計時器，時間到就主動中斷連線，
    不靠 chunk 之間檢查（stream() 會阻塞到湊滿一個 chunk）。"""
    deadline = time.monotonic() + 2 * timeout
    for _hop in range(MAX_HOPS + 1):
        p, bad = _parse(url)
        if bad:
            return None, bad
        scheme = p.scheme.lower()
        port = p.port or (443 if scheme == "https" else 80)
        ips, bad = _resolve(p.host, port)
        if bad:
            return None, bad
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "下載逾時"
        state = {"expired": False, "r": None}

        def fire(state=state):
            state["expired"] = True
            r0 = state["r"]
            if r0 is not None:
                getattr(r0, "abort", r0.close)()
        timer = threading.Timer(remaining, fire)
        timer.daemon = True
        timer.start()
        r = None
        try:
            last: BaseException | None = None
            for ip in ips:  # 依序試每個已驗證的 IP；前一個連不上才換下一個
                try:
                    r = _open(scheme, ip, port, p.host, p.request_uri, min(timeout, max(0.1, deadline - time.monotonic())))
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    if state["expired"] or time.monotonic() > deadline:
                        return None, "下載逾時"
            if r is None:
                raise last  # type: ignore[misc]
            state["r"] = r
            if state["expired"]:
                return None, "下載逾時"
            try:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if not loc:
                        return None, "轉址沒有 Location"
                    url = urljoin(url, loc)
                    continue
                if r.status_code != 200:
                    return None, f"HTTP {r.status_code}"
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    return None, f"Content-Type 不是圖片：{ctype or '空'}"
                clen = r.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > MAX_BYTES:
                    return None, "圖片超過 10MB"
                buf = bytearray()
                try:
                    for chunk in r.iter_content(65536):
                        buf += chunk
                        if len(buf) > MAX_BYTES:
                            return None, "圖片超過 10MB"
                        if state["expired"] or time.monotonic() > deadline:
                            return None, "下載逾時"
                except Exception:  # noqa: BLE001
                    if state["expired"]:
                        return None, "下載逾時"
                    raise
                if state["expired"]:  # 連線被中斷時串流可能只是提早結束，不是正常讀完
                    return None, "下載逾時"
                return bytes(buf), ""
            finally:
                r.close()
        finally:
            timer.cancel()
    return None, f"轉址超過 {MAX_HOPS} 跳"


def _best_window(energy: list[float], win: int) -> int:
    """滑動視窗取能量和最大的起點；能量全平或並列時取最接近置中的位置。"""
    n = len(energy)
    win = max(1, min(win, n))
    pre = [0.0]
    for e in energy:
        pre.append(pre[-1] + e)
    sums = [pre[i + win] - pre[i] for i in range(n - win + 1)]
    best = max(sums)
    if best <= 0:
        return (n - win) // 2
    center = (n - win) / 2
    return min((i for i, v in enumerate(sums) if v >= best - 1e-9), key=lambda i: abs(i - center))


def _crop_box(im) -> tuple[int, int, int, int]:
    """4:5 智慧裁切框。橫圖：水平偏移中邊緣能量最大者；直圖：垂直方向同理但上方權重較高（臉通常在上）；
    能量全平（純色）退回置中。"""
    from PIL import ImageFilter
    w, h = im.size
    target = OUT_W / OUT_H
    scale = max(w, h) / 256 if max(w, h) > 256 else 1.0
    sw, sh = max(1, round(w / scale)), max(1, round(h / scale))
    edges = im.convert("L").resize((sw, sh), Image.BILINEAR).filter(ImageFilter.FIND_EDGES)
    if sw > 2 and sh > 2:  # FIND_EDGES 的 1 像素邊框不是真的邊緣（純色圖也會有值）
        edges = edges.crop((1, 1, sw - 1, sh - 1))
        sw, sh = sw - 2, sh - 2
    if w / h > target:  # 太寬：裁左右
        nw = round(h * target)
        cols = list(edges.resize((sw, 1), Image.BOX).tobytes())  # 每欄平均邊緣能量
        x = (w - nw) // 2 if max(cols) <= 0 else min(max(0, round(_best_window(cols, round(nw / scale)) * scale)), w - nw)
        return (x, 0, x + nw, h)
    nh = round(w / target)
    rows = list(edges.resize((1, sh), Image.BOX).tobytes())
    rows = [e * (1 - 0.5 * i / max(1, sh - 1)) for i, e in enumerate(rows)]  # 由上往下遞減：偏好上方
    y = (h - nh) // 2 if max(rows) <= 0 else min(max(0, round(_best_window(rows, round(nh / scale)) * scale)), max(0, h - nh))
    return (0, y, w, y + nh)


def _to_poster(data: bytes, min_side: int):
    """驗證、轉 RGB、智慧 cover 裁成 4:5 並縮到 1080x1350。回 (Image, "") 或 (None, 原因)。"""
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as im:
            if im.size[0] * im.size[1] > MAX_PIXELS:
                return None, f"圖片像素過多：{im.size[0]}x{im.size[1]}"
            im.verify()
        im = Image.open(io.BytesIO(data), formats=_FORMATS)
        im.load()
        im = im.convert("RGB")
    except Exception as e:  # noqa: BLE001 - 壞圖、炸彈圖都算失敗，不丟例外
        return None, f"圖片無法解碼：{type(e).__name__}"
    w, h = im.size
    if min(w, h) < min_side:
        return None, f"圖片太小：{w}x{h}（短邊需 >= {min_side}）"
    box = _crop_box(im)
    return im.crop(box).resize((OUT_W, OUT_H), Image.LANCZOS), ""


def download_photo(url: str, dest: Path, cfg: dict) -> tuple[bool, str]:
    """下載原圖並存成 1080x1350 PNG（不帶 EXIF）。失敗不丟例外，回 (False, 原因)；失敗時不留下 dest。"""
    try:
        bad = placeholder_problem(url, cfg)
        if bad:
            return False, bad
        data, why = _fetch_bytes(url, cfg["timeouts"]["http"])
        if data is None:
            return False, why
        im, why = _to_poster(data, int(cfg["image"].get("min_photo_side", 400)))
        if im is None:
            return False, why
        dest = Path(dest)
        im.save(dest, format="PNG")
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:120]}"
