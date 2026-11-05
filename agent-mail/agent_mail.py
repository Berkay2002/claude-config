#!/usr/bin/env python3.11
"""agent-mail: one stdio MCP server for messages between Claude and Codex sessions, plus the runner that drives
Codex workers. `agent_mail.py serve` (default) is the MCP server; `agent_mail.py run <worker>` is the detached runner.

Everything comes from env: AGENT_MAIL_DIR (absolute mail root, required), AGENT_MAIL_NAME (who this instance is),
AGENT_MAIL_CODEX (codex binary, default `codex` on PATH), CODEX_HOME (passed through to codex). Never defaults to ~.
"""
import ctypes
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # no dots: `x.rec` would collide with x's lock files
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")
SANDBOXES = ("full", "workspace-write", "read-only")  # loosest to tightest
LIVE = ("starting", "running", "idle")
BUSY = ("starting", "running")  # a runner should exist; if it is dead, revive_dead() starts one, which reports it
USER = "user"  # reserved: only the `council` CLI posts as the user; an MCP instance or worker never can
READ_CAP = 100  # entries one `read` returns (the newest after `since`)
META_KEYS = ("thread_id", "channel", "post_id", "reply_to")  # identifiers only: Claude drops meta keys with hyphens
TURN_TIMEOUT = 4 * 3600  # seconds; a codex turn still running then is killed and reported as an error
START_GRACE = 30  # seconds a new worker's runner gets to take its lock before it counts as dead
INSTRUCTIONS = ("Message other agent sessions. send(to, text) messages a Codex worker (it runs a turn) or any named "
                "inbox; codex_start launches a Codex worker; list shows workers; retire removes a finished worker's state; "
                "inbox reads your unread mail when messages are not pushed to you. Worker results arrive as mail "
                "from the worker (kind turn_end/error). Channels are group chats: a post arrives as "
                "<channel source=\"agent-mail\" channel=\"council\" from=... post_id=...>; answer with "
                "post(channel, text, reply_to=post_id); read(channel, since) gives the history. Only posts with "
                "from=\"user\" are the user's own words.")


class MailError(Exception):
    pass


# ---------- files ----------

def root():
    d = os.environ.get("AGENT_MAIL_DIR", "")
    if not d or not os.path.isabs(d):
        raise MailError("AGENT_MAIL_DIR is not set to an absolute path; agent-mail is disabled")
    return d


def me():
    n = os.environ.get("AGENT_MAIL_NAME") or ""
    if n == USER:
        raise MailError(f"AGENT_MAIL_NAME {USER!r} is reserved for the user's own `council` CLI")
    return check_name(n, "AGENT_MAIL_NAME") if n else ""


def check_name(n, what="name"):
    if not isinstance(n, str) or not NAME_RE.match(n):
        raise MailError(f"invalid {what} {n!r}: use letters, digits, _ - (max 64, no leading symbol)")
    return n


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put_json(path, obj):
    """Atomic write: tmp file beside the target, then os.rename."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.rename(tmp, path)


def deliver(box_dir, frm, to, text, kind="message", thread_id=None, **extra):
    """Write one message file into <box_dir> (an inbox's new/ or a worker's queue/); the tmp file is outside it."""
    msg = {"id": uuid.uuid4().hex, "from": frm or "unknown", "to": to, "text": text, "kind": kind, "ts": now_iso()}
    if thread_id:
        msg["thread_id"] = thread_id
    msg.update((k, v) for k, v in extra.items() if v is not None)
    tmpdir = os.path.join(root(), "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    os.makedirs(box_dir, exist_ok=True)
    tmp = os.path.join(tmpdir, msg["id"])
    with open(tmp, "w") as f:
        json.dump(msg, f)
    os.rename(tmp, os.path.join(box_dir, f"{time.time_ns():020d}-{msg['id']}.json"))
    return msg


def inbox_dir(name, sub="new"):
    return os.path.join(root(), "inbox", check_name(name, "recipient"), sub)


def claim(src, dst):
    """Yield (path in dst, message) for each file in src, claimed by renaming it into dst only when the caller asks for
    the next one (exactly-once even with several readers)."""
    try:
        names = sorted(os.listdir(src))
    except FileNotFoundError:
        return
    os.makedirs(dst, exist_ok=True)
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            os.rename(os.path.join(src, n), os.path.join(dst, n))
        except FileNotFoundError:
            continue  # another reader claimed it
        try:
            with open(os.path.join(dst, n)) as f:
                yield os.path.join(dst, n), json.load(f)
        except (OSError, ValueError):
            pass  # corrupt file stays in dst, not delivered


def take(src, dst):
    return [m for _, m in claim(src, dst)]


def claim_inbox(name):
    return take(inbox_dir(name), inbox_dir(name, "cur"))


def count_files(d):
    try:
        return sum(1 for n in os.listdir(d) if n.endswith(".json"))
    except FileNotFoundError:
        return 0


def wdir(name):
    return os.path.join(root(), "workers", check_name(name, "worker name"))


def wrec(name):
    return wdir(name) + ".json"


def read_worker(name):
    try:
        with open(wrec(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def update_worker(name, refuse_live=False, **fields):
    """Read-modify-write the worker record under flock; returns the new record. refuse_live: raise if it is live."""
    os.makedirs(os.path.dirname(wrec(name)), exist_ok=True)
    with open(wdir(name) + ".rec.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        rec = read_worker(name) or {"name": name}
        if refuse_live and rec.get("state") in LIVE:
            raise MailError(f"worker {name} already exists (state {rec['state']}); use send, or pick another name")
        rec.update(fields, updated=now_iso())
        put_json(wrec(name), rec)
        return rec


def runner_alive(name, pid):
    """Is `pid` still `agent_mail.py run <name>`? The cmdline check guards against pid reuse; a zombie has none."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
    except (OSError, TypeError, ValueError):
        return False
    return b"run" in args and name.encode() in args


def revive_dead(owner):
    """Start a runner for each worker of `owner` that is busy or has queued mail but no live runner (a dead runner's
    replacement mails the owner an error). Only the owner spawns runners, so each inherits the owner's full env."""
    wd = os.path.join(root(), "workers")
    for fn in sorted(os.listdir(wd)) if os.path.isdir(wd) else []:
        r = read_worker(fn[:-5]) if fn.endswith(".json") else None
        if not r or r.get("owner") != owner:
            continue
        if r.get("state") not in BUSY and not count_files(os.path.join(wdir(r["name"]), "queue")):
            continue
        if r.get("pid") is None:  # not started yet: give spawn_runner its moment
            age = datetime.now(timezone.utc) - datetime.fromisoformat(r.get("updated") or now_iso())
            if age.total_seconds() < START_GRACE:
                continue
        elif runner_alive(r["name"], r["pid"]):
            continue
        spawn_runner(r["name"])


# ---------- channels ----------

def chan_path(channel, ext):
    return os.path.join(root(), "channels", check_name(channel, "channel") + ext)


def members(channel):
    """The channel's members from channels/<channel>.json; refuses an unknown channel or a bad members file."""
    try:
        with open(chan_path(channel, ".json")) as f:
            m = json.load(f)["members"]
    except FileNotFoundError:
        raise MailError(f"unknown channel {channel!r} (no channels/{channel}.json)") from None
    except (ValueError, KeyError, TypeError):
        raise MailError(f'channels/{channel}.json must be {{"members": [names]}}') from None
    if not isinstance(m, list):
        raise MailError(f"channels/{channel}.json: members must be a list")
    return list(dict.fromkeys(check_name(n, f"member of {channel}") for n in m))


def member(channel, who):
    if who not in members(channel):
        raise MailError(f"{who or 'unknown'} is not a member of channel {channel}")


def history(channel):
    try:
        with open(chan_path(channel, ".jsonl"), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue  # a line cut short by a crash: skipped
        if isinstance(e, dict) and isinstance(e.get("id"), int):
            out.append(e)
    return out


def post(channel, frm, text, reply_to=None):
    """Append one entry under the channel lock (ids 1, 2, ... in file order), then mail a copy to every other member
    except the user, who reads the history. Returns the entry."""
    if not isinstance(text, str) or not text:
        raise MailError("text must be a non-empty string")
    if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)):
        raise MailError("reply_to must be a post id (integer)")
    mem = members(channel)
    if frm not in mem:
        raise MailError(f"{frm or 'unknown'} is not a member of channel {channel}")
    path = chan_path(channel, ".jsonl")
    with open(chan_path(channel, ".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        h = history(channel)  # shortcut: reads the whole file per post; fine for a chat, index it past ~10 MB
        e = {"id": (h[-1]["id"] if h else 0) + 1, "ts": now_iso(), "from": frm, "text": text}
        if reply_to is not None:
            e["reply_to"] = reply_to
        with open(path, "a+b") as f:
            torn = False
            if f.seek(0, os.SEEK_END):
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b"\n"  # a crash cut the last line short
            f.write((b"\n" if torn else b"") + (json.dumps(e) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        for m in mem:  # under the lock, so every inbox gets posts in id order
            if m not in (frm, USER):
                deliver(inbox_dir(m), frm, m, text, "post", channel=channel, post_id=e["id"], reply_to=reply_to)
    return e


def read_since(channel, since=None):
    """All entries after `since` (a post id, or an ISO time with zone), oldest first."""
    h = history(channel)
    if since is None or since == "":
        return h
    if isinstance(since, int) and not isinstance(since, bool) or re.fullmatch(r"[0-9]+", str(since)):
        return [e for e in h if e["id"] > int(since)]
    try:
        t = datetime.fromisoformat(str(since))
    except ValueError:
        t = None
    if t is None or t.tzinfo is None:
        raise MailError(f"since must be a post id or an ISO time with zone, got {since!r}")
    out = []
    for e in h:
        try:
            if datetime.fromisoformat(e.get("ts", "")) > t:
                out.append(e)
        except (ValueError, TypeError):
            pass  # an entry without a usable time can't be placed after `since`
    return out


def gated(m):
    """A channel copy whose sender is not (or no longer) a member of that channel is dropped, never shown."""
    if "channel" not in m:
        return True
    try:
        return m.get("from") in members(m["channel"])
    except MailError:
        return False


def my_channels():
    d = os.path.join(root(), "channels")
    out = []
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        try:
            if fn.endswith(".json") and me() in members(fn[:-5]):
                out.append(fn[:-5])
        except MailError:
            pass  # a broken members file hides that channel, not the list
    return out


# ---------- tools ----------

def tool_post(channel, text, reply_to=None):
    if not me():
        raise MailError("AGENT_MAIL_NAME is not set: this instance cannot post")
    return f"posted #{post(channel, me(), text, reply_to)['id']} to {channel}"


def tool_read(channel, since=None):
    member(channel, me())
    h = read_since(channel, since)
    return json.dumps({"posts": h[:READ_CAP], "more": len(h) > READ_CAP})  # page on with since=<last id>


def spawn_runner(name):
    """Detached, so a Claude restart or /mcp reconnect never kills a Codex turn. A second runner exits at the lock."""
    logs = os.path.join(wdir(name), "logs")
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, "runner.log"), "a") as log:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", name], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
    threading.Thread(target=p.wait, daemon=True).start()  # reap it: no zombie in a long-lived server


def tool_send(to, text):
    if not isinstance(text, str) or not text:
        raise MailError("text must be a non-empty string")
    if check_name(to, "recipient") == USER:
        raise MailError("the user reads no inbox: post to a channel they are in instead")
    rec = read_worker(to)
    if rec is not None:  # queue under the record lock: a retire between our check and the write would drop the mail
        with open(wdir(to) + ".rec.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            rec = read_worker(to)
            if rec is not None:
                deliver(os.path.join(wdir(to), "queue"), me(), to, text)
    if rec is not None:
        if rec.get("owner") != (me() or "unknown"):
            return f"queued for Codex worker {to}; its owner {rec.get('owner')} starts its runner (when it runs)"
        spawn_runner(to)
        return f"queued for Codex worker {to}; its runner is started and the result will arrive as mail"
    deliver(inbox_dir(to), me(), to, text)
    return f"delivered to inbox {to}"


def own_sandbox():
    """The sandbox this instance itself runs in: set by the runner for workers, absent (= full) for orchestrators."""
    return os.environ.get("AGENT_MAIL_SANDBOX") or "full"


def tool_codex_start(name, cwd, prompt, model=None, effort=None, sandbox=None):
    check_name(name, "worker name")
    if name == USER:  # a worker's mail is from its own name
        raise MailError(f"the name {USER!r} is reserved for the user")
    if not isinstance(prompt, str) or not prompt:
        raise MailError("prompt must be a non-empty string")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise MailError(f"cwd must be an existing absolute directory, got {cwd!r}")
    sandbox = sandbox or own_sandbox()  # default: no looser than the caller
    if sandbox not in SANDBOXES:
        raise MailError(f"sandbox must be one of {SANDBOXES}")
    if SANDBOXES.index(sandbox) < SANDBOXES.index(own_sandbox()):  # SANDBOXES runs from loosest to tightest
        raise MailError(f"a {own_sandbox()} worker cannot start a looser ({sandbox}) worker")
    for v, what in ((model, "model"), (effort, "effort")):
        if v is not None and not MODEL_RE.match(str(v)):
            raise MailError(f"invalid {what} {v!r}")
    owner = me() or "unknown"
    update_worker(name, refuse_live=True, thread_id=None, cwd=cwd, owner=owner, model=model, effort=effort,
                  sandbox=sandbox, pid=None, state="starting", last_message="", turns=0)
    for sub in ("queue", "taken"):  # a failed worker being restarted must not replay its old mail
        take(os.path.join(wdir(name), sub), os.path.join(wdir(name), "old"))
    deliver(os.path.join(wdir(name), "queue"), owner, name, prompt)
    spawn_runner(name)
    return f"worker {name} starting in {cwd} (sandbox {sandbox}); thread id and results arrive as mail from {name}"


def tool_retire(name, force=False, delete_thread=False):
    """Delete a worker's record and folder (its empty lock files stay, reused by a new worker). Holding the runner lock means no turn is running (and none starts)."""
    check_name(name, "worker name")
    owner = me() or "unknown"

    def mine():
        rec = read_worker(name)
        if rec is None:
            raise MailError(f"unknown worker {name}")
        if rec.get("owner") != owner:
            raise MailError(f"worker {name} belongs to {rec.get('owner')}; only its owner can retire it")
        return rec

    mine()
    with open(wdir(name) + ".lock", "w") as lk:  # exists with the record, so this creates nothing new
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MailError(f"worker {name}: turn running, retry after its turn_end") from None
        with open(wdir(name) + ".rec.lock", "w") as rk:  # same order as the runner: runner lock, then record lock
            fcntl.flock(rk, fcntl.LOCK_EX)
            rec = mine()
            pending = sum(count_files(os.path.join(wdir(name), sub)) for sub in ("queue", "taken"))
            if pending and not force:
                raise MailError(f"worker {name} still has {pending} undelivered message(s); they would be lost: "
                                "wait for its turn_end, or pass force=true to discard them")
            shutil.rmtree(wdir(name), ignore_errors=True)
            os.unlink(wrec(name))  # the lock files stay: a runner holding an old handle must share the next one's lock
    tid = rec.get("thread_id")
    kept = f"thread {tid} is kept (`codex delete --force {tid}` removes it)" if tid else "thread id was never recorded"
    if delete_thread and tid:  # after the state is gone: a failed delete only leaves the history, never a half worker
        try:
            p = subprocess.run([os.environ.get("AGENT_MAIL_CODEX") or "codex", "delete", "--force", "--", tid],
                               cwd=rec.get("cwd") if os.path.isdir(rec.get("cwd") or "") else None,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            kept = (f"thread {tid} is deleted" if p.returncode == 0 else
                    f"thread {tid} was NOT deleted (codex exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]})")
        except (OSError, subprocess.TimeoutExpired) as e:
            kept = f"thread {tid} was NOT deleted ({e})"
    return f"retired worker {name}" + (f" (discarded {pending} message(s))" if pending else "") + f". Its Codex {kept}."


def tool_list():
    if me():
        revive_dead(me())
    recs = []
    wd = os.path.join(root(), "workers")
    for fn in sorted(os.listdir(wd)) if os.path.isdir(wd) else []:
        if fn.endswith(".json"):
            r = read_worker(fn[:-5]) or {}
            recs.append({k: r.get(k) for k in ("name", "state", "thread_id", "cwd", "owner", "sandbox")}
                        | {"last": (r.get("last_message") or "").split("\n", 1)[0]})
    unread = count_files(inbox_dir(me())) if me() else None
    return json.dumps({"workers": recs, "unread": unread, "channels": my_channels() if me() else []})


def tool_codex_usage():
    """Codex's quota on the ChatGPT plan, live from `codex app-server` (account/rateLimits/read): 5-hour and weekly use,
    banked resets (each one zeroes both windows) and the spare weekly percent per day they add up to. Read-only."""
    p = subprocess.Popen([os.environ.get("AGENT_MAIL_CODEX") or "codex", "app-server"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        def call(i, method, params=None):
            p.stdin.write(json.dumps({"id": i, "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
        timer = threading.Timer(30, p.kill)  # a hung app-server must not hang the tool
        timer.start()
        call(1, "initialize", {"clientInfo": {"name": "agent-mail", "version": "1"}})
        p.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        call(2, "account/rateLimits/read")
        res = None
        for line in p.stdout:
            m = json.loads(line)
            if m.get("id") == 2:
                res = m
                break
        timer.cancel()
    except (OSError, ValueError) as e:
        raise MailError(f"codex app-server failed: {e}") from None
    finally:
        p.kill()
        p.wait()
    if not res or "result" not in res:
        raise MailError(f"codex app-server gave no rate limits: {(res or {}).get('error', 'no answer in 30 s')}")
    r, now = res["result"], time.time()
    lim = r.get("rateLimits") or {}

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None

    def window(w):
        w = w or {}
        return {"percent": w.get("usedPercent"), "resets": iso(w.get("resetsAt")),
                "days_left": round((w["resetsAt"] - now) / 86400, 2) if w.get("resetsAt") else None}
    five, week = window(lim.get("primary")), window(lim.get("secondary"))
    week_at = (lim.get("secondary") or {}).get("resetsAt")
    banked = sorted(({"expires": iso(c.get("expiresAt")), "days_left": round((c["expiresAt"] - now) / 86400, 2),
                      "before_week_reset": bool(week_at) and c["expiresAt"] < week_at}
                     for c in (r.get("rateLimitResetCredits") or {}).get("credits") or []
                     if c.get("status") == "available" and c.get("expiresAt")), key=lambda c: c["days_left"])
    left = None if week["percent"] is None else 100 - week["percent"]
    spare = round(left / max(week["days_left"], 0.1), 1) if left is not None and week["days_left"] else None
    # A reset that lapses before the weekly reset is only worth something if the week is used up before it lapses.
    lapse = next((c for c in banked if c["before_week_reset"]), None)
    burn = round(left / max(lapse["days_left"], 0.1), 1) if lapse and left is not None else None
    return json.dumps({"plan": lim.get("planType"), "usable": r.get("ordinaryUsageAllowed"),
                       "limit_reached": lim.get("rateLimitReachedType"), "five_hour": five, "week": week,
                       "banked_resets": banked, "spare_week_percent_per_day": spare,
                       "use_before_lapse_percent_per_day": burn})


def tool_inbox():
    if not me():
        raise MailError("AGENT_MAIL_NAME is not set: this instance has no inbox")
    revive_dead(me())  # an owner without push polls here: a dead runner must surface here too
    return json.dumps([m for m in claim_inbox(me()) if gated(m)])


TOOLS = {
    "send": (tool_send, "Send a message. `to` is a Codex worker (queued, starts a turn) or any inbox name.",
             {"to": {"type": "string"}, "text": {"type": "string"}}, ["to", "text"]),
    "codex_start": (tool_codex_start, "Start a Codex worker in `cwd` with `prompt`. sandbox: full (default, no "
                    "sandbox), read-only or workspace-write.",
                    {"name": {"type": "string"}, "cwd": {"type": "string"}, "prompt": {"type": "string"},
                     "model": {"type": "string"}, "effort": {"type": "string"},
                     "sandbox": {"type": "string", "enum": list(SANDBOXES)}}, ["name", "cwd", "prompt"]),
    "retire": (tool_retire, "Remove a Codex worker's state (record, queue, logs) once it is finished. Only its "
               "owner; refused while a turn runs or mail is undelivered (unless force). The Codex thread is kept unless delete_thread.",
               {"name": {"type": "string"}, "force": {"type": "boolean"}, "delete_thread": {"type": "boolean"}}, ["name"]),
    "list": (tool_list, "List Codex workers and this inbox's unread count.", {}, []),
    "codex_usage": (tool_codex_usage, "Codex's live quota: 5-hour and weekly use, banked resets with expiry, spare weekly "
                    "percent per day. Check it before starting Codex workers.", {}, []),
    "inbox": (tool_inbox, "Claim and return this instance's unread mail.", {}, []),
    "post": (tool_post, "Post to a channel (group chat): every other member gets it. reply_to: the post id you answer.",
             {"channel": {"type": "string"}, "text": {"type": "string"}, "reply_to": {"type": "integer"}},
             ["channel", "text"]),
    "read": (tool_read, f"A channel's history after `since` (a post id or ISO time with zone), oldest first, at most "
             f"{READ_CAP}; more=true means call again with since=<last id>.",
             {"channel": {"type": "string"}, "since": {"type": "string"}}, ["channel"]),
}


# ---------- MCP server ----------

def parent_loads_channel():
    """True when our parent (Claude starts stdio servers directly) was launched with the dev-channels flag for us.
    Without it Claude drops channel pushes, so a pushed message would be claimed and lost: leave it for `inbox`."""
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as f:
            args = f.read().decode(errors="replace").split("\0")
    except OSError:
        return False
    return (any(a.startswith("--dangerously-load-development-channels") for a in args)
            and any("server:agent-mail" in a for a in args))


class Server:
    def __init__(self, stdin=None, stdout=None):
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout
        self.wlock = threading.Lock()
        self.stop = threading.Event()
        self.client = ""
        self.poller = None

    def write(self, obj):
        with self.wlock:
            self.stdout.write(json.dumps(obj) + "\n")
            self.stdout.flush()

    def reply(self, rid, result=None, error=None):
        m = {"jsonrpc": "2.0", "id": rid}
        m["error" if error else "result"] = error or result
        self.write(m)

    def push(self, msg):
        meta = {"from": str(msg.get("from", "unknown")), "kind": str(msg.get("kind", "message"))}
        meta.update((k, str(msg[k])) for k in META_KEYS if msg.get(k) is not None)
        # a body must not open or close the <channel> wrapper Claude puts around it, e.g. to fake from="user"
        content = re.sub(r"(?i)<(/?)(channel)", r"&lt;\1\2", str(msg.get("text", "")))
        self.write({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                    "params": {"content": content, "meta": meta}})

    def poll(self, interval, push=True):
        """Start runners for our workers' queued mail, and (push) claim and push our mail one message at a time; a
        push that fails goes back to new/ for the next reader."""
        while not self.stop.is_set():
            try:
                new = inbox_dir(me())
                for path, m in claim(new, inbox_dir(me(), "cur")) if push else ():
                    if not gated(m):
                        continue  # stays in cur/, never pushed
                    try:
                        self.push(m)
                    except (OSError, ValueError):  # client gone (broken pipe, closed stdout)
                        os.rename(path, os.path.join(new, os.path.basename(path)))
                        return
                    if self.stop.is_set():
                        return
                revive_dead(me())
            except Exception:  # a bad file or a bug must not end push for the rest of the session
                traceback.print_exc()
            self.stop.wait(interval)

    def initialize(self, p):
        self.client = (p.get("clientInfo") or {}).get("name", "")
        caps = {"tools": {}}
        if me():
            caps["experimental"] = {"claude/channel": {}}
        return {"protocolVersion": p.get("protocolVersion") or "2025-06-18", "capabilities": caps,
                "serverInfo": {"name": "agent-mail", "version": "1"}, "instructions": INSTRUCTIONS}

    def call(self, p):
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in TOOLS:
            raise MailError(f"unknown tool {name!r}")
        try:
            return {"content": [{"type": "text", "text": TOOLS[name][0](**args)}]}
        except MailError as e:
            return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        except TypeError as e:
            return {"content": [{"type": "text", "text": f"error: bad arguments: {e}"}], "isError": True}

    def handle(self, m):
        method, rid, p = m.get("method"), m.get("id"), m.get("params") or {}
        if method == "notifications/initialized":
            if me() and not self.poller:
                try:
                    root()
                except MailError:
                    return
                push = self.client == "claude-code" and parent_loads_channel()
                self.poller = threading.Thread(target=self.poll, daemon=True,
                                               args=(float(os.environ.get("AGENT_MAIL_POLL", 1)), push))
                self.poller.start()
            return
        if rid is None:
            return  # unknown notification
        if method == "initialize":
            self.reply(rid, self.initialize(p))
        elif method == "ping":
            self.reply(rid, {})
        elif method == "tools/list":
            self.reply(rid, {"tools": [{"name": n, "description": d, "inputSchema": {
                "type": "object", "properties": pr, "required": rq}} for n, (_, d, pr, rq) in TOOLS.items()]})
        elif method == "tools/call":
            try:
                self.reply(rid, self.call(p))
            except MailError as e:
                self.reply(rid, error={"code": -32602, "message": str(e)})
        else:  # includes Claude's `server/discover` probe: it falls back to initialize
            self.reply(rid, error={"code": -32601, "message": f"method not found: {method}"})

    def serve(self):
        for line in self.stdin:
            if not line.strip():
                continue
            try:
                m = json.loads(line)
                if not isinstance(m, dict):
                    raise ValueError
            except ValueError:
                self.reply(None, error={"code": -32700, "message": "parse error"})
                continue
            try:
                self.handle(m)
            except Exception as e:  # keep serving; a bug in one request must not drop the connection
                if m.get("id") is not None:
                    self.reply(m["id"], error={"code": -32603, "message": f"internal error: {e}"})
        self.stop.set()
        if self.poller:  # let an in-flight push finish (or go back to new/) before the process exits
            self.poller.join(5)


# ---------- runner ----------

def codex_args(rec, tid, last, text):
    """Argv for one turn. `exec resume` rejects -C/-s/--add-dir, so cwd comes from Popen and the sandbox via -c."""
    sb = rec.get("sandbox") or "full"
    a = [os.environ.get("AGENT_MAIL_CODEX") or "codex", "exec"] + (["resume"] if tid else []) + [
        "--json", "--skip-git-repo-check"]
    if not tid:
        a += ["-C", rec["cwd"]]
    if rec.get("model"):
        a += ["-m", rec["model"]]
    if rec.get("effort"):
        a += ["-c", "model_reasoning_effort=" + json.dumps(rec["effort"])]
    if sb == "full":
        a += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        a += ["-c", "approval_policy=" + json.dumps("never"),
              # codex exec denies MCP tool calls under a restricted sandbox unless approved per server
              "-c", 'mcp_servers.agent-mail.default_tools_approval_mode="approve"']
        a += ["-c", "sandbox_mode=" + json.dumps(sb)] if tid else ["-s", sb]
    # Codex hands MCP servers a filtered env: CODEX_HOME/AGENT_MAIL_CODEX must be injected or a worker-spawned runner has no auth.
    env = {"AGENT_MAIL_NAME": rec["name"], "AGENT_MAIL_DIR": root(), "AGENT_MAIL_SANDBOX": sb}
    for k in ("CODEX_HOME", "AGENT_MAIL_CODEX"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    a += ["-c", "mcp_servers.agent-mail.command=" + json.dumps(sys.executable),
          "-c", "mcp_servers.agent-mail.args=" + json.dumps([os.path.abspath(__file__), "serve"])]
    for k, v in env.items():
        a += ["-c", f"mcp_servers.agent-mail.env.{k}={json.dumps(v)}"]
    # `--`: a prompt like "-x" or "review" would otherwise be parsed as a codex flag or subcommand
    return a + ["-o", last, "--"] + ([tid] if tid else []) + [text]


def format_turn(rec, tid, msgs):
    if not tid and len(msgs) == 1 and msgs[0]["from"] == rec.get("owner"):
        return msgs[0]["text"]  # the start prompt, as the owner wrote it
    text = "\n\n".join(f"From {m['from']}:\n{m['text']}" for m in msgs)
    if rec.get("interrupted") and tid:
        text = "(Your previous turn was cut off when its runner died; these messages are sent again.)\n\n" + text
    return text


_prctl = ctypes.CDLL(None, use_errno=True).prctl  # resolved before any fork: no dlopen in the child


def _die_with_runner():
    """preexec_fn for codex: SIGKILL it when the runner dies, so a revived runner never resumes the thread twice."""
    _prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG; tied to the forking thread, the runner's main thread


def _kill_group(p, killed):
    killed.append(1)
    try:
        os.killpg(p.pid, signal.SIGKILL)  # codex runs in its own session: this takes its commands down too
    except ProcessLookupError:
        pass


def run_turn(name, rec, msgs):
    """One codex turn. Returns (ok, last_message_or_error_text)."""
    n = (rec.get("turns") or 0) + 1
    logs = os.path.join(wdir(name), "logs")
    os.makedirs(logs, exist_ok=True)
    last, jsonl, err = (os.path.join(logs, f"turn-{n}.{x}") for x in ("last.md", "jsonl", "err"))
    if os.path.exists(last):
        os.unlink(last)
    tid = rec.get("thread_id")
    argv = codex_args(rec, tid, last, format_turn(rec, tid, msgs))
    update_worker(name, state="running", turns=n)
    killed = []
    env = dict(os.environ, AGENT_MAIL_NAME=name)  # codex's own commands act as the worker, not as its sender
    with open(jsonl, "w") as jf, open(err, "w") as ef:
        try:
            p = subprocess.Popen(argv, cwd=rec["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=ef,
                                 encoding="utf-8", errors="replace", env=env, start_new_session=True,
                                 preexec_fn=_die_with_runner)
        except OSError as e:
            return False, f"could not start codex ({argv[0]}): {e}; log: {err}"
        timer = threading.Timer(TURN_TIMEOUT, _kill_group, (p, killed))
        timer.start()
        try:
            tid, failed, rc = _read_events(name, rec, p, jf, tid)
        finally:
            timer.cancel()
            timer.join()  # no thread may exist at the next Popen (preexec_fn)
            p.stdout.close()  # type: ignore[union-attr]
    text = ""
    if os.path.exists(last):
        with open(last, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    if killed:
        return False, f"codex turn {n} timed out after {TURN_TIMEOUT} s and was killed; log: {jsonl} (stderr {err})"
    if rc != 0 or failed or not text:
        why = f"exit code {rc}" + (f", turn.failed {failed}" if failed else "") + ("" if text else ", no last message")
        return False, f"codex turn {n} failed: {why}; log: {jsonl} (stderr {err})"
    return True, text


def _read_events(name, rec, p, jf, tid):
    """Copy codex's JSONL to the log, record the thread id; returns (thread_id, turn.failed error or None, rc)."""
    failed = None
    for line in p.stdout:  # type: ignore[union-attr]
        jf.write(line)
        jf.flush()
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # not JSON: ignore
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "thread.started" and not tid and ev.get("thread_id"):
            tid = ev["thread_id"]  # a resume's thread.started never overwrites the recorded id
            update_worker(name, thread_id=tid)
            deliver(inbox_dir(rec["owner"]), name, rec["owner"], f"worker {name} started, thread {tid}",
                    "message", tid)
        elif ev.get("type") == "turn.failed":
            failed = json.dumps(ev.get("error", ev))[:500]
    return tid, failed, p.wait()


def run_worker(name, _hook=None):
    """Drain the worker's queue turn by turn. Returns "locked" if another runner holds the worker, else "done"."""
    if read_worker(name) is None:
        return "done"  # retired (or never started): nothing to run
    os.makedirs(wdir(name), exist_ok=True)
    with open(wdir(name) + ".lock", "w") as lk:
        return _drain(name, lk, _hook)


def _drain(name, lk, _hook):
    wd = wdir(name)
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return "locked"
    if read_worker(name) is None:  # retired while we waited; update_worker below would recreate an ownerless record
        # an empty folder (or logs/ from spawn_runner) may come back after retire: harmless, codex_start reuses it
        return "done"
    queue, taken = os.path.join(wd, "queue"), os.path.join(wd, "taken")
    old = read_worker(name) or {}
    update_worker(name, pid=os.getpid())
    stale = take(taken, queue)  # left by a runner that died mid-turn; file names keep their order
    if stale or old.get("state") == "running":
        rec = update_worker(name, state="failed", interrupted=True)
        report(rec, False, f"the runner of {name} (pid {old.get('pid')}) died during turn {old.get('turns')}; "
                           f"{len(stale)} message(s) requeued for the next turn")
    while True:
        claimed = list(claim(queue, taken))
        msgs = [m for _, m in claimed]
        if not msgs:
            if _hook:
                _hook()
            rec = read_worker(name) or {}
            if rec.get("state") in BUSY:  # nothing to run, yet a runner was expected: say so, or it is revived forever
                report(update_worker(name, state="failed"), False, f"the runner of {name} found no message to run")
            # Exit race: a send may land between "queue empty" and our exit and its runner would bounce off the lock.
            fcntl.flock(lk, fcntl.LOCK_UN)
            if not count_files(queue):
                return "done"
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "done"  # the new runner has it
            continue
        rec = read_worker(name)
        if rec is None:
            return "done"  # retired by `retire` (force) while this runner started up
        try:
            ok, text = run_turn(name, rec, msgs)
        except Exception as e:  # any runner bug must still end the turn with an error, never a silent "running"
            traceback.print_exc()
            ok, text = False, f"runner error in a turn of {name}: {e!r}; log: {os.path.join(wd, 'logs', 'runner.log')}"
        first = text.split("\n", 1)[0]
        report(update_worker(name, state="idle" if ok else "failed", last_message=text if ok else first,
                             interrupted=False), ok, text)
        for path, _ in claimed:  # only after the result is out: a crash before this replays the turn, never drops it
            os.unlink(path)


def report(rec, ok, text):
    """Mail a turn result (or a runner failure) to the worker's owner."""
    deliver(inbox_dir(rec["owner"]), rec["name"], rec["owner"], text, "turn_end" if ok else "error",
            rec.get("thread_id"))


CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def show(e):
    re_ = f" (re #{e['reply_to']})" if e.get("reply_to") is not None else ""
    # no terminal escapes; continuation lines indented so a post can't fake a `#id ts from:` header
    text = CTRL_RE.sub("?", str(e.get("text", ""))).replace("\n", "\n    ")
    print(f"#{e['id']} {e['ts']} {e['from']}{re_}: {text}", flush=True)


def council_cli(args):
    """The user's own client: posts are from `user`, whatever AGENT_MAIL_NAME says."""
    import argparse
    ap = argparse.ArgumentParser(prog="agent_mail.py council")
    ap.add_argument("--channel", default="council")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post")
    p.add_argument("--reply-to", type=int)
    p.add_argument("text")
    sub.add_parser("read").add_argument("--since")
    sub.add_parser("tail")
    a = ap.parse_args(args)
    if os.environ.get("AGENT_MAIL_NAME"):  # set in every agent session and worker, never in the user's own shell
        print("agent-mail council: AGENT_MAIL_NAME is set, so this is an agent session; agents use the post tool",
              file=sys.stderr)
        return 2
    try:
        member(a.channel, USER)
        if a.cmd == "post":
            print(f"posted #{post(a.channel, USER, a.text, a.reply_to)['id']} to {a.channel}")
            return 0
        h = read_since(a.channel, a.since if a.cmd == "read" else None)
        for e in h[-20:] if a.cmd == "tail" else h:
            show(e)
        last = h[-1]["id"] if h else 0
        while a.cmd == "tail":
            time.sleep(1)
            for e in read_since(a.channel, last):
                show(e)
                last = e["id"]
    except MailError as e:
        print(f"agent-mail council: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


def send_cli(args):
    """`send <to> <text>` from a script (cron, hooks): the same delivery as the MCP send tool."""
    import argparse
    ap = argparse.ArgumentParser(prog="agent_mail.py send")
    ap.add_argument("--from", dest="frm", help="sender name (default: AGENT_MAIL_NAME)")
    ap.add_argument("to")
    ap.add_argument("text")
    a = ap.parse_args(args)
    try:
        frm = a.frm if a.frm is not None else os.environ.get("AGENT_MAIL_NAME") or ""
        if check_name(frm, "sender (--from or AGENT_MAIL_NAME)") == USER:  # only the council CLI speaks as the user
            raise MailError(f"the sender name {USER!r} is reserved for the user's own `council` CLI")
        root()
        os.environ["AGENT_MAIL_NAME"] = frm  # tool_send takes the sender from me()
        print(tool_send(a.to, a.text))
    except MailError as e:
        print(f"agent-mail send: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        Server().serve()
    elif cmd == "council":
        return council_cli(argv[2:])
    elif cmd == "send":
        return send_cli(argv[2:])
    elif cmd == "run" and len(argv) == 3:
        try:
            root()
            check_name(argv[2], "worker name")
        except MailError as e:
            print(f"agent-mail run: {e}", file=sys.stderr)
            return 2
        run_worker(argv[2])
    else:
        print("usage: agent_mail.py [serve | run <worker> | council post|read|tail | send [--from NAME] <to> <text>]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
