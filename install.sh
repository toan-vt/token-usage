#!/usr/bin/env bash
# Install the Claude Code token-usage uploader on this machine.
#   curl -fsSL https://raw.githubusercontent.com/toan-vt/token-usage/main/install.sh | bash -s -- <machine-name>
set -euo pipefail

REPO="toan-vt/token-usage"
HOST="${1:-}"
DIR="$HOME/.claude/token-usage"
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"

command -v python3 >/dev/null || { echo "python3 not found (try: module load python)"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' || { echo "python3 >= 3.6 required"; exit 1; }

if [ -z "$HOST" ]; then
  read -r -p "Name for this machine (e.g. mac, clusterA): " HOST </dev/tty
fi
case "$HOST" in (*[!A-Za-z0-9._-]*|"") echo "Machine name may only use letters, digits, . _ -"; exit 1;; esac

mkdir -p "$DIR"
curl -fsSL "https://raw.githubusercontent.com/$REPO/main/push_usage.py" -o "$DIR/push_usage.py"
chmod 700 "$DIR"

TOKEN="${TU_TOKEN:-}"
if [ -n "${TU_TOKEN+x}" ]; then
  :  # token given via environment (may be empty = use gh); no prompt
elif [ -f "$DIR/config.json" ]; then
  TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("token",""))' "$DIR/config.json")"
fi
if [ -z "$TOKEN" ] && [ -z "${TU_TOKEN+x}" ]; then
  if command -v gh >/dev/null && gh auth token >/dev/null 2>&1; then
    echo "gh is logged in here, so you can leave the token blank."
  fi
  read -r -s -p "GitHub token for $REPO (leave blank to use gh): " TOKEN </dev/tty; echo
fi

umask 077
python3 - "$DIR/config.json" "$REPO" "$HOST" "$TOKEN" <<'EOF'
import json, sys
path, repo, host, token = sys.argv[1:]
json.dump({"repo": repo, "host": host, "token": token}, open(path, "w"), indent=2)
EOF

python3 - "$SETTINGS" "$DIR/push_usage.py" <<'EOF'
import json, os, shutil, sys
path, script = sys.argv[1:]
s = {}
if os.path.exists(path):
    shutil.copy(path, path + ".bak")
    with open(path) as f:
        s = json.load(f)
s["cleanupPeriodDays"] = max(int(s.get("cleanupPeriodDays", 0) or 0), 180)
hooks = s.setdefault("hooks", {})
for event, kind in (("Stop", "stop"), ("SessionEnd", "end")):
    cmd = 'python3 "%s" --hook %s' % (script, kind)
    groups = hooks.setdefault(event, [])
    if not any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
        groups.append({"hooks": [{"type": "command", "command": cmd}]})
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(s, f, indent=2)
print("Updated", path, "(backup: settings.json.bak)" if os.path.exists(path + ".bak") else "")
EOF

echo "First upload (may take a few seconds)..."
python3 "$DIR/push_usage.py" --run
echo "Done. '$HOST' will upload at most every 30 min while Claude Code is in use."
echo "Log: $DIR/last.log"
