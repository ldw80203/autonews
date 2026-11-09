"""發文時段選擇：從 config [schedule] slots 取時段（今天＋明天，跨午夜也接得上），
距離現在 >= min_lead 分鐘，且不與 DB 已排時段重複。"""
from __future__ import annotations

from datetime import datetime, timedelta


def slot_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def pick_slots(now: datetime, cfg: dict, taken: set[str], max_n: int) -> list[datetime]:
    sc = cfg["schedule"]
    lead = timedelta(minutes=sc["min_lead_minutes"])
    out: list[datetime] = []
    for day in (0, 1):
        base = now + timedelta(days=day)
        for hhmm in sorted(sc["slots"]):
            h, m = (int(x) for x in hhmm.split(":"))
            dt = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt - now < lead or slot_key(dt) in taken:
                continue
            out.append(dt)
            if len(out) >= max_n:
                return out
    return out
