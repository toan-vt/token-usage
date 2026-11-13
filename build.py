#!/usr/bin/env python3
"""Merge data/hosts/*.json into daily totals and estimated cost for the page.

Writes <out>/data/daily.json:
  {"generated_at", "tz", "prices_as_of", "hosts": {host: updated_at},
   "days": {"YYYY-MM-DD": {"input", "output", "cache_write", "cache_read", "web_search",
                           "cost", "unpriced",
                           "models": {"<model>": {"tokens", "output", "cost"}}}}}
Days are in TZ_NAME (default America/New_York). Cost uses prices.json (USD, Claude API
list prices); tokens from models not in prices.json, or from version-1 host files that
have no model info ("unknown"), are counted in "unpriced" and left out of "cost".
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
PRICES = json.loads((ROOT / "prices.json").read_text())
RATE_KEYS = ["input", "output", "cw5m", "cw1h", "cache_read"]  # order of the usage vector


def find_model(model: str):
    """Longest prices.json id equal to `model` or a prefix of it at a '-' boundary."""
    best = None
    for k in PRICES["models"]:
        if (model == k or model.startswith(k + "-")) and (best is None or len(k) > len(best)):
            best = k
    return PRICES["models"][best] if best else None


def rates_for(key: str):
    """(per-token rates for the 5 token fields, token multiplier) or None if unpriced."""
    model, *flags = key.split("+")
    entry = find_model(model)
    if entry is None:
        return None
    rates = {k: entry[k] for k in RATE_KEYS}
    # The uploader marks prompts over 100K ("p100k") or over 200K ("p200k"); a model's long-prompt
    # rates apply when that guaranteed lower bound reaches its threshold.
    bound = 200000 if "p200k" in flags else 100000 if "p100k" in flags else 0
    long = entry.get("long")
    if long and bound and bound >= long["over"]:
        rates = {k: long[k] for k in RATE_KEYS}
    if "fast" in flags and entry.get("fast"):
        f = entry["fast"]
        hit = rates["cache_read"] / rates["input"]  # model's cache-hit multiplier applies on top of fast pricing
        rates = {"input": f["input"], "output": f["output"], "cw5m": 1.25 * f["input"],
                 "cw1h": 2 * f["input"], "cache_read": hit * f["input"]}
    mult = 1.0
    for flag in flags:
        mult *= PRICES.get("geo_multiplier", {}).get(flag, 1.0)
    return rates, mult


def cost_of(key: str, vec: list):
    r = rates_for(key)
    if r is None:
        return None
    rates, mult = r
    tokens = sum(n * rates[k] for n, k in zip(vec[:5], RATE_KEYS)) / 1e6 * mult
    return tokens + vec[5] * PRICES.get("web_search_per_1k", 0) / 1000


def entries(hour_value):
    """Yield (model_key, 6-vector) for one hour, from version 1 or 2 host files."""
    if isinstance(hour_value, list):  # v1: [input, output, cache_write, cache_read], no model info
        i, o, cw, cr = (hour_value + [0, 0, 0, 0])[:4]
        yield "unknown", [i, o, cw, 0, cr, 0]
        return
    for key, vec in hour_value.items():
        yield key, (list(vec) + [0] * 6)[:6]


def main(out: Path) -> None:
    days, hosts = {}, {}
    for f in sorted((ROOT / "data" / "hosts").glob("*.json")):
        doc = json.loads(f.read_text())
        hosts[doc.get("host", f.stem)] = doc.get("updated_at")
        for hour, value in doc.get("hours", {}).items():
            utc = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
            day = utc.astimezone(TZ).date().isoformat()
            acc = days.setdefault(day, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                                        "web_search": 0, "cost": 0.0, "unpriced": 0, "models": {}})
            for key, v in entries(value):
                acc["input"] += v[0]
                acc["output"] += v[1]
                acc["cache_write"] += v[2] + v[3]
                acc["cache_read"] += v[4]
                acc["web_search"] += v[5]
                tokens = sum(v[:5])
                c = cost_of(key, v)
                model = key.split("+")[0]
                m = acc["models"].setdefault(model, {"tokens": 0, "output": 0, "cost": 0.0})
                m["tokens"] += tokens
                m["output"] += v[1]
                if c is None:
                    acc["unpriced"] += tokens
                    m["cost"] = None
                else:
                    acc["cost"] += c
                    if m["cost"] is not None:
                        m["cost"] += c
    for d in days.values():
        d["cost"] = round(d["cost"], 4)
        for m in d["models"].values():
            if m["cost"] is not None:
                m["cost"] = round(m["cost"], 4)
    (out / "data").mkdir(parents=True, exist_ok=True)
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "tz": TZ_NAME, "prices_as_of": PRICES.get("as_of"), "hosts": hosts,
           "days": {d: days[d] for d in sorted(days)}}
    (out / "data" / "daily.json").write_text(json.dumps(doc, indent=1) + "\n")
    print(f"{len(hosts)} host(s), {len(days)} day(s)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "_site"))
