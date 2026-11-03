# token-usage

A GitHub-style calendar of my daily Claude Code token usage across all my machines.

**Live page:** https://toan-vt.github.io/token-usage/

## How it works

```
each machine: Claude Code hook (after a response, at most every 30 min; in the background)
   → push_usage.py reads ~/.claude/projects/**/*.jsonl
   → sums tokens per UTC hour, deduplicated by message id
   → writes data/hosts/<machine>.json in this repo (GitHub API)
GitHub Actions on every push: build.py merges all machines → daily totals (US Eastern) → Pages
```

- Each machine writes only its own file, so machines never conflict, and re-uploading never double-counts.
- Totals are exact: they come from the per-message `usage` that Claude Code logs.
- Nothing is lost when offline. The next upload recomputes from the local logs.
- The installer sets `cleanupPeriodDays: 180` (6 months) so Claude Code keeps the logs, and older hours already uploaded are kept in the repo regardless.

## Add a machine

### Easiest: let Claude Code do it

Start Claude Code on the new machine and paste:

```
Follow https://raw.githubusercontent.com/toan-vt/token-usage/main/INSTRUCTION.md to set up this machine as {machine-name}
```

Replace `{machine-name}` with a unique name for the machine, such as `frontier` or `turing` (letters, digits, `.` `_` `-`). Claude Code checks the prerequisites, picks SSH or a token automatically, installs the hooks, and confirms the machine appears on the dashboard. See [INSTRUCTION.md](INSTRUCTION.md) for the exact steps.

### Manual

Machines that can already `git push` to GitHub over SSH need no token. Leave the token prompt blank and the uploader pushes with your SSH key. Otherwise:

1. Create a GitHub token once, and reuse it on every machine:
   GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
   - Repository access: **Only select repositories → token-usage**
   - Permissions: **Contents: Read and write** (nothing else)
2. On the machine:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/toan-vt/token-usage/main/install.sh | bash -s -- <machine-name>
   ```
   Paste the token when asked. Leave it blank on machines where `gh` is logged in or GitHub SSH access works.
   The installer adds `Stop` and `SessionEnd` hooks to `~/.claude/settings.json` (backup saved as `settings.json.bak`) and does a first upload.

Requirements: `python3` ≥ 3.6 and outbound HTTPS to github.com.

## Handy commands

```bash
python3 ~/.claude/token-usage/push_usage.py --dry-run   # what would be uploaded
python3 ~/.claude/token-usage/push_usage.py --run       # upload now
cat ~/.claude/token-usage/last.log                      # background upload results
```

To remove a machine, delete the two hooks from `~/.claude/settings.json`, delete `~/.claude/token-usage/`, and delete its `data/hosts/<machine>.json` here.
