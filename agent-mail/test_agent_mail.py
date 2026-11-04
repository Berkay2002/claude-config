#!/usr/bin/env python3.11
"""agent_mail.py with a fake codex and temp dirs. Run: /usr/bin/python3.11 -B -m unittest -v test_agent_mail

HOME, the mail dir, the codex binary and the MCP clients are all fakes in a temp dir: nothing reaches the real
codex, mail dir, CODEX_HOME or any MCP config.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent_mail as am  # noqa: E402

AM = os.path.join(HERE, "agent_mail.py")
FAKE = """#!/usr/bin/env python3.11
import json, os, sys, time
d = os.environ["FAKE_DIR"]
a = sys.argv[1:]
resume = a[1:2] == ["resume"]
last, prompt = a[a.index("-o") + 1], a[-1]
tid = a[-2] if resume else None
with open(d + "/calls.jsonl", "a") as f:
    f.write(json.dumps({"argv": a, "cwd": os.getcwd(), "stdin": os.readlink("/proc/self/fd/0"),
                        "name": os.environ.get("AGENT_MAIL_NAME"), "pid": os.getpid(),
                        "owner_only": os.environ.get("OWNER_ONLY")}) + "\\n")
def p(o): print(json.dumps(o), flush=True)
if os.path.exists(d + "/otherid") and resume:
    p({"type": "thread.started", "thread_id": "resume-other"})
else:
    p({"type": "thread.started", "thread_id": tid or "thread-1"})
p({"type": "item.completed", "item": {"id": "i0", "type": "error", "message": "deprecated"}})
if os.path.exists(d + "/sleep") and not resume:
    time.sleep(float(open(d + "/sleep").read()))
if os.path.exists(d + "/fail"):
    sys.stderr.write("boom\\n"); sys.exit(3)
if not os.path.exists(d + "/nolast"):
    open(last, "w").write("done: " + prompt)
p({"type": "turn.completed"})
"""


RELAY = "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:3] + ['serve']))"


def gone(pid):
    """The process has exited (no /proc entry, or a zombie with an empty cmdline)."""
    try:
        with open(f"/proc/{pid}/cmdline") as f:
            return not f.read()
    except OSError:
        return True


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


class Client:
    """A fake MCP client on a real stdio pipe to `agent_mail.py serve`."""

    def __init__(self, env, cwd, channel):
        # channel: the server's parent carries Claude's dev-channels flag, as `claude-orch` does
        argv = [sys.executable, "-c", RELAY, sys.executable, AM, "--dangerously-load-development-channels",
                "server:agent-mail"] if channel else [sys.executable, AM, "serve"]
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env, cwd=cwd)
        self.resp, self.notes, self.n = queue.Queue(), queue.Queue(), 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            m = json.loads(line)
            (self.resp if "id" in m else self.notes).put(m)

    def req(self, method, params=None, rid=None):
        self.n += 1
        rid = self.n if rid is None else rid
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        r = self.resp.get(timeout=10)
        assert r["id"] == rid, r
        return r

    def notify(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def init(self, client="claude-code"):
        r = self.req("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                    "clientInfo": {"name": client, "version": "1"}})
        self.notify("notifications/initialized")
        return r["result"]

    def tool(self, tool_name, **args):
        r = self.req("tools/call", {"name": tool_name, "arguments": args})["result"]
        return r.get("isError", False), r["content"][0]["text"]

    def note(self, timeout=5):
        return self.notes.get(timeout=timeout)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)
        self.p.stdout.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-mail-test-")
        self.mail = os.path.join(self.tmp, "mail")
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        fake = os.path.join(self.tmp, "codex")
        with open(fake, "w") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        self.env0 = dict(os.environ)
        for k in [k for k in os.environ if k.startswith("AGENT_MAIL")]:
            del os.environ[k]
        os.environ.update(HOME=self.tmp, AGENT_MAIL_DIR=self.mail, AGENT_MAIL_NAME="orch", AGENT_MAIL_CODEX=fake,
                          AGENT_MAIL_POLL="0.1", FAKE_DIR=self.tmp, CODEX_HOME=os.path.join(self.tmp, "codexhome"))
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            try:
                c.close()
            except Exception:
                c.p.kill()
        wd = os.path.join(self.mail, "workers")
        for fn in os.listdir(wd) if os.path.isdir(wd) else []:
            if fn.endswith(".json") and not fn.endswith(".rec.lock"):
                pid = (am.read_worker(fn[:-5]) or {}).get("pid")
                if pid and pid != os.getpid():  # in-process run_worker records the test's own pid
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
        os.environ.clear()
        os.environ.update(self.env0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, name="orch", mail=True, channel=True, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("AGENT_MAIL_DIR", "AGENT_MAIL_NAME")}
        env.update(extra)
        if mail:
            env["AGENT_MAIL_DIR"] = self.mail
        if name:
            env["AGENT_MAIL_NAME"] = name
        c = Client(env, self.cwd, channel)
        self.clients.append(c)
        return c

    def calls(self):
        p = os.path.join(self.tmp, "calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f]

    def flag(self, name, content="1"):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(content)

    def box(self, name="orch"):
        d = os.path.join(self.mail, "inbox", name)
        out = []
        for sub in ("new", "cur"):
            p = os.path.join(d, sub)
            for fn in sorted(os.listdir(p)) if os.path.isdir(p) else []:
                with open(os.path.join(p, fn)) as f:
                    out.append(json.load(f))
        return out

    def start_sync(self, name="w1", prompt="hello", **kw):
        """codex_start without a real runner; the test calls run_worker itself."""
        with mock.patch.object(am, "spawn_runner") as sp:
            am.tool_codex_start(name, self.cwd, prompt, **kw)
        return sp


class ServerTests(Base):
    def test_handshake_without_name(self):
        c = self.client(name=None)
        r = c.req("server/discover", {}, rid="server-discover-probe-1")  # Claude probes before initialize
        self.assertEqual(r["error"]["code"], -32601)
        r = c.req("initialize", {"protocolVersion": "2099-01-01", "clientInfo": {"name": "claude-code"}})["result"]
        self.assertEqual(r["protocolVersion"], "2099-01-01")
        self.assertEqual(r["capabilities"], {"tools": {}})
        self.assertTrue(r["instructions"])
        c.notify("notifications/initialized")
        c.notify("notifications/whatever")  # unknown notification: ignored, no reply
        self.assertEqual(c.req("ping")["result"], {})
        names = {t["name"] for t in c.req("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"send", "codex_start", "retire", "list", "inbox"})
        self.assertEqual(c.req("nope")["error"]["code"], -32601)
        err, text = c.tool("send", to="x", text="hi")
        self.assertFalse(err)
        self.assertEqual(self.box("x")[0]["from"], "unknown")
        err, text = c.tool("inbox")
        self.assertTrue(err)  # no name: no inbox
        c.close()

    def test_channel_capability_only_with_name(self):
        r = self.client(name="orch").init()
        self.assertEqual(r["capabilities"]["experimental"], {"claude/channel": {}})

    def test_missing_dir_gives_clean_errors(self):
        c = self.client(mail=False)
        c.init()
        for tool, args in (("send", {"to": "x", "text": "t"}), ("list", {}), ("inbox", {}),
                           ("codex_start", {"name": "w", "cwd": self.cwd, "prompt": "p"})):
            err, text = c.tool(tool, **args)
            self.assertTrue(err, tool)
            self.assertIn("AGENT_MAIL_DIR", text)
        c.close()
        self.assertEqual(os.listdir(self.cwd), [])
        self.assertFalse(os.path.exists(self.mail))

    def test_push_including_mail_sent_before_start(self):
        am.deliver(am.inbox_dir("orch"), "w1", "orch", "while down", "turn_end", "t-9")
        c = self.client()
        c.init()
        n = c.note()
        self.assertEqual(n["method"], "notifications/claude/channel")
        self.assertEqual(n["params"]["content"], "while down")
        self.assertEqual(n["params"]["meta"], {"from": "w1", "kind": "turn_end", "thread_id": "t-9"})
        am.deliver(am.inbox_dir("orch"), "bob", "orch", "live")
        n = c.note()
        self.assertEqual((n["params"]["content"], n["params"]["meta"]), ("live", {"from": "bob", "kind": "message"}))
        self.assertEqual(os.listdir(am.inbox_dir("orch")), [])
        self.assertEqual(len(os.listdir(am.inbox_dir("orch", "cur"))), 2)

    def test_two_readers_deliver_each_message_once(self):
        for i in range(12):
            am.deliver(am.inbox_dir("orch"), "bob", "orch", f"m{i}")
        cs = [self.client() for _ in range(2)]
        for c in cs:
            c.init()
        time.sleep(1.5)
        got = []
        for c in cs:
            while not c.notes.empty():
                got.append(c.notes.get()["params"]["content"])
        self.assertEqual(sorted(got), sorted(f"m{i}" for i in range(12)))

    def test_push_to_a_closed_client_puts_the_mail_back(self):
        """A push that hits a broken pipe must not leave its message (or the rest of the batch) in cur/."""
        class Broken:
            def write(self, _):
                raise BrokenPipeError

            def flush(self):
                pass
        for i in range(3):
            am.deliver(am.inbox_dir("orch"), "bob", "orch", f"m{i}")
        am.Server(stdin=[], stdout=Broken()).poll(0.01)  # returns once the client is gone
        self.assertEqual(am.count_files(am.inbox_dir("orch")), 3)
        self.assertEqual(am.count_files(am.inbox_dir("orch", "cur")), 0)

    def test_poller_survives_an_unexpected_error(self):
        """One failing round (here a broken worker record) must not stop push for the rest of the session."""
        srv = am.Server(stdin=[], stdout=mock.Mock())
        calls = []

        def revive(owner):
            calls.append(owner)
            if len(calls) == 1:
                raise KeyError("name")
            srv.stop.set()
        with mock.patch.object(am, "revive_dead", side_effect=revive), mock.patch("traceback.print_exc"):
            srv.poll(0.01)
        self.assertEqual(calls, ["orch", "orch"])

    def test_claim_race_between_threads(self):
        for i in range(60):
            am.deliver(am.inbox_dir("orch"), "bob", "orch", f"m{i}")
        got, lock = [], threading.Lock()

        def reader():
            for m in am.claim_inbox("orch"):
                with lock:
                    got.append(m["text"])
        ts = [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(got), sorted(f"m{i}" for i in range(60)))

    def test_non_claude_client_gets_no_push_but_inbox_tool(self):
        am.deliver(am.inbox_dir("orch"), "w1", "orch", "for codex")
        c = self.client()
        c.init(client="codex-mcp-client")
        time.sleep(0.5)
        self.assertTrue(c.notes.empty())
        self.assertEqual(len(os.listdir(am.inbox_dir("orch"))), 1)
        err, text = c.tool("inbox")
        self.assertEqual([m["text"] for m in json.loads(text)], ["for codex"])
        self.assertEqual(json.loads(c.tool("inbox")[1]), [])
        self.assertEqual(json.loads(c.tool("list")[1])["unread"], 0)

    def test_only_the_owner_starts_runners_so_they_get_its_env(self):
        """A non-owner's send only queues; the owner's poll thread starts the runner, with the owner's environment."""
        owner = self.client(channel=False, OWNER_ONLY="1")
        owner.init(client="codex-mcp-client")
        self.assertFalse(owner.tool("codex_start", name="w1", cwd=self.cwd, prompt="first")[0])
        wait_for(lambda: [m for m in self.box() if m["kind"] == "turn_end"])
        bob = self.client(name="bob", channel=False)
        bob.init(client="codex-mcp-client")
        with mock.patch.object(am, "spawn_runner") as sp:  # in-process sends by a non-owner: no spawn either
            os.environ["AGENT_MAIL_NAME"] = "bob"
            self.assertIn("its owner orch starts", am.tool_send("w1", "x"))
            json.loads(am.tool_list()), json.loads(am.tool_inbox())
        sp.assert_not_called()
        wait_for(lambda: len([m for m in self.box() if m["kind"] == "turn_end"]) == 2)  # the owner ran it
        self.assertIn("From bob:\nx", self.calls()[1]["argv"][-1])
        self.assertEqual(self.calls()[1]["owner_only"], "1")  # the runner came from the owner's server

    def test_claude_without_the_channel_flag_gets_no_push(self):
        """A Claude that would drop the push (no dev-channels flag, e.g. --bg) must not claim the mail."""
        am.deliver(am.inbox_dir("orch"), "w1", "orch", "keep me")
        c = self.client(channel=False)
        c.init()
        time.sleep(0.5)
        self.assertTrue(c.notes.empty())
        self.assertEqual([m["text"] for m in json.loads(c.tool("inbox")[1])], ["keep me"])

    def test_codex_start_and_send_over_stdio_with_real_runner(self):
        c = self.client()
        c.init(client="codex-mcp-client")
        err, text = c.tool("codex_start", name="w1", cwd=self.cwd, prompt="first")
        self.assertFalse(err, text)
        wait_for(lambda: [m for m in self.box() if m["kind"] == "turn_end"])
        err, text = c.tool("send", to="w1", text="second")
        self.assertFalse(err, text)
        wait_for(lambda: len([m for m in self.box() if m["kind"] == "turn_end"]) == 2)
        wk = json.loads(c.tool("list")[1])["workers"][0]
        self.assertEqual((wk["name"], wk["state"], wk["thread_id"], wk["last"]), ("w1", "idle", "thread-1", "done: From orch:"))
        pid = am.read_worker("w1")["pid"]
        wait_for(lambda: not os.path.exists(f"/proc/{pid}"))  # runner exits when the queue is empty


class RunnerTests(Base):
    def test_send_to_worker_queues_and_starts_runner(self):
        sp = self.start_sync()
        self.assertEqual(sp.call_count, 1)
        rec = am.read_worker("w1")
        self.assertEqual((rec["state"], rec["owner"], rec["sandbox"]), ("starting", "orch", "full"))
        with mock.patch.object(am, "spawn_runner") as sp2:
            self.assertIn("queued", am.tool_send("w1", "more"))
        sp2.assert_called_once_with("w1")
        self.assertEqual(am.count_files(os.path.join(am.wdir("w1"), "queue")), 2)
        self.assertEqual(self.box("w1"), [])  # a worker's mail goes to its queue, not an inbox

    def test_first_turn_records_thread_and_notifies_owner(self):
        self.start_sync(prompt="do it", model="gpt-x", effort="low")
        self.assertEqual(am.run_worker("w1"), "done")
        rec = am.read_worker("w1")
        self.assertEqual((rec["thread_id"], rec["state"], rec["last_message"]), ("thread-1", "idle", "done: do it"))
        box = self.box()
        self.assertEqual([m["kind"] for m in box], ["message", "turn_end"])
        self.assertIn("thread-1", box[0]["text"])
        self.assertEqual((box[1]["from"], box[1]["text"], box[1]["thread_id"]), ("w1", "done: do it", "thread-1"))
        (call,) = self.calls()
        a = call["argv"]
        self.assertEqual(a[:2], ["exec", "--json"])
        self.assertEqual(call["cwd"], os.path.realpath(self.cwd))
        self.assertEqual(call["stdin"], "/dev/null")
        self.assertEqual(a[a.index("-C") + 1], self.cwd)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", a)
        self.assertEqual(a[a.index("-m") + 1], "gpt-x")
        self.assertEqual(a[-1], "do it")
        cfg = [a[i + 1] for i, x in enumerate(a) if x == "-c"]
        self.assertIn('mcp_servers.agent-mail.env.AGENT_MAIL_NAME="w1"', cfg)
        self.assertIn(f'mcp_servers.agent-mail.env.AGENT_MAIL_DIR="{self.mail}"', cfg)
        self.assertIn(f'mcp_servers.agent-mail.env.CODEX_HOME="{os.environ["CODEX_HOME"]}"', cfg)
        self.assertIn('model_reasoning_effort="low"', cfg)
        self.assertEqual(json.loads(cfg[cfg.index(next(c for c in cfg if "agent-mail.args" in c))].split("=", 1)[1]),
                         [AM, "serve"])

    def test_messages_queued_during_a_turn_go_into_the_next_turn(self):
        self.start_sync(prompt="p1")
        self.flag("sleep", "1.5")
        t = threading.Thread(target=am.run_worker, args=("w1",))
        t.start()
        wait_for(lambda: self.calls())
        os.environ["AGENT_MAIL_NAME"] = "bob"
        with mock.patch.object(am, "spawn_runner"):
            am.tool_send("w1", "m1")
            am.tool_send("w1", "m2")
        t.join(20)
        first, second = self.calls()
        self.assertNotIn("resume", first["argv"])
        a = second["argv"]
        self.assertEqual(a[:3], ["exec", "resume", "--json"])
        self.assertEqual(a[-2], "thread-1")
        self.assertEqual(a[-1], "From bob:\nm1\n\nFrom bob:\nm2")
        self.assertNotIn("-C", a)
        self.assertEqual(len([m for m in self.box() if m["kind"] == "turn_end"]), 2)

    def test_resume_never_overwrites_thread_id(self):
        self.start_sync()
        am.run_worker("w1")
        self.flag("otherid")
        with mock.patch.object(am, "spawn_runner"):
            am.tool_send("w1", "again")
        am.run_worker("w1")
        self.assertEqual(am.read_worker("w1")["thread_id"], "thread-1")

    def test_failing_codex_gives_error_event(self):
        self.start_sync()
        self.flag("fail")
        am.run_worker("w1")
        (err,) = [m for m in self.box() if m["kind"] == "error"]
        self.assertIn("exit code 3", err["text"])
        log = err["text"].split("log: ")[1].split(" ")[0]
        self.assertTrue(os.path.exists(log))
        self.assertEqual(am.read_worker("w1")["state"], "failed")
        self.assertFalse([m for m in self.box() if m["kind"] == "turn_end"])

    def test_exit_zero_without_last_message_is_an_error(self):
        self.start_sync()
        self.flag("nolast")
        am.run_worker("w1")
        (err,) = [m for m in self.box() if m["kind"] == "error"]
        self.assertIn("no last message", err["text"])

    def test_missing_codex_binary_is_an_error_event(self):
        self.start_sync()
        os.environ["AGENT_MAIL_CODEX"] = os.path.join(self.tmp, "nope")
        am.run_worker("w1")
        (err,) = [m for m in self.box() if m["kind"] == "error"]
        self.assertIn("could not start codex", err["text"])

    def test_second_runner_exits_while_one_holds_the_lock(self):
        self.start_sync()
        import fcntl
        os.makedirs(am.wdir("w1"), exist_ok=True)
        with open(am.wdir("w1") + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            self.assertEqual(am.run_worker("w1"), "locked")
            r = subprocess.run([sys.executable, AM, "run", "w1"], timeout=10)
            self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(am.count_files(os.path.join(am.wdir("w1"), "queue")), 1)  # still queued

    def test_send_landing_while_runner_exits_is_not_lost(self):
        self.start_sync()
        fired = []

        def late_send():  # runs after the runner saw an empty queue, while it still holds the lock
            if fired:
                return
            fired.append(1)
            with mock.patch.object(am, "spawn_runner", side_effect=lambda n: fired.append(am.run_worker(n))):
                am.tool_send("w1", "late")
        am.run_worker("w1", _hook=late_send)
        self.assertEqual(fired, [1, "locked"])  # the new runner bounced off the lock...
        self.assertEqual(len(self.calls()), 2)  # ...and the old one picked the message up
        self.assertEqual(self.calls()[1]["argv"][-1], "From orch:\nlate")

    def test_restricted_sandbox_args(self):
        self.start_sync(sandbox="read-only")
        rec = am.read_worker("w1")
        first = am.codex_args(rec, None, "/l", "p")
        resume = am.codex_args(rec, "tid", "/l", "p")
        self.assertEqual(first[first.index("-s") + 1], "read-only")
        self.assertIn("sandbox_mode=\"read-only\"", resume)  # exec resume rejects -s
        for a in (first, resume):
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", a)
            self.assertIn('mcp_servers.agent-mail.default_tools_approval_mode="approve"', a)
        self.assertNotIn("-s", resume)

    def test_restricted_worker_cannot_start_a_looser_worker(self):
        os.environ["AGENT_MAIL_SANDBOX"] = "read-only"
        for sb in ("full", "workspace-write"):
            with self.assertRaises(am.MailError, msg=sb):
                self.start_sync("w2", sandbox=sb)
        self.start_sync("w3")  # default: inherits the caller's level
        self.assertEqual(am.read_worker("w3")["sandbox"], "read-only")
        cfg = am.codex_args(am.read_worker("w3"), None, "/l", "p")
        self.assertIn('mcp_servers.agent-mail.env.AGENT_MAIL_SANDBOX="read-only"', cfg)

    def test_prompts_that_look_like_flags_or_subcommands_stay_prompts(self):
        """`codex exec "-x"` prints help and `codex exec review` runs a review: positionals go after `--`."""
        rec = {"name": "w1", "cwd": self.cwd, "owner": "orch"}
        for tid in (None, "tid"):
            a = am.codex_args(rec, tid, "/l", "--dangerously-bypass-approvals-and-sandbox")
            self.assertEqual(a[a.index("--"):], ["--"] + ([tid] if tid else []) + [a[-1]])
            self.assertEqual(a.count("--dangerously-bypass-approvals-and-sandbox"), 2)  # the flag itself + the prompt

    def test_turn_timeout_kills_codex_and_reports_an_error(self):
        """A hung turn must not keep the worker `running`: it is killed and reported."""
        self.start_sync()
        self.flag("sleep", "30")
        with mock.patch.object(am, "TURN_TIMEOUT", 0.5):
            t0 = time.time()
            am.run_worker("w1")
        self.assertLess(time.time() - t0, 10)
        (err,) = [m for m in self.box() if m["kind"] == "error"]
        self.assertIn("timed out", err["text"])
        self.assertEqual(am.read_worker("w1")["state"], "failed")
        self.assertTrue(gone(self.calls()[0]["pid"]))

    def test_runner_killed_mid_turn_is_revived_and_replays_the_turn(self):
        """SIGKILL the runner mid-turn: codex dies with it, the owner hears of it and the message is not lost."""
        self.flag("sleep", "30")
        with mock.patch.object(am, "spawn_runner"):
            am.tool_codex_start("w1", self.cwd, "first")
        os.environ["AGENT_MAIL_NAME"] = "bob"
        with mock.patch.object(am, "spawn_runner"):
            am.tool_send("w1", "second")  # queued before the runner starts: both go into turn 1
        os.environ["AGENT_MAIL_NAME"] = "orch"
        runner = subprocess.Popen([sys.executable, AM, "run", "w1"], start_new_session=True)
        wait_for(lambda: self.calls())
        codex_pid = self.calls()[0]["pid"]
        runner.kill()
        runner.wait()
        wait_for(lambda: gone(codex_pid))  # PR_SET_PDEATHSIG: no orphan codex keeps working on the thread
        self.assertEqual(am.read_worker("w1")["state"], "running")
        os.unlink(os.path.join(self.tmp, "sleep"))
        json.loads(am.tool_list())  # list notices the dead runner and starts a new one
        wait_for(lambda: [m for m in self.box() if m["kind"] == "turn_end"])
        kinds = [m["kind"] for m in self.box()]
        self.assertEqual(kinds, ["message", "error", "turn_end"])
        self.assertIn("2 message(s) requeued", self.box()[1]["text"])
        replay = self.calls()[1]["argv"]
        self.assertEqual(replay[-2], "thread-1")
        self.assertIn("cut off", replay[-1])
        self.assertIn("From orch:\nfirst\n\nFrom bob:\nsecond", replay[-1])
        wait_for(lambda: am.read_worker("w1")["state"] == "idle")
        self.assertEqual(os.listdir(os.path.join(am.wdir("w1"), "taken")), [])

    def test_revive_skips_live_and_just_started_workers(self):
        """A live runner or a record still inside its start grace is left alone."""
        self.start_sync()
        with mock.patch.object(am, "spawn_runner") as sp:
            am.revive_dead("orch")  # starting, pid None, fresh: grace
            am.update_worker("w1", state="running", pid=os.getpid())
            am.revive_dead("orch")  # the pid is alive but is not `agent_mail.py run w1`: pid reuse, so it is dead
            self.assertEqual(sp.call_count, 1)
            am.revive_dead("bob")  # someone else's worker: not ours to revive
            os.environ["AGENT_MAIL_NAME"] = "bob"
            am.tool_list()
            self.assertEqual(sp.call_count, 1)
            os.environ["AGENT_MAIL_NAME"] = "orch"
            am.tool_inbox()  # owners without push poll with inbox: it revives too
            self.assertEqual(sp.call_count, 2)
            am.update_worker("w1", state="idle", pid=None)
            am.revive_dead("orch")
            self.assertEqual(sp.call_count, 2)

    def test_a_runner_bug_ends_the_turn_with_an_error(self):
        """Any exception inside a turn becomes an error event and state failed, not a silent `running`."""
        self.start_sync()
        with mock.patch.object(am, "codex_args", side_effect=RuntimeError("bug")), mock.patch("traceback.print_exc"):
            am.run_worker("w1")
        (err,) = [m for m in self.box() if m["kind"] == "error"]
        self.assertIn("bug", err["text"])
        self.assertEqual(am.read_worker("w1")["state"], "failed")

    def test_codex_runs_as_the_worker_and_taken_is_emptied(self):
        """Codex's own commands see the worker's name, not the sender's; a finished turn leaves nothing to replay."""
        self.start_sync()
        am.run_worker("w1")
        self.assertEqual(self.calls()[0]["name"], "w1")
        self.assertEqual(os.listdir(os.path.join(am.wdir("w1"), "taken")), [])

    def test_validation(self):
        for bad in ("../x", "a/b", "", ".hidden", "x" * 80):
            with self.assertRaises(am.MailError, msg=bad):
                am.tool_send(bad, "t")
        with self.assertRaises(am.MailError):
            am.tool_codex_start("w1", "relative/dir", "p")
        with self.assertRaises(am.MailError):
            am.tool_codex_start("w1", self.cwd, "p", sandbox="yolo")
        with self.assertRaises(am.MailError):
            am.tool_codex_start("w1", self.cwd, "p", model="--evil")
        self.start_sync()
        with self.assertRaises(am.MailError):
            self.start_sync()  # live name refused
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))

    def test_dotted_names_and_bad_own_name_are_refused(self):
        """Dots would let `x.rec` share x's lock files; a bad AGENT_MAIL_NAME must fail loudly, not silently."""
        for bad in ("x.rec", "a.json"):
            with self.assertRaises(am.MailError, msg=bad):
                self.start_sync(bad)
        os.environ["AGENT_MAIL_NAME"] = "../orch"
        with self.assertRaisesRegex(am.MailError, "AGENT_MAIL_NAME"):
            am.tool_send("y", "t")
        with self.assertRaisesRegex(am.MailError, "AGENT_MAIL_NAME"):
            am.tool_inbox()

    def test_concurrent_starts_of_one_name_start_it_once(self):
        """The live-name check runs under the record lock, so a start racing another server's start is refused."""
        import fcntl
        os.makedirs(os.path.join(self.mail, "workers"), exist_ok=True)
        out = []
        with open(am.wdir("w1") + ".rec.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)  # the other server is mid-start
            t = threading.Thread(target=lambda: out.append(self._try_start()))
            t.start()
            time.sleep(0.3)
            am.put_json(am.wrec("w1"), {"name": "w1", "state": "starting", "owner": "other"})
        t.join(10)
        self.assertEqual(out, ["refused"])
        self.assertEqual(am.read_worker("w1")["owner"], "other")
        self.assertEqual(am.count_files(os.path.join(am.wdir("w1"), "queue")), 0)

    def _try_start(self):
        try:
            self.start_sync()
            return "ok"
        except am.MailError:
            return "refused"

    def test_failed_worker_can_be_restarted_without_replaying_old_mail(self):
        self.start_sync(prompt="old")
        self.flag("fail")
        am.run_worker("w1")
        os.unlink(os.path.join(self.tmp, "fail"))
        self.start_sync(prompt="new")
        am.run_worker("w1")
        self.assertEqual(self.calls()[-1]["argv"][-1], "new")

    def idle_worker(self, name="w1"):
        self.start_sync(name)
        am.run_worker(name)
        self.assertEqual(am.read_worker(name)["state"], "idle")

    def worker_files(self):
        return sorted(os.listdir(os.path.join(self.mail, "workers")))

    def test_retire_idle_worker_removes_everything_and_the_name_is_free_again(self):
        """retire deletes record and folder (lock files stay); list forgets it; the thread id is in the answer."""
        self.idle_worker()
        self.assertEqual(self.worker_files(), ["w1", "w1.json", "w1.lock", "w1.rec.lock"])
        out = am.tool_retire("w1")
        self.assertIn("thread-1", out)
        self.assertIn("codex delete --force thread-1", out)
        self.assertEqual(self.worker_files(), ["w1.lock", "w1.rec.lock"])
        self.assertEqual(json.loads(am.tool_list())["workers"], [])
        self.start_sync()  # same name again
        self.assertEqual(am.read_worker("w1")["state"], "starting")

    def test_retire_refuses_a_non_owner(self):
        """Only the worker's owner may retire it; nothing is touched."""
        self.idle_worker()
        os.environ["AGENT_MAIL_NAME"] = "intruder"
        with self.assertRaisesRegex(am.MailError, "belongs to orch"):
            am.tool_retire("w1", force=True)
        self.assertEqual(am.read_worker("w1")["state"], "idle")

    def test_retire_refuses_while_the_runner_lock_is_held(self):
        """A held runner lock means a turn may be running: refuse, even with force."""
        import fcntl
        self.idle_worker()
        with open(am.wdir("w1") + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            for force in (False, True):
                with self.assertRaisesRegex(am.MailError, "turn running, retry after its turn_end"):
                    am.tool_retire("w1", force=force)
        self.assertIsNotNone(am.read_worker("w1"))
        am.tool_retire("w1")  # lock free again

    def test_retire_refuses_queued_mail_unless_forced(self):
        """Undelivered mail (queue/ or taken/) would be lost: refused with its count, removed with force."""
        self.idle_worker()
        am.deliver(os.path.join(am.wdir("w1"), "queue"), "orch", "w1", "a")
        am.deliver(os.path.join(am.wdir("w1"), "taken"), "orch", "w1", "b")
        with self.assertRaisesRegex(am.MailError, r"2 undelivered message\(s\)"):
            am.tool_retire("w1")
        self.assertIsNotNone(am.read_worker("w1"))
        self.assertIn("discarded 2", am.tool_retire("w1", force=True))
        self.assertEqual(self.worker_files(), ["w1.lock", "w1.rec.lock"])

    def test_retire_unknown_and_invalid_names(self):
        """Unknown worker: a clear error and nothing created; dotted or path-like names never reach the filesystem."""
        with self.assertRaisesRegex(am.MailError, "unknown worker nope"):
            am.tool_retire("nope")
        for bad in ("x.rec", "a.json", "../x", "", ".hidden"):
            with self.assertRaises(am.MailError, msg=bad):
                am.tool_retire(bad)
        self.assertFalse(os.path.exists(os.path.join(self.mail, "workers")))

    def test_send_racing_retire_is_not_lost(self):
        """A retire between send's first look and its queue write: the mail lands in the inbox, no phantom worker."""
        self.idle_worker()
        orig, raced = am.read_worker, []

        def read_then_retire(name):
            rec = orig(name)
            if not raced:
                raced.append(1)
                am.tool_retire(name)
            return rec
        with mock.patch.object(am, "read_worker", side_effect=read_then_retire):
            out = am.tool_send("w1", "late")
        self.assertIn("delivered to inbox w1", out)
        self.assertEqual(self.worker_files(), ["w1.lock", "w1.rec.lock"])
        self.assertEqual([m["text"] for m in self.box("w1")], ["late"])

    def test_a_runner_opened_before_retire_still_excludes_the_next_runner(self):
        """Retire keeps the lock files, so a handle opened before it and one opened after share one lock."""
        import fcntl
        self.idle_worker()
        with open(am.wdir("w1") + ".lock", "w") as old:
            am.tool_retire("w1", force=True)
            self.start_sync()
            with open(am.wdir("w1") + ".lock", "w") as new:
                fcntl.flock(new, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_runner_of_a_retired_worker_recreates_nothing(self):
        """A runner that starts after retire exits without writing a record or files; one that waited on the lock too."""
        self.assertEqual(am.run_worker("ghost"), "done")
        self.assertFalse(os.path.exists(os.path.join(self.mail, "workers")))
        self.idle_worker()
        with open(am.wdir("w1") + ".lock", "w") as lk:
            os.unlink(am.wrec("w1"))  # retired while this runner waited
            self.assertEqual(am._drain("w1", lk, None), "done")
        self.assertIsNone(am.read_worker("w1"))

    def test_retire_over_stdio(self):
        """The tool is registered with a schema and answers errors as isError results."""
        c = self.client()
        c.init()
        tools = {t["name"]: t for t in c.req("tools/list")["result"]["tools"]}
        self.assertEqual(tools["retire"]["inputSchema"]["required"], ["name"])
        err, text = c.tool("retire", name="nope")
        self.assertTrue(err)
        self.assertIn("unknown worker", text)

    def test_run_cli_without_dir_fails_cleanly(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_MAIL")}
        r = subprocess.run([sys.executable, AM, "run", "w1"], env=env, cwd=self.cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("AGENT_MAIL_DIR", r.stderr)
        self.assertEqual(os.listdir(self.cwd), [])


if __name__ == "__main__":
    unittest.main()
