#!/usr/bin/env python3.11
"""Find and clean up git worktrees, finished background sessions and offline Remote Control rows.

  tidy.py [report]            table of every worktree and background session, with class and reason
  tidy.py --apply             remove the Safe worktrees and `claude rm` the removable sessions
  tidy.py --auto              same as --apply, quiet, for cron; appends a block to ~/.claude/tidy.log
  tidy.py --backup PATH       Ask worktree: tag detached commits, save changes as a patch, then remove it
  tidy.py offline [--delete|--archive ID ...]   list (or delete/archive) offline Remote Control rows

Never deletes a local branch itself, never touches a repo's main working tree, never deletes or archives a
Remote Control row on its own. `claude rm` can delete a session's own worktree branch, so a session is only
removed when that branch has nothing unpushed.
Config: ~/.claude/tidy.json {"roots": [...], "keep": [...], "min_age_hours": 24} (created if missing).
"""
import argparse
import calendar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SKIP_DIRS = {"node_modules", ".cache", ".git"}
FINISHED = {"done", "failed", "stopped", "finished", "completed", "error", "exited", "killed"}
API = "https://api.anthropic.com"


class GitError(Exception):
    pass


def home(*p):
    return os.path.join(os.path.expanduser("~"), *p)


def real(p):
    return os.path.realpath(os.path.expanduser(p))


def inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def load_conf():
    path = home(".claude", "tidy.json")
    if not os.path.exists(path):
        conf = {"roots": [home()], "keep": ["decl-test-nightly"], "min_age_hours": 24}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(conf, f, indent=2)
            f.write("\n")
    with open(path) as f:
        conf = json.load(f)
    conf.setdefault("roots", [home()])
    conf.setdefault("keep", [])
    conf.setdefault("min_age_hours", 24)
    return conf


def git(cwd, *args):
    # GIT_OPTIONAL_LOCKS=0: `git status` must not rewrite the index, or every run would reset the age check.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    # core.fsmonitor=false: a repo's own config must not make a scan run a command (fsmonitor hooks run on status).
    p = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", cwd, *args], capture_output=True, text=True, env=env)
    if p.returncode:
        last = p.stderr.strip().splitlines()
        raise GitError(f"git {args[0]}: {last[-1] if last else f'exit {p.returncode}'}")
    return p.stdout


# ---------- discovery ----------

def find_repos(roots, depth=3):
    """{git common dir: a directory inside that repo}, for every repo under the roots (max depth)."""
    repos = {}
    for root in roots:
        stack = [(real(root), 0)]
        while stack:
            d, lvl = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            if any(e.name == ".git" for e in entries):
                try:
                    common = git(d, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                    repos.setdefault(real(common), d)
                except GitError:
                    pass
            if lvl < depth:
                for e in entries:
                    if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS and not e.name.startswith(".venv"):
                        stack.append((e.path, lvl + 1))
    return repos


def list_worktrees(common, anydir):
    """Linked worktrees of one repo (main working tree left out) as dicts."""
    out = git(anydir, "worktree", "list", "--porcelain")
    items, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
            items.append(cur)
        elif cur is not None and line:
            k, _, v = line.partition(" ")
            cur[k] = v
    if not items:
        return []
    main = items[0]["path"]
    admin = {}  # worktree path -> its admin dir in <common>/worktrees/<id>
    wdir = os.path.join(common, "worktrees")
    for name in os.listdir(wdir) if os.path.isdir(wdir) else []:
        try:
            with open(os.path.join(wdir, name, "gitdir")) as f:
                admin[os.path.dirname(f.read().strip())] = os.path.join(wdir, name)
        except OSError:
            pass
    res = []
    for it in items[1:]:
        p = it["path"]
        a = admin.get(p) or admin.get(real(p))
        lockfile = a and os.path.join(a, "locked")
        locked = "locked" in it or bool(lockfile and os.path.exists(lockfile))
        reason = it.get("locked", "")
        if locked and not reason and lockfile and os.path.exists(lockfile):
            with open(lockfile) as f:
                reason = f.read().strip()
        res.append({
            "path": p, "real": real(p), "repo": main, "common": common, "admin": a,
            "head": it.get("HEAD", ""), "branch": it.get("branch", "").removeprefix("refs/heads/"),
            "detached": "detached" in it, "locked": locked, "lock_reason": reason,
        })
    return res


def proc_cwds():
    """[(pid, real cwd)] of every readable process, or None when /proc is not there (then nothing is removed)."""
    if not os.path.isdir("/proc"):
        return None
    res = []
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                res.append((int(pid), os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))))
            except OSError:
                pass
    return res


def claude_exe():
    for c in (os.environ.get("TIDY_CLAUDE"), shutil.which("claude"), home("bin", "claude"), home(".local", "bin", "claude")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def parse_ts(v):
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else v
    if isinstance(v, str) and v:
        try:
            return calendar.timegm(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"))  # state.json times are UTC
        except ValueError:
            return None
    return None


def sessions():
    """(list of sessions, error). On any error the list is None and nothing gets removed."""
    exe = claude_exe()
    if not exe:
        return None, "claude CLI not found (set PATH or TIDY_CLAUDE)"
    try:
        p = subprocess.run([exe, "agents", "--json", "--all"], capture_output=True, text=True, timeout=120)
        if p.returncode:
            return None, f"claude agents failed: {p.stderr.strip()[:200]}"
        data = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return None, f"claude agents failed: {e}"
    res = []
    for s in data:
        sid = s.get("id")
        st = {}
        jobdir = home(".claude", "jobs", sid) if sid else None
        if jobdir:
            try:
                with open(os.path.join(jobdir, "state.json")) as f:
                    st = json.load(f)
            except (OSError, ValueError):
                pass
        state = s.get("state") or s.get("status") or st.get("state") or ""
        kind = s.get("kind", "")
        live = kind != "background" or state.lower() not in FINISHED
        paths = [real(x) for x in (s.get("cwd"), st.get("worktreePath")) if x]
        res.append({
            "id": sid, "name": s.get("name") or "", "kind": kind, "state": state, "live": live,
            "paths": paths, "worktree": real(st["worktreePath"]) if st.get("worktreePath") else None,
            "jobdir": real(jobdir) if jobdir else None, "bridge": st.get("bridgeSessionId"),
            "branch": st.get("worktreeBranch"), "cwd": s.get("cwd"), "state_ok": bool(st),
            "updated": parse_ts(st.get("lastTerminalAt") or st.get("updatedAt")) or parse_ts(s.get("startedAt")),
        })
    return res, None


# ---------- classification ----------

def walk(path):
    """(bytes on disk, [nested dirs that hold a .git]) below a worktree; symlinks are not followed."""
    size, nested, stack = 0, [], [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.name == ".git" and d != path:
                        nested.append(os.path.relpath(d, path))
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        size += e.stat(follow_symlinks=False).st_blocks * 512
                except OSError:
                    pass
    return size, nested


def default_refs(repo):
    refs = []
    for b in ("main", "master"):
        if subprocess.run(["git", "-C", repo, "rev-parse", "-q", "--verify", f"refs/heads/{b}"], capture_output=True).returncode == 0:
            refs.append(f"refs/heads/{b}")
            break
    p = subprocess.run(["git", "-C", repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], capture_output=True, text=True)
    if p.returncode == 0 and p.stdout.strip():
        refs.append(p.stdout.strip())
    return refs


def unpushed(repo, sha):
    """Commits of `sha` on no remote-tracking ref and not patch-equivalent to (or in) a default branch."""
    revs = git(repo, "rev-list", sha, "--not", "--remotes").split()
    for d in default_refs(repo) if revs else []:
        plus = {line[2:] for line in git(repo, "cherry", d, sha).splitlines() if line.startswith("+ ")}
        revs = [r for r in revs if r in plus]
    return revs


def shas_in(path):
    """Object names in the first two columns of a reflog file, or of every file (reflogs, loose refs) below a dir."""
    files = [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
    out = set()
    for fn in files:
        try:
            with open(fn) as f:
                for line in f:
                    out.update(x for x in line.split()[:2] if re.fullmatch(r"[0-9a-f]{40}", x))
        except OSError:
            pass
    out.discard("0" * 40)
    return out


def reflog_lost(wt):
    """(commits, tips) that only this worktree's HEAD reflog or private refs hold: they go with its admin dir."""
    if not wt["admin"]:
        return [], []
    kept = shas_in(os.path.join(wt["common"], "logs", "refs", "heads"))  # branch reflogs survive the removal
    tips = (shas_in(os.path.join(wt["admin"], "logs")) | shas_in(os.path.join(wt["admin"], "refs"))) - kept - {wt["head"]}
    if not tips:
        return [], []
    lost = git(wt["repo"], "rev-list", *sorted(tips), "--not", "--remotes", "--branches", "--tags", *sorted(kept)).split()
    return lost, sorted(tips & set(lost))  # a tip outside `lost` is reachable from a kept ref, and so are its ancestors


def git_status(path):
    out = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
    tracked = untracked = 0
    parts, i = out.split("\0"), 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if not e:
            continue
        if e[:2] == "??":
            untracked += 1
        else:
            tracked += 1
            if e[0] in "RC":
                i += 1  # rename/copy: the source path follows
    return tracked, untracked


def classify(wt, conf, procs, sess, now=None):
    """Sets wt['cls'] (keep/ask/safe) and wt['reasons']. Keep wins over Ask; any doubt is Keep or Ask."""
    now = now or time.time()
    p, keep, ask = wt["real"], [], []
    exists = os.path.isdir(wt["path"])
    if wt["locked"]:
        keep.append("locked" + (f" ({wt['lock_reason']})" if wt["lock_reason"] else ""))
    for k in conf["keep"]:
        if os.path.basename(wt["path"].rstrip("/")) == k or real(k) == p:
            keep.append("on keep list")
    if procs is None:
        keep.append("cannot read /proc")
    else:
        pids = [pid for pid, cwd in procs if inside(cwd, p)]
        if pids:
            keep.append(f"process cwd inside (pid {', '.join(map(str, pids[:3]))})")
    if sess is None:
        keep.append("session list unavailable")
    else:
        for s in sess:
            if s["live"] and (any(inside(x, p) for x in s["paths"]) or (s["jobdir"] and inside(p, s["jobdir"]))):
                keep.append(f"live session {s['name'] or s['id']} ({s['state']})")
    if wt["admin"]:
        mt = [os.path.getmtime(os.path.join(wt["admin"], f)) for f in ("HEAD", "index") if os.path.exists(os.path.join(wt["admin"], f))]
        age = (now - max(mt)) / 3600 if mt else None
        if age is not None and age < conf["min_age_hours"]:
            keep.append(f"touched {age:.0f}h ago")
    wt["size"] = None
    try:
        if not exists:
            ask.append("directory missing, still registered (prune)")
        else:
            tracked, untracked = git_status(wt["path"])
            if tracked:
                ask.append(f"{tracked} tracked change(s)")
            if untracked:
                ask.append(f"{untracked} untracked file(s)")
        if wt["head"]:
            n = len(unpushed(wt["repo"], wt["head"]))
            if n:
                ask.append(f"{n} unpushed commit(s)" + (" on detached HEAD" if wt["detached"] else f" on {wt['branch']}"))
        lost = reflog_lost(wt)[0]
        if lost:
            ask.append(f"{len(lost)} commit(s) only in this worktree's HEAD reflog")
    except GitError as e:
        keep.append(f"git error: {e}")
    if exists and not keep:
        wt["size"], nested = walk(wt["path"])
        if nested:
            ask.append(f"holds nested git repo(s): {', '.join(nested[:3])}")
    wt["cls"] = "keep" if keep else "ask" if ask else "safe"
    wt["reasons"] = keep or ask or ["clean, on a remote or the default branch"]
    return wt


def branch_risk(s):
    """claude rm can delete the session's worktree branch; refuse when that branch holds unpushed commits."""
    if not s["branch"]:
        return None
    if not s["cwd"] or not os.path.isdir(s["cwd"]):
        return f"cannot check branch {s['branch']}: session cwd {s['cwd']} is gone"
    try:
        rc = subprocess.run(["git", "-C", s["cwd"], "rev-parse", "-q", "--verify", f"refs/heads/{s['branch']}"], capture_output=True).returncode
        if rc == 1:
            return None  # branch no longer exists
        if rc:
            return f"cannot check branch {s['branch']} (git exit {rc})"
        n = len(unpushed(s["cwd"], f"refs/heads/{s['branch']}"))
        return f"branch {s['branch']} has {n} unpushed commit(s)" if n else None
    except GitError as e:
        return f"cannot check branch {s['branch']}: {e}"


def classify_sessions(sess, wts, conf, now=None):
    """Background sessions: rm (finished, worktree safe or gone), ask (finished, worktree holds something), live."""
    now = now or time.time()
    by_path = {w["real"]: w for w in wts}
    res = []
    for s in sess or []:
        if s["kind"] != "background":
            continue
        age = (now - s["updated"]) / 3600 if s["updated"] else None
        row = {**s, "age": age}
        if s["live"]:
            row["action"], row["reason"] = "live", "running or waiting; report only"
        else:
            mine = [w for w in wts if (s["worktree"] and w["real"] == s["worktree"]) or any(inside(x, w["real"]) for x in s["paths"])
                    or (s["jobdir"] and inside(w["real"], s["jobdir"]))]
            bad = [w for w in mine if w["cls"] != "safe"]
            if not s["state_ok"]:
                row["action"], row["reason"] = "ask", "no readable ~/.claude/jobs/<id>/state.json: its worktree is unknown"
            elif s["worktree"] and s["worktree"] not in by_path and os.path.exists(s["worktree"]):
                row["action"], row["reason"] = "ask", f"worktree {s['worktree']} exists but is not registered"
            elif bad:
                w = bad[0]
                row["action"], row["reason"] = "ask", f"worktree {w['path']} is {w['cls']}: {'; '.join(w['reasons'])}"
            elif (b := branch_risk(s)):
                row["action"], row["reason"] = "ask", b
            elif age is None or age < conf["min_age_hours"]:
                row["action"], row["reason"] = "keep", f"finished {age:.0f}h ago" if age is not None else "finish time unknown"
            else:
                row["action"], row["reason"] = "rm", "finished; worktree " + ("safe" if mine else "gone or none")
        res.append(row)
    return res


def scan(conf, fetch=False):
    repos = find_repos(conf["roots"])
    procs = proc_cwds()
    sess, serr = sessions()
    wts, errors = [], ([serr] if serr else [])
    if procs is None:
        errors.append("cannot read /proc: nothing will be removed")
    for common, d in sorted(repos.items()):
        if fetch:
            try:
                git(d, "fetch", "--all", "--prune", "-q")
            except GitError as e:
                errors.append(f"{d}: {e}")
        try:
            wts += list_worktrees(common, d)
        except GitError as e:
            errors.append(f"{d}: {e}")
    for w in wts:
        classify(w, conf, procs, sess)
    return wts, classify_sessions(sess, wts, conf), sess, errors


# ---------- output ----------

def report(wts, srows, errors, out=sys.stdout):
    order = {"safe": 0, "ask": 1, "keep": 2}
    print("WORKTREES", file=out)
    for w in sorted(wts, key=lambda w: (order[w["cls"]], w["path"])):
        head = w["branch"] or f"detached {w['head'][:8]}"
        size = human(w["size"]) if w["size"] is not None else "-"
        print(f"  {w['cls']:<5} {size:>7}  {w['path']}  [{head}]  {'; '.join(w['reasons'])}", file=out)
    print("SESSIONS (background)", file=out)
    for s in sorted(srows, key=lambda s: s["action"]):
        age = f"{s['age']:.0f}h" if s["age"] is not None else "?"
        print(f"  {s['action']:<5} {s['id']}  {s['name'] or '-'}  {s['state']}  {age}  {s['reason']}", file=out)
    for e in errors:
        print(f"WARNING: {e}", file=out)
    print(summary(wts, srows), file=out)


def summary(wts, srows):
    c = {k: sum(w["cls"] == k for w in wts) for k in ("safe", "ask", "keep")}
    free = sum(w["size"] or 0 for w in wts if w["cls"] == "safe")
    sc = {k: sum(s["action"] == k for s in srows) for k in ("rm", "ask", "keep", "live")}
    return (f"worktrees: {c['safe']} safe ({human(free)}), {c['ask']} ask, {c['keep']} keep; "
            f"sessions: {sc['rm']} rm, {sc['ask']} ask, {sc['keep']} recent, {sc['live']} live")


# ---------- actions ----------

def apply(conf, quiet=False):
    wts, srows, sess, errors = scan(conf)
    lines = [summary(wts, srows)]
    if any(e.startswith(("claude", "session", "cannot read /proc")) for e in errors) or sess is None:
        lines += [f"ABORT: {e}" for e in errors] + ["ABORT: removed nothing"]
        return finish(lines, quiet, 1)
    lines += [f"warning: {e}" for e in errors]
    try:
        act(conf, wts, srows, lines)
    except Exception as e:  # whatever was already removed still gets its log block
        lines.append(f"ABORT: {type(e).__name__}: {e}")
        return finish(lines, quiet, 1)
    return finish(lines, quiet, 0)


def act(conf, wts, srows, lines):
    exe = claude_exe() or "claude"  # the scan listed sessions, so it was found
    for s in srows:
        if s["action"] != "rm":
            continue
        now_sess, _ = sessions()  # it may have been resumed since the scan
        cur = [x for x in now_sess or [] if x["id"] == s["id"]]
        if not cur or cur[0]["live"] or cur[0]["updated"] != s["updated"]:
            lines.append(f"skip session {s['id']} {s['name']}: changed since the scan")
            continue
        # claude rm refuses dirty/unpushed worktrees itself; never pass --discard-unpushed or --force-remove-worktree.
        p = subprocess.run([exe, "rm", s["id"]], capture_output=True, text=True, timeout=300)
        msg = (p.stdout + p.stderr).strip().replace("\n", " | ")[:300]
        lines.append(f"{'rm' if p.returncode == 0 else 'rm refused'} session {s['id']} {s['name']}: {msg}")
    procs = proc_cwds()
    sess, _ = sessions()
    for w in wts:
        if w["cls"] != "safe" or not os.path.isdir(w["path"]):
            continue
        fresh = [x for x in list_worktrees(w["common"], w["repo"]) if x["real"] == w["real"]]
        if not fresh or classify(fresh[0], conf, procs, sess)["cls"] != "safe":
            lines.append(f"skip {w['path']}: no longer safe ({'; '.join(fresh[0]['reasons']) if fresh else 'gone'})")
            continue
        try:
            git(w["repo"], "worktree", "remove", w["path"])  # never --force: git's own dirty check is the last guard
            lines.append(f"removed {w['path']} ({human(w['size'] or 0)})")
        except GitError as e:
            lines.append(f"failed {w['path']}: {e}")


def finish(lines, quiet, code):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(home(".claude", "tidy.log"), "a") as f:
        f.write(f"== {stamp} {'auto' if quiet else 'apply'}\n" + "".join(f"  {x}\n" for x in lines))
    if not quiet:
        print("\n".join(lines))
    return code


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]", "-", s).strip("-.") or "wt"


def backup(conf, path):
    target = real(path)
    wts, _, _, errors = scan(conf)
    match = [w for w in wts if w["real"] == target or w["path"] == path]
    if not match:
        print(f"{path}: not a registered linked worktree under the configured roots", file=sys.stderr)
        return 1
    w = match[0]
    if w["cls"] == "keep" or any(e.startswith(("claude", "session", "cannot")) for e in errors):
        print(f"refused: {w['path']} is keep ({'; '.join(w['reasons'])})", file=sys.stderr)
        return 1
    exists = os.path.isdir(w["path"])
    if exists and walk(w["path"])[1]:
        print(f"refused: {w['path']} holds nested git repos; handle them first", file=sys.stderr)
        return 1
    name, stamp = safe_name(os.path.basename(w["path"].rstrip("/"))), time.strftime("%Y%m%d-%H%M%S")
    done = []
    def tag(sha, suffix=""):
        ref = f"refs/tags/backup/wt-{name}{suffix}"
        if subprocess.run(["git", "-C", w["repo"], "rev-parse", "-q", "--verify", ref], capture_output=True).returncode == 0:
            ref += f"-{stamp}"
        git(w["repo"], "update-ref", ref, sha, "")  # "" = must not exist yet; never overwrite a backup
        done.append(f"tagged {sha[:8]} as {ref.removeprefix('refs/tags/')}")

    try:
        if w["detached"] and w["head"] and unpushed(w["repo"], w["head"]):
            tag(w["head"])
        elif w["branch"]:
            done.append(f"commits stay on branch {w['branch']}")
        for sha in reflog_lost(w)[1]:
            tag(sha, f"-reflog-{sha[:8]}")
        if exists and any(git_status(w["path"])):
            git(w["path"], "add", "-A", "-N")  # intent-to-add, so new files land in the patch too
            # plumbing-safe diff: user config (color, noprefix, external diff, textconv) must not shape the patch
            patch = git(w["path"], "diff", "HEAD", "--binary", "--no-color", "--no-ext-diff", "--no-textconv",
                        "--src-prefix=a/", "--dst-prefix=b/")
            if not patch.strip():
                raise GitError("changes found but the patch came out empty")
            check = subprocess.run(["git", "-C", w["path"], "apply", "-R", "--check", "--binary"], input=patch, text=True, capture_output=True)
            if check.returncode:
                raise GitError(f"patch does not reverse-apply cleanly: {check.stderr.strip()[:200]}")
            d = home("worktree-backups")
            os.makedirs(d, exist_ok=True)
            f = os.path.join(d, f"{safe_name(os.path.basename(w['repo']))}-{name}.patch")
            if os.path.exists(f):
                f = f[:-6] + f"-{stamp}.patch"
            with open(f, "x") as fh:
                fh.write(patch)
            done.append(f"saved changes to {f} (apply with: git apply --index {f})")
        git(w["repo"], "worktree", "remove", "--force", w["path"])
        done.append(f"removed {w['path']}")
    except (GitError, OSError) as e:
        print("\n".join(done + [f"stopped: {e}; worktree left in place"]), file=sys.stderr)
        return 1
    print("\n".join(done))
    return 0


# ---------- Remote Control offline rows ----------

def api(method, path):
    # IPv6 to api.anthropic.com stalls ~20s per request on the VDI; try IPv4 first.
    gai = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: sorted(gai(*a, **k), key=lambda r: r[0] != socket.AF_INET)
    try:
        with open(home(".claude", ".credentials.json")) as f:
            c = json.load(f).get("claudeAiOauth") or {}
        if not c.get("accessToken") or c.get("expiresAt", 0) / 1000 < time.time():
            raise SystemExit("OAuth token missing or expired: run any claude command once to refresh it")
        h = {"Authorization": "Bearer " + c["accessToken"], "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        req = urllib.request.Request(API + path, headers=h, method=method, data=b"{}" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read()[:300].decode("utf8", "replace")}
    finally:
        socket.getaddrinfo = gai


def offline_rows():
    rows, cursor = [], None
    for _ in range(10):  # the CLI's own page budget
        code, d = api("GET", "/v1/code/sessions?limit=100" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else ""))
        if code != 200:
            raise SystemExit(f"listing sessions failed: HTTP {code} {d.get('error', '')}")
        for r in d.get("data", []):
            t = (r.get("title") or "").strip()
            if (r.get("status") == "active" and r.get("environment_kind") == "bridge" and r.get("connection_status") == "disconnected"
                    and re.match(r"^(session|cse)_[A-Za-z0-9_-]+$", r.get("id", ""))
                    and "__CBU_POOLED__" not in t and t != "__warming__" and not t.startswith("ditto:")):
                rows.append(r)
        cursor = d.get("next_cursor")
        if not cursor or not d.get("data"):
            break
    return rows


def link(rid):
    return "https://claude.ai/code/session_" + rid.split("_", 1)[1]


def local_bridges():
    """{bridge key: (session, name, live)} from ~/.claude/jobs (background) and ~/.claude/sessions (interactive).
    Keyed by the part after cse_/session_; live is None when the session list failed."""
    sess, _ = sessions()
    live = {s["id"]: s["live"] for s in sess or []}
    res = {}
    for kind in ("jobs", "sessions"):
        d = home(".claude", kind)
        for e in os.listdir(d) if os.path.isdir(d) else []:
            f = os.path.join(d, e, "state.json") if kind == "jobs" else os.path.join(d, e)
            if kind == "sessions" and not e.endswith(".json"):
                continue
            try:
                with open(f) as fh:
                    st = json.load(fh)
            except (OSError, ValueError):
                continue
            b = st.get("bridgeSessionId")
            if not isinstance(b, str) or "_" not in b:
                continue
            if kind == "jobs":
                res[bridge_key(b)] = (e, st.get("name", ""), live.get(e, False) if sess is not None else None)
            else:
                res[bridge_key(b)] = (f"pid {st.get('pid')}", st.get("name", ""), pid_alive(st.get("pid")))
    return res


def bridge_key(rid):
    return rid.split("_", 1)[1]


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def offline(ids, delete=False):
    rows, local = offline_rows(), local_bridges()
    if not ids:
        for r in rows:
            j = local.get(bridge_key(r["id"]))
            note = ""
            if j:
                note = f"  [local session {j[0]} is finished: claude rm it first]" if j[2] is False else f"  [local session {j[0]} may be live: leave it]"
            print(f"{(r.get('last_event_at') or '')[:16]}  {r['id']}  {r.get('title') or '-'}  {link(r['id'])}{note}")
        print(f"{len(rows)} offline Remote Control row(s)")
        return 0
    known, code, acted = {r["id"]: r for r in rows}, 0, []
    for i in ids:
        i = "cse_" + i.rsplit("session_", 1)[1] if "session_" in i else i  # a claude.ai/code link works too
        j = local.get(bridge_key(i))
        if i not in known:
            print(f"refused {i}: not an offline row right now", file=sys.stderr); code = 1
        elif j and (j[2] or j[2] is None):
            print(f"refused {i}: belongs to live local session {j[0]} {j[1]}", file=sys.stderr); code = 1
        else:
            st, d = api("DELETE", f"/v1/code/sessions/{i}") if delete else api("POST", f"/v1/code/sessions/{i}/archive")
            ok = st == 200 or (st == 409 and not delete)  # 409 = already archived
            verb = "deleted" if delete else "archived"
            print(f"{verb if ok else 'failed'} {i} {known[i].get('title') or ''}" + ("" if ok else f": HTTP {st} {d.get('error', '')}"))
            code |= 0 if ok else 1
            acted += [i] if ok else []
    left = sorted({r["id"] for r in offline_rows()} & set(acted)) if acted else []
    for i in left:
        print(f"still listed after the call: {i}", file=sys.stderr)
    return 1 if left else code


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="report", choices=["report", "offline"])
    ap.add_argument("--apply", action="store_true", help="remove Safe worktrees and removable sessions")
    ap.add_argument("--auto", action="store_true", help="like --apply, quiet, logs to ~/.claude/tidy.log")
    ap.add_argument("--backup", metavar="PATH", help="back up and remove one Ask worktree")
    ap.add_argument("--fetch", action="store_true", help="git fetch --prune each repo first")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--delete", nargs="+", metavar="ID", help="with offline: delete these rows (gone for good)")
    g.add_argument("--archive", nargs="+", metavar="ID", help="with offline: archive these rows (kept in claude.ai/code)")
    a = ap.parse_args(argv)
    conf = load_conf()
    if a.cmd == "offline":
        return offline(a.delete or a.archive or [], delete=bool(a.delete))
    if a.backup:
        return backup(conf, a.backup)
    if a.apply or a.auto:
        return apply(conf, quiet=a.auto)
    wts, srows, _, errors = scan(conf, fetch=a.fetch)
    report(wts, srows, errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
