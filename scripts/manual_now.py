"""手動立即發一篇已寫好的稿：跳過模型審稿，只跑確定性閘門 -> 生圖 -> 立即公開（published=true）。
用法：python manual_now.py <專案根目錄> <post_dir> [--post] [--image-prompt "<英文提示詞>"]"""
import json
import sys
from datetime import datetime
from pathlib import Path

root, post_dir = Path(sys.argv[1]), Path(sys.argv[2])
do_post = "--post" in sys.argv
new_prompt = sys.argv[sys.argv.index("--image-prompt") + 1] if "--image-prompt" in sys.argv else None
sys.path.insert(0, str(root))

import run_daily as rd  # noqa: E402
from src import db, gates, image, publish, schedule  # noqa: E402
from src import select as sel  # noqa: E402
from src.common import load_config, write_json  # noqa: E402

cfg = load_config()
terms = sel.load_blocklist()
topic = json.loads((post_dir / "source.json").read_text(encoding="utf-8"))
draft = json.loads((post_dir / "draft.json").read_text(encoding="utf-8"))
for k in ("caption", "publish_time", "opinion", "sentences_to_check"):
    draft.pop(k, None)
if new_prompt:
    draft["image_prompt"] = new_prompt
oks = [s for s in topic["sources"] if s.get("ok")]
urls = [s["url"] for s in oks]
texts = [s["text"] for s in oks]
tag_texts = [topic["keyword"]] + [s.get("title") or "" for s in oks]
draft["hashtags"] = gates.sanitize_hashtags(draft["hashtags"], tag_texts, cfg["hashtags"]["whitelist"])

ok, why, caption = gates.draft_gate(draft, urls, texts, cfg, terms, tag_texts, topic["keyword"])
print("draft_gate:", ok, why)
con = db.connect()
dup = db.is_duplicate(con, topic["norm_key"], urls)
print("duplicate:", dup)
payload = publish.build_payload(draft, urls, None)
payload["published"] = "true"
bad = gates.final_caption_problem(payload["message"], cfg, terms)
print("final_caption_problem:", bad)
print("image_prompt:", draft["image_prompt"])
print("----- MESSAGE -----\n" + payload["message"])
if not (do_post and ok and not dup and not bad):
    sys.exit(0)

write_json(post_dir / "draft_manual.json", draft)
img, err = image.generate_image(post_dir, draft["image_prompt"], cfg)
print("image:", img, err)
if err:
    sys.exit(3)
err = image.add_headline(post_dir / "image.png", draft["title"], cfg)
print("headline:", err)
if err:
    sys.exit(3)
write_json(post_dir / "payload.json", payload)
slot_k = schedule.slot_key(datetime.now(rd.TZ))
pid = db.reserve(con, topic["norm_key"], topic["keyword"], urls, slot_k, "live", str(post_dir))
if pid is None:
    print("DB 衝突")
    sys.exit(5)
try:
    fb_id = publish.post_photo(post_dir / "image.png", payload, cfg)
except publish.PublishFailed as e:
    db.release(con, pid)
    print("PUBLISH FAILED", e)
    sys.exit(6)
db.mark(con, pid, "scheduled", fb_id)
print("PUBLISHED", slot_k, "fb_id", fb_id)
