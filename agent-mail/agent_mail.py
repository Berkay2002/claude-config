#!/usr/bin/env python3.11
"""agent-mail: one stdio MCP server for messages between Claude and Codex sessions, plus the runner that drives
Codex workers. `agent_mail.py serve` (default) is the MCP server; `agent_mail.py run <worker>` is the detached runner.

Everything comes from env: AGENT_MAIL_DIR (absolute mail root, required), AGENT_MAIL_NAME (who this instance is),
AGENT_MAIL_CODEX (codex binary, default `codex` on PATH), CODEX_HOME (passed through to codex). Never defaults to ~.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # no dots: `x.rec` would collide with x's lock files
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")
SANDBOXES = ("full", "workspace-write", "read-only")  # loosest to tightest
LIVE = ("starting", "running", "idle")
INSTRUCTIONS = ("Message other agent sessions. send(to, text) messages a Codex worker (it runs a turn) or any named "
                "inbox; codex_start launches a Codex worker; list shows workers; inbox reads your unread mail when "
                "messages are not pushed to you. Worker results arrive as mail from the worker (kind turn_end/error).")


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


def deliver(box_dir, frm, to, text, kind="message", thread_id=None):
    """Write one message file into <box_dir> (an inbox's new/ or a worker's queue/); the tmp file is outside it."""
    msg = {"id": uuid.uuid4().hex, "from": frm or "unknown", "to": to, "text": text, "kind": kind, "ts": now_iso()}
    if thread_id:
        msg["thread_id"] = thread_id
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


# ---------- tools ----------

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
    if read_worker(check_name(to, "recipient")) is not None:
        deliver(os.path.join(wdir(to), "queue"), me(), to, text)
        spawn_runner(to)
        return f"queued for Codex worker {to}; its runner is started and the result will arrive as mail"
    deliver(inbox_dir(to), me(), to, text)
    return f"delivered to inbox {to}"


def own_sandbox():
    """The sandbox this instance itself runs in: set by the runner for workers, absent (= full) for orchestrators."""
    return os.environ.get("AGENT_MAIL_SANDBOX") or "full"


def tool_codex_start(name, cwd, prompt, model=None, effort=None, sandbox=None):
    check_name(name, "worker name")
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


def tool_list():
    recs = []
    wd = os.path.join(root(), "workers")
    for fn in sorted(os.listdir(wd)) if os.path.isdir(wd) else []:
        if fn.endswith(".json"):
            r = read_worker(fn[:-5]) or {}
            recs.append({k: r.get(k) for k in ("name", "state", "thread_id", "cwd", "owner", "sandbox")}
                        | {"last": (r.get("last_message") or "").split("\n", 1)[0]})
    unread = count_files(inbox_dir(me())) if me() else None
    return json.dumps({"workers": recs, "unread": unread})


def tool_inbox():
    if not me():
        raise MailError("AGENT_MAIL_NAME is not set: this instance has no inbox")
    return json.dumps(claim_inbox(me()))


TOOLS = {
    "send": (tool_send, "Send a message. `to` is a Codex worker (queued, starts a turn) or any inbox name.",
             {"to": {"type": "string"}, "text": {"type": "string"}}, ["to", "text"]),
    "codex_start": (tool_codex_start, "Start a Codex worker in `cwd` with `prompt`. sandbox: full (default, no "
                    "sandbox), read-only or workspace-write.",
                    {"name": {"type": "string"}, "cwd": {"type": "string"}, "prompt": {"type": "string"},
                     "model": {"type": "string"}, "effort": {"type": "string"},
                     "sandbox": {"type": "string", "enum": list(SANDBOXES)}}, ["name", "cwd", "prompt"]),
    "list": (tool_list, "List Codex workers and this inbox's unread count.", {}, []),
    "inbox": (tool_inbox, "Claim and return this instance's unread mail.", {}, []),
}


# ---------- MCP server ----------

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
        if msg.get("thread_id"):
            meta["thread_id"] = str(msg["thread_id"])
        self.write({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                    "params": {"content": msg.get("text", ""), "meta": meta}})

    def poll(self, interval):
        while not self.stop.is_set():
            try:
                for m in claim_inbox(me()):
                    self.push(m)
            except (MailError, OSError):
                pass
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
            if me() and self.client == "claude-code" and not self.poller:
                try:
                    root()
                except MailError:
                    return
                self.poller = threading.Thread(target=self.poll, args=(float(os.environ.get("AGENT_MAIL_POLL", 1)),),
                                               daemon=True)
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
    return "\n\n".join(f"From {m['from']}:\n{m['text']}" for m in msgs)


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
    failed = None
    with open(jsonl, "w") as jf, open(err, "w") as ef:
        try:
            p = subprocess.Popen(argv, cwd=rec["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=ef,
                                 text=True)
        except OSError as e:
            return False, f"could not start codex ({argv[0]}): {e}; log: {err}"
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
        rc = p.wait()
        p.stdout.close()  # type: ignore[union-attr]
    text = ""
    if os.path.exists(last):
        with open(last) as f:
            text = f.read().strip()
    if rc != 0 or failed or not text:
        why = f"exit code {rc}" + (f", turn.failed {failed}" if failed else "") + ("" if text else ", no last message")
        return False, f"codex turn {n} failed: {why}; log: {jsonl} (stderr {err})"
    return True, text


def run_worker(name, _hook=None):
    """Drain the worker's queue turn by turn. Returns "locked" if another runner holds the worker, else "done"."""
    os.makedirs(wdir(name), exist_ok=True)
    with open(wdir(name) + ".lock", "w") as lk:
        return _drain(name, lk, _hook)


def _drain(name, lk, _hook):
    wd = wdir(name)
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return "locked"
    queue, taken = os.path.join(wd, "queue"), os.path.join(wd, "taken")
    update_worker(name, pid=os.getpid())
    while True:
        msgs = take(queue, taken)
        if not msgs:
            if _hook:
                _hook()
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
        ok, text = run_turn(name, rec, msgs)
        first = text.split("\n", 1)[0]
        update_worker(name, state="idle" if ok else "failed", last_message=text if ok else first)
        deliver(inbox_dir(rec["owner"]), name, rec["owner"], text, "turn_end" if ok else "error",
                (read_worker(name) or {}).get("thread_id"))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        Server().serve()
    elif cmd == "run" and len(argv) == 3:
        try:
            root()
            check_name(argv[2], "worker name")
        except MailError as e:
            print(f"agent-mail run: {e}", file=sys.stderr)
            return 2
        run_worker(argv[2])
    else:
        print("usage: agent_mail.py [serve | run <worker>]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
