#!/usr/bin/env python3
"""Upload this machine's Claude Code token usage to the token-usage GitHub repo.

Reads ~/.claude/projects/**/*.jsonl, sums tokens per UTC hour (deduplicated by
message id), and writes data/hosts/<host>.json in the repo via the GitHub API.
Each machine only ever writes its own file, so machines never conflict.

Usage:
  push_usage.py --hook stop     # from a Claude Code Stop hook (throttled, runs in background)
  push_usage.py --hook end      # from a SessionEnd hook (shorter throttle)
  push_usage.py --run           # compute + upload now (foreground)
  push_usage.py --dry-run       # compute and print a summary, no upload

Config: ~/.claude/token-usage/config.json
  {"repo": "toan-vt/token-usage", "host": "mac", "token": "github_pat_..."}
  If "token" is empty: $GITHUB_TOKEN, then `gh auth token`, then git's stored
  credentials for github.com (`git credential fill`) are used.
  If no token is found at all, it pushes with git over SSH (your SSH key) using a
  private shallow clone in ~/.claude/token-usage/repo.
  Optional: "transport": "api" | "git" to force one; "git_url" to override the remote.

Works with Python 3.6+ and the standard library only.
"""
import base64
import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, ".claude", "token-usage")
CONFIG = os.path.join(APP_DIR, "config.json")
STAMP = os.path.join(APP_DIR, ".last_attempt")
LOCK = os.path.join(APP_DIR, ".lock")
LOG = os.path.join(APP_DIR, "last.log")
PROJECTS = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude")), "projects")
THROTTLE = {"stop": 30 * 60, "end": 5 * 60}
# per-hour value: [input, output, cache_write, cache_read]


def load_config():
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg = json.load(f)
    cfg.setdefault("repo", "toan-vt/token-usage")
    cfg.setdefault("host", socket.gethostname().split(".")[0])
    if not cfg.get("token"):
        cfg["token"] = find_token()
    return cfg


def find_token():
    """Fallbacks when config has no token: $GITHUB_TOKEN, `gh auth token`, then git's stored credentials."""
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        tok = subprocess.check_output(["gh", "auth", "token"], stderr=subprocess.DEVNULL).decode().strip()
        if tok:
            return tok
    except Exception:
        pass
    try:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        p = subprocess.run(["git", "credential", "fill"], input=b"protocol=https\nhost=github.com\n\n",
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=15)
        for line in p.stdout.decode().splitlines():
            if line.startswith("password="):
                return line[len("password="):].strip()
    except Exception:
        pass
    return ""


def compute_hours():
    """{'YYYY-MM-DDTHH' (UTC): [input, output, cache_write, cache_read]}"""
    best = {}  # message id -> (hour, [4 ints]); keep the max per field across duplicate lines
    for path in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            f = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                ts = d.get("timestamp") or ""
                if d.get("type") != "assistant" or not u or len(ts) < 13:
                    continue
                vals = [int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                        int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0)]
                if not any(vals):
                    continue
                key = m.get("id") or d.get("requestId") or d.get("uuid")
                prev = best.get(key)
                if prev is None:
                    best[key] = (ts[:13], vals)
                else:
                    best[key] = (prev[0], [max(a, b) for a, b in zip(prev[1], vals)])
    hours = {}
    for hour, vals in best.values():
        acc = hours.setdefault(hour, [0, 0, 0, 0])
        for i in range(4):
            acc[i] += vals[i]
    return hours, len(best)


def gh_request(cfg, method, path, body=None):
    url = "https://api.github.com/repos/%s/contents/%s" % (cfg["repo"], path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + cfg["token"])
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "token-usage-uploader")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        raise RuntimeError("GitHub %s %s -> %s %s" % (method, path, e.code, e.read()[:300]))


def merged_doc(cfg, old, hours):
    """New file content (bytes), or None when nothing changed.

    Keeps older hours the local logs no longer have (Claude Code prunes old transcripts).
    """
    earliest = min(hours) if hours else "9999"
    merged = {h: v for h, v in old.items() if h < earliest}
    merged.update(hours)
    if merged == old:
        return None, len(merged)
    doc = {"host": cfg["host"], "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "hours": {h: merged[h] for h in sorted(merged)}}
    return json.dumps(doc, separators=(",", ":")).encode(), len(merged)


def upload_api(cfg, hours):
    path = "data/hosts/%s.json" % cfg["host"]
    remote = gh_request(cfg, "GET", path)
    sha, old = None, {}
    if remote:
        sha = remote["sha"]
        old = json.loads(base64.b64decode(remote["content"]).decode()).get("hours", {})
    content, n = merged_doc(cfg, old, hours)
    if content is None:
        return "unchanged"
    body = {"message": "usage: %s" % cfg["host"], "content": base64.b64encode(content).decode()}
    if sha:
        body["sha"] = sha
    gh_request(cfg, "PUT", path, body)
    return "uploaded %d hours (api)" % n


def git(args, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=20"))
    p = subprocess.run(["git"] + args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args[0], p.stderr.decode(errors="ignore").strip()[-300:]))
    return p.stdout.decode(errors="ignore")


def upload_git(cfg, hours):
    """Push via git (SSH keys work). Uses a private shallow clone in ~/.claude/token-usage/repo."""
    repo = os.path.join(APP_DIR, "repo")
    url = cfg.get("git_url") or "git@github.com:%s.git" % cfg["repo"]
    if not os.path.isdir(os.path.join(repo, ".git")):
        git(["clone", "--quiet", "--depth", "1", "--branch", "main", url, repo])
    path = os.path.join(repo, "data", "hosts", "%s.json" % cfg["host"])
    ident = ["-c", "user.name=token-usage (%s)" % cfg["host"], "-c", "user.email=token-usage@users.noreply.github.com"]
    for attempt in range(4):  # another machine may push in between; its files never overlap ours
        git(["fetch", "--quiet", "--depth", "1", "origin", "main"], cwd=repo)
        git(["reset", "--quiet", "--hard", "FETCH_HEAD"], cwd=repo)
        old = {}
        if os.path.exists(path):
            with open(path) as f:
                old = json.load(f).get("hours", {})
        content, n = merged_doc(cfg, old, hours)
        if content is None:
            return "unchanged"
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(content)
        git(["add", path], cwd=repo)
        git(ident + ["commit", "--quiet", "-m", "usage: %s" % cfg["host"]], cwd=repo)
        try:
            git(["push", "--quiet", "origin", "HEAD:main"], cwd=repo)
            return "uploaded %d hours (git)" % n
        except RuntimeError:
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 3)


def upload(cfg, hours):
    transport = cfg.get("transport") or ("api" if cfg["token"] else "git")
    return upload_git(cfg, hours) if transport == "git" else upload_api(cfg, hours)


def run():
    if not os.path.isdir(APP_DIR):
        os.makedirs(APP_DIR)
    try:  # one uploader at a time per machine; stale locks expire after 10 min
        if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) > 600:
            os.remove(LOCK)
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        return "another upload is running"
    try:
        cfg = load_config()
        hours, n = compute_hours()
        return "%d messages; %s" % (n, upload(cfg, hours))
    finally:
        os.remove(LOCK)


def hook(kind):
    try:
        sys.stdin.read()  # drain the hook payload
    except Exception:
        pass
    try:
        if time.time() - os.path.getmtime(STAMP) < THROTTLE.get(kind, 1800):
            return
    except OSError:
        pass
    if not os.path.isdir(APP_DIR):
        os.makedirs(APP_DIR)
    open(STAMP, "w").close()
    # Detach so Claude Code never waits on the network.
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--run", "--log"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def main(argv):
    if "--hook" in argv:
        i = argv.index("--hook")
        hook(argv[i + 1] if i + 1 < len(argv) else "stop")
        return 0
    if "--dry-run" in argv:
        hours, n = compute_hours()
        days = {}
        for h, v in hours.items():
            days[h[:10]] = days.get(h[:10], 0) + sum(v)
        for d in sorted(days)[-10:]:
            print("%s  %15s tokens (UTC day)" % (d, "{:,}".format(days[d])))
        print("%d unique messages, %d hours, config: %s" % (n, len(hours), load_config()["host"]))
        return 0
    try:
        result = run()
    except Exception as e:  # never crash loudly from a hook
        result = "ERROR: %s" % e
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), result)
    if "--log" in argv:
        with open(LOG, "a") as f:
            f.write(line)
    else:
        sys.stdout.write(line)
    return 1 if result.startswith("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
