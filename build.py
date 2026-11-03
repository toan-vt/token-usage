#!/usr/bin/env python3
"""Merge data/hosts/*.json (per-machine UTC hours) into daily totals for the page.

Writes <out>/data/daily.json:
  {"generated_at": ..., "tz": ..., "hosts": {host: updated_at},
   "days": {"YYYY-MM-DD": {"input":..,"output":..,"cache_write":..,"cache_read":..}}}
Days are in TZ_NAME (default America/New_York).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
TZ_NAME = os.environ.get("TZ_NAME") or "America/New_York"
TZ = ZoneInfo(TZ_NAME)
KEYS = ["input", "output", "cache_write", "cache_read"]


def main(out: Path) -> None:
    days, hosts = {}, {}
    for f in sorted((ROOT / "data" / "hosts").glob("*.json")):
        doc = json.loads(f.read_text())
        hosts[doc.get("host", f.stem)] = doc.get("updated_at")
        for hour, vals in doc.get("hours", {}).items():
            utc = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
            day = utc.astimezone(TZ).date().isoformat()
            acc = days.setdefault(day, dict.fromkeys(KEYS, 0))
            for k, v in zip(KEYS, vals):
                acc[k] += v
    (out / "data").mkdir(parents=True, exist_ok=True)
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "tz": TZ_NAME, "hosts": hosts, "days": {d: days[d] for d in sorted(days)}}
    (out / "data" / "daily.json").write_text(json.dumps(doc, indent=1) + "\n")
    print(f"{len(hosts)} host(s), {len(days)} day(s)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "_site"))
