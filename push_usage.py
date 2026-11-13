#!/usr/bin/env python3
"""Upload this machine's Claude Code token usage to the token-usage GitHub repo.

Reads ~/.claude/projects/**/*.jsonl, sums tokens per UTC hour and per model
(deduplicated by message id), and writes data/hosts/<host>.json in the repo.
Each machine only ever writes its own file, so machines never conflict.

File format (version 2):
  {"host": ..., "updated_at": ..., "version": 2,
   "hours": {"YYYY-MM-DDTHH": {"<model>[+flags]": [input, output, cache_write_5m,
                                                    cache_write_1h, cache_read, web_searches]}}}
  Flags change the price: "+fast" (fast mode), "+us" (US-only inference_geo),
  "+p100k" / "+p200k" (prompt incl. cache over 100K / 200K tokens).
  Prices are applied later by build.py (prices.json), not here.
  Version 1 files stored a plain [input, output, cache_write, cache_read] list per hour.

Usage:
  push_usage.py --hook stop     # from a Claude Code Stop hook (throttled, runs in background)
  push_usage.py --hook end      # from a SessionEnd hook (shorter throttle)
  push_usage.py --run           # compute + upload now (foreground)
  push_usage.py --dry-run       # compute and print a summary, no upload
  push_usage.py --version       # print the uploader version

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
VERSION = 3  # uploader version (3: commits as a bot identity, never as the token owner)
FORMAT = 2   # data file format version (see docstring)
# Commit identity for every upload. The reserved .invalid domain can never be verified by a
# GitHub account, so these commits never count toward anyone's contribution graph.
BOT_EMAIL = "token-usage-bot@noreply.invalid"


def bot_name(cfg):
    return "token-usage-bot (%s)" % cfg["host"]
N = 6  # [input, output, cache_write_5m, cache_write_1h, cache_read, web_searches]


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


def usage_vector(u):
    """[input, output, cache_write_5m, cache_write_1h, cache_read, web_searches] from one usage object."""
    cw_total = int(u.get("cache_creation_input_tokens") or 0)
    cc = u.get("cache_creation") or {}
    cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    cw5m = max(cw_total - cw1h, int(cc.get("ephemeral_5m_input_tokens") or 0))
    ws = int((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
    return [int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
            cw5m, cw1h, int(u.get("cache_read_input_tokens") or 0), ws]


def model_key(m, u):
    """Model id plus flags for anything that changes its price (prompt size is added later)."""
    key = m.get("model") or "unknown"
    if u.get("speed") == "fast":
        key += "+fast"
    geo = u.get("inference_geo")
    if geo and geo not in ("global", "not_available"):
        key += "+" + str(geo)
    return key


def compute_hours():
    """({'YYYY-MM-DDTHH' (UTC): {model_key: [6 ints]}}, number of messages)"""
    best = {}  # message id -> [hour, model_key, vals]; keep the max per field across duplicate lines
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
                vals = usage_vector(u)
                if not any(vals):
                    continue
                key = m.get("id") or d.get("requestId") or d.get("uuid")
                prev = best.get(key)
                if prev is None:
                    best[key] = [ts[:13], model_key(m, u), vals]
                else:
                    prev[2] = [max(a, b) for a, b in zip(prev[2], vals)]
    hours = {}
    for hour, mkey, vals in best.values():
        prompt = vals[0] + vals[2] + vals[3] + vals[4]  # all input tokens incl. cache reads/writes
        if prompt > 200000:
            mkey += "+p200k"
        elif prompt > 100000:
            mkey += "+p100k"
        acc = hours.setdefault(hour, {}).setdefault(mkey, [0] * N)
        for i in range(N):
            acc[i] += vals[i]
    return hours, len(best)


def gh_request(cfg, method, path, body=None, url=None):
    url = url or "https://api.github.com/repos/%s/contents/%s" % (cfg["repo"], path)
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
           "version": FORMAT, "hours": {h: merged[h] for h in sorted(merged)}}
    return json.dumps(doc, separators=(",", ":")).encode(), len(merged)


def upload_api(cfg, hours):
    path = "data/hosts/%s.json" % cfg["host"]
    remote = gh_request(cfg, "GET", path)
    sha, old = None, {}
    if remote:
        sha = remote["sha"]
        raw = remote.get("content") or ""
        if remote.get("encoding") != "base64" or not raw:  # files over 1 MB come without content
            blob = gh_request(cfg, "GET", None, url="https://api.github.com/repos/%s/git/blobs/%s" % (cfg["repo"], sha))
            raw = blob["content"]
        old = json.loads(base64.b64decode(raw).decode()).get("hours", {})
    content, n = merged_doc(cfg, old, hours)
    if content is None:
        return "unchanged"
    # Without author/committer, the API credits the commit to the token's owner (your account).
    ident = {"name": bot_name(cfg), "email": BOT_EMAIL}
    body = {"message": "usage: %s" % cfg["host"], "content": base64.b64encode(content).decode(),
            "author": ident, "committer": ident}
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
    ident = ["-c", "user.name=" + bot_name(cfg), "-c", "user.email=" + BOT_EMAIL]
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
    if "--version" in argv:
        print("token-usage uploader version %d" % VERSION)
        return 0
    if "--dry-run" in argv:
        hours, n = compute_hours()
        days, models = {}, {}
        for h, per_model in hours.items():
            for mkey, v in per_model.items():
                tokens = sum(v[:5])
                days[h[:10]] = days.get(h[:10], 0) + tokens
                models[mkey] = models.get(mkey, 0) + tokens
        for d in sorted(days)[-10:]:
            print("%s  %15s tokens (UTC day)" % (d, "{:,}".format(days[d])))
        print("by model:")
        for mkey in sorted(models, key=models.get, reverse=True):
            print("  %-32s %15s tokens" % (mkey, "{:,}".format(models[mkey])))
        print("%d unique messages, %d hours, uploader version %d, host: %s" % (n, len(hours), VERSION, load_config()["host"]))
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
