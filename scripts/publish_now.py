"""把 DB 裡已排程的貼文改成立即公開：用同一份 payload/圖片發一篇 published=true，
確認公開後刪除原排程貼文，DB 換成新 fb_id 並釋出原時段。
FB 不允許把排程貼文直接改成立即發佈（#10 Failed to publish post），所以只能重發。
用法：python scripts/publish_now.py <DB id> [<DB id> ...]"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import requests  # noqa: E402

from src import db, publish  # noqa: E402
from src.common import TZ, load_config, write_json  # noqa: E402

cfg = load_config()
con = db.connect()
_pid, tok, ver = publish._creds()
H = {"Authorization": f"Bearer {tok}"}

for post_id in (int(x) for x in sys.argv[1:]):
    row = con.execute("SELECT keyword, slot, status, fb_id, path FROM posts WHERE id=? AND mode='live'",
                      (post_id,)).fetchone()
    if not row or row[2] != "scheduled":
        print(post_id, "不是已排程的 live 貼文，略過", row)
        continue
    kw, slot, _st, old_fb, path = row
    d = Path(path)
    payload = json.loads((d / "payload.json").read_text(encoding="utf-8"))
    payload = {"message": payload["message"], "published": "true"}
    new_fb = publish.post_photo(d / "image.png", payload, cfg)
    r = requests.get(f"{publish.GRAPH}/{ver}/{new_fb}", params={"fields": "page_story_id"}, headers=H, timeout=60)
    story = r.json().get("page_story_id")
    s = requests.get(f"{publish.GRAPH}/{ver}/{story}", params={"fields": "is_published,permalink_url"},
                     headers=H, timeout=60).json()
    if not s.get("is_published"):
        print(post_id, "新貼文未確認公開，保留原排程，請人工檢查", new_fb, s)
        continue
    publish.delete_post(old_fb)
    now_k = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S")
    with con:
        con.execute("UPDATE posts SET fb_id=?, slot=? WHERE id=?", (new_fb, now_k, post_id))
    write_json(d / "payload.json", dict(payload, _published_now=now_k, _replaced_scheduled=old_fb, _slot_was=slot))
    print(post_id, kw[:20], "已公開", s.get("permalink_url"), "| 原排程", slot, old_fb, "已刪除")
