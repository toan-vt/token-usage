#!/usr/bin/env python3
"""Pull daily Claude Code token totals from Grafana Cloud (Prometheus/Mimir).

Writes:
  data/history.csv  - one row per finished day (committed to git, permanent)
  data/today.json   - running total for the current day (deployed, not committed)

Env:
  GRAFANA_PROM_URL   Prometheus URL of your Grafana Cloud stack, e.g.
                     https://prometheus-prod-13-prod-us-east-0.grafana.net/api/prom
                     (a ".../api/prom/push" URL is accepted too)
  GRAFANA_PROM_USER  Prometheus user / instance ID (number)
  GRAFANA_TOKEN      Access-policy token with metrics:read
  TOKEN_METRIC       optional, default claude_code_token_usage_tokens_total
  TZ_NAME            optional, default America/New_York
  BACKFILL_DAYS      optional, default 13 (Grafana free retention is 14 days)
"""
import base64
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
HISTORY = ROOT / "data" / "history.csv"
TODAY = ROOT / "data" / "today.json"
FIELDS = ["date", "input", "output", "cache_read", "cache_write"]
TYPE_MAP = {"input": "input", "output": "output",
            "cacheRead": "cache_read", "cacheCreation": "cache_write"}

METRIC = os.environ.get("TOKEN_METRIC") or "claude_code_token_usage_tokens_total"
TZ = ZoneInfo(os.environ.get("TZ_NAME") or "America/New_York")
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS") or "13")


def prom_base() -> str:
    url = os.environ["GRAFANA_PROM_URL"].rstrip("/")
    for suffix in ("/push", "/api/v1/query", "/api/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def query(promql: str, at: datetime) -> list:
    params = urllib.parse.urlencode({"query": promql, "time": f"{at.timestamp():.0f}"})
    req = urllib.request.Request(f"{prom_base()}/api/v1/query?{params}")
    cred = f"{os.environ['GRAFANA_PROM_USER']}:{os.environ['GRAFANA_TOKEN']}"
    req.add_header("Authorization", "Basic " + base64.b64encode(cred.encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {body}")
    return body["data"]["result"]


def tokens_between(start: datetime, end: datetime) -> dict:
    """Tokens used in [start, end), summed over all hosts/sessions/models, by type.

    Every Claude Code session is its own cumulative counter series (session.id label),
    so usage in the window = value at end - last value before start, or the full value
    for series that started inside the window. This avoids increase()'s extrapolation
    and its blind spot for a new series' first sample.
    """
    w = max(int((end - start).total_seconds()), 60)
    m = METRIC
    promql = (
        f"sum by (type) ("
        f"clamp_min(max_over_time({m}[{w}s]) - last_over_time({m}[3d] offset {w}s), 0)"
        f" or max_over_time({m}[{w}s]))"
    )
    out = {v: 0 for v in TYPE_MAP.values()}
    for series in query(promql, end):
        key = TYPE_MAP.get(series["metric"].get("type", ""))
        if key:
            out[key] += round(float(series["value"][1]))
    return out


def day_bounds(d: date) -> tuple:
    start = datetime.combine(d, time.min, TZ)
    return start, start + timedelta(days=1)


def load_history() -> dict:
    if not HISTORY.exists():
        return {}
    with HISTORY.open() as f:
        return {row["date"]: row for row in csv.DictReader(f)}


def save_history(rows: dict) -> None:
    HISTORY.parent.mkdir(exist_ok=True)
    with HISTORY.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for d in sorted(rows):
            w.writerow({k: rows[d][k] for k in FIELDS})


def main() -> int:
    if not all(os.environ.get(k) for k in ("GRAFANA_PROM_URL", "GRAFANA_PROM_USER", "GRAFANA_TOKEN")):
        print("Grafana secrets not set; skipping snapshot (page deploys with existing data).")
        return 0

    now = datetime.now(TZ)
    today = now.date()
    rows = load_history()

    added = 0
    for i in range(BACKFILL_DAYS, 0, -1):
        d = today - timedelta(days=i)
        if d.isoformat() in rows:
            continue
        start, end = day_bounds(d)
        rows[d.isoformat()] = {"date": d.isoformat(), **tokens_between(start, end)}
        added += 1
    if added:
        save_history(rows)
    print(f"history: {added} day(s) added, {len(rows)} total")

    start, _ = day_bounds(today)
    t = {"date": today.isoformat(), "updated_at": now.isoformat(timespec="minutes"),
         **tokens_between(start, now)}
    TODAY.write_text(json.dumps(t, indent=2) + "\n")
    print(f"today so far: {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
