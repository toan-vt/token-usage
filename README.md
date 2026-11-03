# token-usage

A GitHub-style calendar of my daily Claude Code token usage across all machines.

**Live page:** https://toan-vt.github.io/token-usage/

## How it works

```
Claude Code (every machine) ──OTLP──▶ Grafana Cloud (live metrics, 14-day retention)
                                           │ read-only query
                                           ▼
             GitHub Actions, every 30 min (snapshot.py)
               • finished days  → data/history.csv  (committed, permanent)
               • today so far   → data/today.json   (deployed only)
                                           ▼
                         GitHub Pages: index.html
```

Days are in US Eastern time. Totals include input, output, cache-write and cache-read tokens.

## 1. Send telemetry from each machine

Add this to `~/.claude/settings.json` (merge it into any existing `env` block). Change `service.namespace` / `host.name` on each machine:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp-gateway-prod-<region>.grafana.net/otlp",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20<base64 of instanceID:token>",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
    "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=mac,host.name=mac"
  }
}
```

Keep the session ID on (the default). Each session is its own counter series, and `snapshot.py` relies on that to count exactly.

## 2. Repository secrets (read-only Grafana access)

| Secret | Where to find it |
|---|---|
| `GRAFANA_PROM_URL` | Grafana Cloud → your stack → Prometheus → Details → URL |
| `GRAFANA_PROM_USER` | same page → User / Instance ID (differs from the OTLP instance ID) |
| `GRAFANA_TOKEN` | Access policy token with the `metrics:read` scope |

```bash
gh secret set GRAFANA_PROM_URL
gh secret set GRAFANA_PROM_USER
gh secret set GRAFANA_TOKEN
gh workflow run update.yml
```

If your metric name differs from `claude_code_token_usage_tokens_total` (check Grafana → Explore), set it with `gh variable set TOKEN_METRIC --body <name>`.

## Local test

```bash
GRAFANA_PROM_URL=... GRAFANA_PROM_USER=... GRAFANA_TOKEN=... python3 snapshot.py
python3 -m http.server   # then open http://localhost:8000
```
