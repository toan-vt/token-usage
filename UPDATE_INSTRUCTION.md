# Update the token-usage uploader on this machine

**For Claude Code:** this machine already uploads Claude Code usage to https://toan-vt.github.io/token-usage/. Update its uploader to **version 2**, which records usage per model so the dashboard can estimate cost. Follow the steps in order, then report the result.

What changes: only the script `~/.claude/token-usage/push_usage.py`. The hooks in `~/.claude/settings.json` call that same file, and `~/.claude/token-usage/config.json` (machine name, credentials) stays as it is.

## Rules

- **Never print, echo, log, or paste a GitHub token**, including while debugging.
- **Do not edit `~/.claude/settings.json` or `~/.claude/token-usage/config.json`.**
- **Do not clone or commit to the repo yourself.** The uploader manages `~/.claude/token-usage/repo` on its own.
- If this machine has no `~/.claude/token-usage/push_usage.py`, it was never set up. Stop and tell the user to use https://raw.githubusercontent.com/toan-vt/token-usage/main/INSTRUCTION.md instead.

## Steps

### 1. Check the current install
```bash
ls ~/.claude/token-usage/push_usage.py
python3 ~/.claude/token-usage/push_usage.py --version 2>/dev/null || echo "version 1 (no --version flag)"
python3 -c 'import json,os; print("machine:", json.load(open(os.path.expanduser("~/.claude/token-usage/config.json")))["host"])'
```
If it already says `version 2`, skip to step 3.

### 2. Replace the script
The new file is downloaded next to the old one, checked, and only then swapped in, so a failed download never breaks the hooks:
```bash
cd ~/.claude/token-usage \
  && curl -fsSL https://raw.githubusercontent.com/toan-vt/token-usage/main/push_usage.py -o push_usage.py.new \
  && python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' push_usage.py.new \
  && mv push_usage.py.new push_usage.py \
  && python3 push_usage.py --version
```
Expected: `token-usage uploader version 2`. If it still prints version 1 or fails on `--version`, GitHub's file cache may be stale for a few minutes after an update. Wait 5 minutes and run this step again.

`python3` must be the same one the hooks use. If the hooks call an absolute path (check with `grep push_usage ~/.claude/settings.json`), use that interpreter in steps 2–4.

### 3. Preview
```bash
python3 ~/.claude/token-usage/push_usage.py --dry-run
```
The output now has a **`by model:`** section listing ids such as `claude-opus-5-5`, `claude-fable-5-2+p200k` or `claude-haiku-5-5+p100k`. The `+p100k` / `+p200k` / `+fast` / `+us` suffixes are expected: they mark usage billed at a different rate.

### 4. Upload now
```bash
python3 ~/.claude/token-usage/push_usage.py --run
```
Expected: `<date> <N> messages; uploaded <H> hours (git)` (or `(api)`).
- `unchanged` means this machine has no Claude Code logs on disk any more. Its existing data stays on the dashboard without model info, so it counts as "unpriced". That's fine; report it.
- A line starting with `ERROR`: see Troubleshooting.

### 5. Verify
Check that the repo now has a version 2 file for this machine (replace `<machine>`):
```bash
curl -s https://api.github.com/repos/toan-vt/token-usage/contents/data/hosts/<machine>.json \
  | python3 -c 'import json,sys,base64; d=json.loads(base64.b64decode(json.load(sys.stdin)["content"])); print("version", d.get("version", 1), "updated", d["updated_at"])'
```
Expected: `version 2` and an `updated` time from the last few minutes (UTC).

About 2 minutes later the dashboard rebuilds. Its "Unknown model" (unpriced) usage for this machine's days should be gone, apart from days whose logs are no longer on disk.

### 6. Report to the user
The machine name, the version now installed, the `--run` result line, and the models listed by `--dry-run`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ERROR: … Permission denied (publickey)` or `401`/`403` | Credentials stopped working. They're unrelated to this update. Follow the "No usable token" section of [INSTRUCTION.md](https://raw.githubusercontent.com/toan-vt/token-usage/main/INSTRUCTION.md). |
| `ERROR: git push failed` once | Another machine pushed at the same moment. Run step 4 again. |
| `another upload is running` | A background upload from a hook is in progress. Wait a minute and run step 4 again. |
| `SyntaxError` on the new file | The machine's `python3` is older than 3.6. Run `module load python3` (or similar) and repeat from step 2. |

To roll back, run step 2 with a URL pinned to an older commit (`https://raw.githubusercontent.com/toan-vt/token-usage/<commit>/push_usage.py`). The dashboard reads both file versions.
