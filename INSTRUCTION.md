# Set up Claude Code token-usage uploads on this machine

**For Claude Code:** follow these steps to connect this machine to the user's token-usage dashboard at
https://toan-vt.github.io/token-usage/. Repo: https://github.com/toan-vt/token-usage

## What gets installed

- `~/.claude/token-usage/push_usage.py`: reads `~/.claude/projects/**/*.jsonl`, sums tokens per hour (deduplicated by message id), and uploads `data/hosts/<machine>.json` to the repo through the GitHub API. It uses only the Python 3.6+ standard library.
- Two hooks in `~/.claude/settings.json` (`Stop`, `SessionEnd`) that run the uploader in the background, at most every 30 min.
- `cleanupPeriodDays` set to at least 180, so Claude Code keeps 6 months of logs.

## Rules

- **Never print, echo, log, or paste a GitHub token.** That includes debugging. Report only "token found" or "token not found".
- **Do not remove or edit existing hooks** or other settings. The installer only appends its own two hooks (no duplicates) and saves `settings.json.bak` first.
- **Do not clone the repo or commit to it.** The uploader writes its own file through the API.
- **Install once per home directory.** Cluster nodes usually share `$HOME`, so the login node is enough. Separate clusters each need their own install with their own machine name.

## Steps

### 1. Choose the machine name
Use the name the user gave when they asked for setup. If none was given, ask for one, suggesting the cluster's short name such as `hpc1` (letters, digits, `.` `_` `-` only). Each machine must have a **unique** name, because the same name on two machines makes them overwrite each other's data. To see names already in use:
```bash
curl -s https://api.github.com/repos/toan-vt/token-usage/contents/data/hosts | python3 -c 'import json,sys; print([f["name"][:-5] for f in json.load(sys.stdin)])'
```
If `~/.claude/token-usage/config.json` already exists, this machine is already set up. Reuse its `host` value (`python3 -c 'import json,os; print(json.load(open(os.path.expanduser("~/.claude/token-usage/config.json")))["host"]')`) and continue, which updates the install.

### 2. Check prerequisites
```bash
python3 -c 'import sys; print(sys.version)'            # needs >= 3.6; if missing: module avail python; module load python3
curl -sS -o /dev/null -w "%{http_code}\n" https://api.github.com   # expect 200
```
If `python3` only works after `module load`, the hooks also need it on PATH. Tell the user, and use the absolute path of the module's python in step 4 (see Troubleshooting).

### 3. Check that a GitHub credential with write access exists (without showing it)
```bash
mkdir -p ~/.claude/token-usage && curl -fsSL https://raw.githubusercontent.com/toan-vt/token-usage/main/push_usage.py -o ~/.claude/token-usage/push_usage.py
python3 - <<'EOF'
import json, os, sys, urllib.request
sys.path.insert(0, os.path.expanduser("~/.claude/token-usage"))
import push_usage
tok = push_usage.find_token()          # $GITHUB_TOKEN, gh auth token, or git credential for github.com
if not tok:
    sys.exit("token not found")
req = urllib.request.Request("https://api.github.com/repos/toan-vt/token-usage",
                             headers={"Authorization": "Bearer " + tok, "User-Agent": "token-usage-check"})
try:
    perms = json.load(urllib.request.urlopen(req, timeout=20)).get("permissions", {})
    print("token found; write access:", perms.get("push"))
except Exception as e:
    print("token found but API check failed:", getattr(e, "code", e))
EOF
```
- `token found; write access: True` → go to step 4.
- `token not found`, `write access: False`, or `401`/`404` → the existing credential can't be used. Go to **"No usable token"** below, then come back to step 4.

### 4. Install
```bash
curl -fsSL https://raw.githubusercontent.com/toan-vt/token-usage/main/install.sh | TU_TOKEN= bash -s -- <machine-name>
```
`TU_TOKEN=` (empty) means "don't prompt; use the credential found in step 3." Expected output ends with:
```
<date> <N> messages; uploaded <H> hours      (or "unchanged")
Done. '<machine-name>' will upload at most every 30 min while Claude Code is in use.
```
A line starting with `ERROR` means the upload failed. See Troubleshooting.

### 5. Verify
```bash
python3 -c 'import json,os; s=json.load(open(os.path.expanduser("~/.claude/settings.json"))); print(s.get("cleanupPeriodDays")); print([h["command"] for e in ("Stop","SessionEnd") for g in s["hooks"][e] for h in g["hooks"] if "push_usage" in h["command"]])'
```
Wait about 2 minutes for the site to rebuild, then check that the machine appears:
```bash
curl -s "https://toan-vt.github.io/token-usage/data/daily.json?t=$(date +%s)" | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["hosts"]))'
```
Report to the user: the machine name, the number of messages uploaded, and that it appears in the hosts list.

## No usable token

Ask the user to create a fine-grained token at GitHub → Settings → Developer settings → **Fine-grained tokens**:
- Repository access: **Only select repositories → `toan-vt/token-usage`**
- Permissions: **Contents → Read and write** (nothing else)

Then ask the user to run this **themselves** by typing it at the Claude Code prompt with the `!` prefix, so the token is never in the conversation:
```
! read -r -s -p "GitHub token: " T && echo && mkdir -p ~/.claude/token-usage && chmod 700 ~/.claude/token-usage && printf '{"repo":"toan-vt/token-usage","host":"<machine-name>","token":"%s"}\n' "$T" > ~/.claude/token-usage/config.json && chmod 600 ~/.claude/token-usage/config.json && unset T && echo saved
```
Then run step 4. The installer keeps the token already in `config.json`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python3: command not found` from the hooks | In `~/.claude/settings.json`, change the two `python3 "…/push_usage.py"` hook commands to the absolute python path (`which python3` after `module load`). |
| `ERROR: GitHub PUT … -> 401/403` | The token is invalid or can't write to the repo. Use "No usable token" above. |
| `ERROR: … 409` | Two uploads from the same machine name raced each other. It fixes itself on the next upload. If it keeps happening, two machines share a name; rename one. |
| Network errors | Check `curl -sI https://api.github.com`. Some clusters need `export HTTPS_PROXY=…`; put it in the shell profile so the hooks inherit it. |
| No new data on the page | `cat ~/.claude/token-usage/last.log`; run `python3 ~/.claude/token-usage/push_usage.py --run` for an immediate upload. |

Useful commands: `push_usage.py --dry-run` (shows per-day totals, uploads nothing), `push_usage.py --run` (uploads now).

## Uninstall
Remove the two hooks whose command contains `push_usage.py` from `~/.claude/settings.json`, then `rm -rf ~/.claude/token-usage`. To also remove the machine's data from the dashboard, delete `data/hosts/<machine-name>.json` in the repo.
