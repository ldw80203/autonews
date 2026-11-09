"""發佈到 FB 粉專：POST /{page_id}/photos（multipart）。dry-run 不打 API，只寫 payload 預覽。

用法：
  python -m src.publish --test <image.png>   # 發一則 published=false、不排程的圖，再 DELETE
憑證只用 dotenv_values() 局部讀取（不寫進 os.environ），放在 Authorization header。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import dotenv_values

from .common import ROOT, load_config, redact

DISCLAIMER = "（示意圖／AI生成，僅作為新聞說明輔助使用）"
GRAPH = "https://graph.facebook.com"


class PublishFailed(RuntimeError):
    """FB 明確拒絕（確定沒發出去），可安全釋出 slot。"""


class PublishUnknown(RuntimeError):
    """結果不明（逾時、連線中斷、5xx、回應異常）：貼文可能已被 FB 收下，需人工對帳，不得重用 slot。"""


def build_caption(draft: dict, source_urls: list[str]) -> str:
    """source_urls 保留參數相容；2026-11-09 使用者裁示：貼文不再列「來源：網址」行（來源網址仍記在 DB 與 post 目錄）。"""
    parts = [draft["title"].strip(), "", draft["body"].strip(), "", DISCLAIMER]
    if draft.get("hashtags"):
        parts += ["", " ".join(draft["hashtags"])]
    return "\n".join(parts)


def build_payload(draft: dict, source_urls: list[str], slot: datetime | None) -> dict:
    """要送出的表單欄位（不含 token、不含圖檔本體）。"""
    p = {"message": build_caption(draft, source_urls), "published": "false"}
    if slot is not None:
        p["scheduled_publish_time"] = str(int(slot.timestamp()))
        p["_slot_local"] = slot.strftime("%Y-%m-%d %H:%M")
    return p


def _creds() -> tuple[str, str, str]:
    """局部讀取，不污染 os.environ。優先 .env，其次系統環境變數。"""
    env = dotenv_values(ROOT / ".env")
    pid = env.get("FB_PAGE_ID") or os.environ.get("FB_PAGE_ID", "")
    tok = env.get("FB_PAGE_TOKEN") or os.environ.get("FB_PAGE_TOKEN", "")
    ver = env.get("FB_GRAPH_VERSION") or os.environ.get("FB_GRAPH_VERSION") or "v27.0"
    if not pid or not tok:
        raise PublishFailed("缺少 FB_PAGE_ID / FB_PAGE_TOKEN（請填 .env，見 .env.example）")
    return pid, tok, ver


def _scrub(e: Exception, tok: str) -> str:
    return redact(f"{type(e).__name__}: {e}", (tok,))


def post_photo(image_path: Path, payload: dict, cfg: dict | None = None) -> str:
    """送出並回傳 FB id。明確拒絕丟 PublishFailed；結果不明丟 PublishUnknown。"""
    pid, tok, ver = _creds()
    data = {k: v for k, v in payload.items() if not k.startswith("_")}
    try:
        with open(image_path, "rb") as f:
            r = requests.post(f"{GRAPH}/{ver}/{pid}/photos", data=data, files={"source": f},
                              headers={"Authorization": f"Bearer {tok}"}, timeout=120)
    except requests.RequestException as e:
        raise PublishUnknown(_scrub(e, tok)) from None
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code == 200 and isinstance(j, dict) and "id" in j:
        return str(j["id"])
    err = redact((j.get("error") or {}).get("message", r.text[:200]) if isinstance(j, dict) else r.text[:200], (tok,))
    if 400 <= r.status_code < 500:
        raise PublishFailed(f"FB 拒絕 HTTP {r.status_code}: {err}")
    raise PublishUnknown(f"FB 回應不明 HTTP {r.status_code}: {err}")


def delete_post(fb_id: str) -> None:
    _pid, tok, ver = _creds()
    try:
        r = requests.delete(f"{GRAPH}/{ver}/{fb_id}", headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        raise PublishFailed(_scrub(e, tok)) from None
    if r.status_code != 200 or not (isinstance(j, dict) and j.get("success") is True):
        raise PublishFailed(f"FB 刪除未確認成功 HTTP {r.status_code}: {redact(r.text[:200], (tok,))}")


def test_roundtrip(image_path: Path) -> str:
    """P0 驗證：published=false、不排程，拿到 id 後立刻刪除。刪除失敗時錯誤訊息帶 fb_id。"""
    fb_id = post_photo(image_path, {"message": "autonews connection test (unpublished)", "published": "false"})
    try:
        delete_post(fb_id)
    except PublishFailed as e:
        raise PublishFailed(f"測試貼文已建立但刪除失敗，請手動清除 fb_id={fb_id}：{e}") from None
    return fb_id


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        try:
            print("OK id=" + test_roundtrip(Path(sys.argv[2])) + "（已刪除）")
        except (PublishFailed, PublishUnknown) as e:
            print(f"FAIL {redact(str(e))}")
            sys.exit(1)
    else:
        print(__doc__)
        sys.exit(2)
