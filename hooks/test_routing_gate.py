"""Tests for routing_gate.py. Run: python3.11 -m unittest hooks/test_routing_gate.py"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routing_gate as rg  # noqa: E402

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)  # a Thursday
RESET = NOW + timedelta(days=4, hours=-6)  # Mon 06:00Z: the week started Mon 11-02 06:00Z
FM = {"scout": ("haiku", "medium"), "scout-lead": ("sonnet", "medium"), "builder": ("sonnet", "high"),
      "reviewer": ("opus", "medium")}


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.mail = os.path.join(self.home, "mail")
        for d in (".claude/shared/local", ".claude/usage", ".claude/agents", "mail"):
            os.makedirs(os.path.join(self.home, d))
        for name, (m, e) in FM.items():
            with open(os.path.join(self.home, ".claude/agents", f"{name}.md"), "w") as f:
                f.write(f"---\nname: {name}\ndescription: x\nmodel: {m}\neffort: {e}\n---\nbody\n")
        env = mock.patch.dict(os.environ, {"HOME": self.home, "AGENT_MAIL_DIR": self.mail, "TZ": "UTC"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GATE_TEST_OFF", None)
        self.addCleanup(self.tmp.cleanup)
        time.tzset()
        self.addCleanup(time.tzset)

    # helpers
    def routing(self, data):
        with open(os.path.join(self.home, ".claude/shared/local/routing.json"), "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def limits(self, pct, updated=NOW, reset=RESET):
        z = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
        with open(os.path.join(self.home, ".claude/usage/limits.json"), "w") as f:
            json.dump({"fiveHour": None, "week": {"percentUsed": pct, "resetsAt": z(reset)}, "updatedAt": z(updated)}, f)

    def decisions(self, *events):
        with open(os.path.join(self.mail, "decisions.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def pre(self, tool, **ti):
        out = rg.run("pre", io.StringIO(json.dumps({"tool_name": tool, "tool_input": ti})), now=NOW)
        return out and out["hookSpecificOutput"]["permissionDecisionReason"]

    def ctx(self):
        out = rg.run("session-start", None, now=NOW)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return out["hookSpecificOutput"]["additionalContext"]

    def log(self):
        try:
            with open(os.path.join(self.home, ".claude/routing-gate.log")) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def valid(self, **agents):
        a = {k: {"model": m, "effort": e, "enforce": enf} for k, (m, e, enf) in agents.items()}
        return {"version": 1, "agents": a, "overrides": [], "rules": {"stop_at": 90, "working_days": rg.DAYS[:5]}}

    # routing file
    def test_missing_routing_uses_contract_defaults(self):
        c = self.ctx()
        self.assertIn("builder: opus@medium (default)", c)
        self.assertIn("security-audit: fable@high (default)", c)
        self.assertNotIn("WARNING", c)

    def test_fable_allowed_only_for_review_rows(self):
        self.routing({"version": 1, "agents": {"security-audit": {"model": "fable", "effort": "high"}}})
        c = self.ctx()
        self.assertIn("security-audit: fable@high", c)
        self.assertNotIn("WARNING", c)
        self.routing({"version": 1, "agents": {"builder": {"model": "fable", "effort": "high"}}})
        self.assertIn("invalid", self.ctx())

    def test_invalid_routing_warns_once_and_never_blocks(self):
        for bad in ("{not json", json.dumps({"version": 1, "agents": {"builder": {"model": "fable", "effort": "high",
                                                                                   "enforce": "lock"}}})):
            self.routing(bad)
            c = self.ctx()
            self.assertEqual(c.count("WARNING"), 1)
            self.assertIn("builder: opus@medium (default)", c)
            self.assertIsNone(self.pre("Agent", subagent_type="builder", model="haiku", effort="low", prompt="x"))

    def test_override_with_bare_date_is_invalid(self):
        cfg = self.valid()
        cfg["overrides"] = [{"id": "o1", "agent": "builder", "model": "opus", "effort": "high", "until": "2026-11-12"}]
        self.routing(cfg)
        self.assertIn("WARNING", self.ctx())

    def test_valid_routing_shows_lock(self):
        self.routing(self.valid(builder=("opus", "high", "lock")))
        self.assertIn("builder: opus@high (lock)", self.ctx())

    # lock and override
    def test_lock_mismatch_denied_match_allowed(self):
        self.routing(self.valid(builder=("opus", "high", "lock")))
        r = self.pre("Agent", subagent_type="builder", model="opus", effort="medium", prompt="x")
        self.assertIn("builder is locked to opus@high", r)
        self.assertIn("relaunch with model opus, effort high", r)
        self.assertIsNone(self.pre("Agent", subagent_type="builder", model="opus", effort="high", prompt="x"))

    def test_default_never_denies(self):
        self.routing(self.valid(builder=("opus", "high", "default")))
        self.assertIsNone(self.pre("Agent", subagent_type="builder", model="haiku", effort="low", prompt="x"))

    def test_override_active_vs_lapsed(self):
        cfg = self.valid()
        cfg["overrides"] = [{"id": "o1", "agent": "builder", "model": "opus", "effort": "high",
                             "until": (NOW + timedelta(days=1)).isoformat()}]
        self.routing(cfg)
        self.assertIn("override until", self.ctx())
        self.assertIn("has an override until", self.pre("Agent", subagent_type="builder", model="opus",
                                                        effort="medium", prompt="x"))
        cfg["overrides"][0]["until"] = (NOW - timedelta(minutes=1)).isoformat()
        self.routing(cfg)
        self.assertIn("builder: opus@medium (default)", self.ctx())
        self.assertIsNone(self.pre("Agent", subagent_type="builder", model="opus", effort="medium", prompt="x"))

    def test_agent_without_model_uses_frontmatter(self):
        self.routing(self.valid(builder=("sonnet", "high", "lock"), reviewer=("opus", "high", "lock")))
        self.assertIsNone(self.pre("Agent", subagent_type="builder", prompt="x"))  # frontmatter sonnet@high
        self.assertIn("reviewer is locked", self.pre("Agent", subagent_type="reviewer", prompt="x"))  # opus@medium

    def test_security_audit_mapping(self):
        self.routing(self.valid(**{"security-audit": ("opus", "high", "lock")}))
        self.assertIn("security-audit is locked",
                      self.pre("Agent", subagent_type="reviewer", description="security audit", prompt="x"))
        # a builder brief that mentions the audit stays a builder
        self.assertIsNone(self.pre("Agent", subagent_type="builder", prompt="fix the security audit findings"))

    def test_model_family_normalized(self):
        self.routing(self.valid(builder=("opus", "high", "lock")))
        self.assertIsNone(self.pre("Bash", command="claude --bg --model claude-opus-5-5 --effort high 'do it'"))
        self.assertIsNone(self.pre("Bash", command="claude --bg --model='opus[1m]' --effort=high 'do it'"))

    # bash launches
    def test_bash_launch_mapping_and_missing_flags(self):
        self.routing(self.valid(builder=("opus", "high", "lock"), reviewer=("opus", "medium", "lock")))
        self.assertIn("builder is locked", self.pre("Bash", command="cd /x && claude --bg --model sonnet 'go'"))
        self.assertIn("builder is locked", self.pre("Bash", command="claude --bg --model opus 'go'"))  # no effort
        self.assertIsNone(self.pre("Bash", command="claude --bg -n repo-review --model opus --effort medium 'x'"))
        self.assertIn("builder is locked",
                      self.pre("Bash", command="FOO=1 claude -p --model sonnet --effort high 'spawn the reviewer'"))
        self.assertIn("builder is locked",
                      self.pre("Bash", command="cd /x\nclaude --bg --model sonnet --effort high 'a\nb'"))

    def test_non_launch_bash_allowed(self):
        self.routing(self.valid(builder=("opus", "high", "lock")))
        for cmd in ("ls -la", "git -C ~/.claude/shared status", "claude rm repo-x", "claude agents",
                    "echo 'claude --bg --model sonnet'", "claude -p 'hi'", "grep claude <<'EOF'\nclaude --bg\nEOF",
                    "git commit -m 'claude --bg --model sonnet'", "tmux send-keys 'claude --bg x' Enter"):
            self.assertIsNone(self.pre("Bash", command=cmd), cmd)
        self.limits(95)  # at the stop, only commands with no claude word plus launch flag pass
        for cmd in ("ls -la", "git -C ~/.claude/shared status", "claude rm repo-x", "claude agents"):
            self.assertIsNone(self.pre("Bash", command=cmd), cmd)

    def test_non_launch_bash_is_fast(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"},
                              "transcript_path": "/x/.claude/projects/t.jsonl"})
        t = time.monotonic()
        p = subprocess.run([sys.executable, "-S", rg.__file__, "pre"], input=payload, capture_output=True, text=True)
        self.assertLess(time.monotonic() - t, 0.2)  # process start included; the gate itself is far below 50 ms
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    # stop
    def test_below_stop_allowed_at_and_above_denied(self):
        self.limits(89)
        self.assertIsNone(self.pre("Agent", subagent_type="scout", prompt="x"))
        for pct in (90, 97):
            self.limits(pct)
            r = self.pre("Agent", subagent_type="scout", prompt="x")
            self.assertIn(f"weekly {pct}% ≥ stop_at 90%", r)
            self.assertIn("routing-ok: <decision id>", r)
        self.assertIn("no new workers", self.pre("Agent", subagent_type="general-purpose", prompt="x"))
        self.assertIn("no new workers", self.pre("Bash", command="claude --bg --model opus --effort medium 'x'"))
        self.assertIn("stopped", self.ctx())

    def ask(self, i, q="routing-ok: launch a builder at 92% weekly?", answer="Launch over the stop (Recommended)", where="toolsmith window",
            ts=None, close="answer", by="toolsmith", via="hook", tid="t1"):
        ts = ts or (NOW - timedelta(hours=1)).isoformat()
        ev = [{"id": i, "event": "open", "ts": ts, "question": q, "role": "toolsmith", "by": "toolsmith",
               "tool_use_id": tid}]
        if close:
            ev.append({"id": i, "event": close, "ts": ts, "answer": answer, "answered_in": where, "by": by, "via": via})
        return ev

    def test_routing_ok_rules(self):
        self.limits(92)
        old = "2026-10-20T10:05:00+00:00"
        self.decisions(*self.ask("ok-1"), *self.ask("ok-2", where="council#12", by="mothership", via=None, tid=None),
                       *self.ask("ok-3", where="page", by="mothership", via=None, tid=None),
                       *self.ask("ok-4", tid=None), {"id": "ok-4", "event": "touch", "ts": NOW.isoformat(),
                                                     "tool_use_id": "t9"},
                       *self.ask("open-1", close=None), *self.ask("wd-1", close="withdraw"),
                       *self.ask("q-1", q="Merge the branch?"), *self.ask("q-2", q="Launch above the weekly stop?"),
                       *self.ask("yes-1", answer="Yes, launch it"),
                       *self.ask("self-1", via=None),  # the role wrote the answer itself
                       *self.ask("self-2", where="council#3", via=None),  # a role claiming a council answer
                       *self.ask("notid-1", tid=None),
                       *self.ask("no-1", answer="No, wait for the reset"), *self.ask("no-2", answer="Cancel"),
                       *self.ask("old-1", ts=old),
                       *self.ask("re-1"), {"id": "re-1", "event": "supersede", "ts": NOW.isoformat()})
        for i in ("ok-1", "ok-2", "ok-3", "ok-4"):
            self.assertIsNone(self.pre("Agent", subagent_type="scout", prompt=f"go. routing-ok: {i}"), i)
        for i in ("ok-1", "open-1", "wd-1", "q-1", "q-2", "yes-1", "self-1", "self-2", "notid-1", "no-1", "no-2", "old-1", "re-1",
                  "unknown-9"):
            self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt=f"routing-ok: {i}"), i)
        with open(os.path.join(self.home, ".claude/routing-gate-used.jsonl")) as f:
            self.assertEqual([json.loads(x)["id"] for x in f], ["ok-1", "ok-2", "ok-3", "ok-4"])

    def test_claim_is_single_use_under_concurrency(self):
        import threading
        res = []
        ts = [threading.Thread(target=lambda: res.append(rg.claim("ok-1"))) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(res), [False] * 7 + [True])

    def pre_from(self, tool, extra, **ti):
        ev = {"tool_name": tool, "tool_input": ti, **extra}
        out = rg.run("pre", io.StringIO(json.dumps(ev)), now=NOW)
        return out and out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_running_subagent_passes_the_stop_but_not_a_lock(self):
        self.limits(95)
        self.routing(self.valid(reviewer=("opus", "high", "lock")))
        sub = {"agent_id": "a123", "agent_type": "builder", "cwd": "/repo/x"}
        self.assertIsNone(self.pre_from("Agent", sub, subagent_type="reviewer", model="opus", effort="high"))
        self.assertIn("reviewer is locked", self.pre_from("Agent", sub, subagent_type="reviewer", effort="low"))
        self.assertIn("no new workers", self.pre_from("Agent", {"agent_id": "", "cwd": "/repo/x"},
                                                      subagent_type="reviewer", model="opus", effort="high"))

    def test_only_agent_calls_get_the_running_worker_exemption(self):
        self.limits(95)
        wt = {"cwd": "/repo/r/.claude/worktrees/feat", "agent_id": "a1"}
        self.assertIn("no new workers", self.pre_from("Bash", wt, command="claude --bg --model opus --effort high x"))
        r = self.pre_from("Agent", {"cwd": "/repo/r/.claude/worktrees/feat"}, subagent_type="reviewer")
        self.assertIn("finish without new subagents and note the skipped step", r)

    # local extension (~/.claude/shared/local/routing_gate_local.json)
    ML = "claude-meter --bg -n repo-x --worktree x --model opus --effort medium 'You are repo-x. Read BRIEF.md'"

    def tool(self, d, name):
        os.makedirs(os.path.join(self.home, d), exist_ok=True)
        p = os.path.join(self.home, d, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
        return p

    def local_cfg(self, cfg=None, temp_ok=True):
        """A fake launcher in ~/bin on PATH and the local file naming it; the test home is under /tmp, so the temp-dir
        rule is off unless temp_ok is False."""
        self.meter = self.tool("bin", "claude-meter")
        env = mock.patch.dict(os.environ, {"PATH": os.path.join(self.home, "bin") + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        if temp_ok:
            t = mock.patch.object(rg, "temp_roots", return_value={"/nonexistent-tmp"})
            t.start()
            self.addCleanup(t.stop)
        if cfg is None:
            cfg = {"metered_launchers": [self.meter], "off_subscription_env": {"GATE_TEST_OFF": ["1", "true"]}}
        with open(os.path.join(self.home, ".claude/shared/local/routing_gate_local.json"), "w") as f:
            f.write(cfg if isinstance(cfg, str) else json.dumps(cfg))

    def test_local_launcher_meets_locks_and_overrides(self):
        self.local_cfg()
        self.routing(self.valid(builder=("opus", "high", "lock")))
        for cmd in (self.ML, "AGENT_MAIL_NAME=repo-x " + self.ML, self.meter + self.ML[len("claude-meter"):],
                    "/opt/bin/" + self.ML, "tmux new-window -n x \"" + self.ML.replace("'", "") + "\"",
                    "claude-meter -p --model sonnet --effort high 'x'"):
            self.assertIn("builder is locked", self.pre("Bash", command=cmd), cmd)
        self.assertIsNone(self.pre("Bash", command=self.ML.replace("medium", "high")))
        cfg = self.valid()
        cfg["overrides"] = [{"id": "o1", "agent": "reviewer", "model": "opus", "effort": "high",
                             "until": (NOW + timedelta(days=1)).isoformat()}]
        self.routing(cfg)
        self.assertIn("reviewer has an override",
                      self.pre("Bash", command="claude-meter --bg -n repo-review --model opus --effort medium x"))
        # a first launch does not hide a second one from its lock
        self.routing(self.valid(builder=("opus", "medium", "lock")))
        self.assertIn("builder is locked", self.pre("Bash", command=self.ML + "; claude --bg --model haiku --effort low y"))
        self.assertIn("builder is locked", self.pre("Bash", command="claude --bg --model opus --effort medium x && "
                                                                    "claude-meter --bg --model haiku y"))

    def test_local_launcher_skips_the_stop_but_not_a_lock(self):
        self.local_cfg()
        self.limits(95)
        for cmd in (self.ML, self.meter + " --bg x", "cd x && claude-meter --bg y", "tmux new-window -n x 'claude-meter --bg x'", "screen -dm claude-meter",
                    "claude-meter -p --model haiku 'x'", "ls ~/.claude-meter -p",
                    "claude-meter --bg 'Read BRIEF.md, source the env, set PATH, call f() and eval \\n'"):
            self.assertIsNone(self.pre("Bash", command=cmd), cmd)
        self.routing(self.valid(builder=("opus", "high", "lock")))
        self.assertIn("builder is locked", self.pre("Bash", command=self.ML))

    def test_unmetered_or_redirected_launcher_meets_the_stop(self):
        self.local_cfg()
        other = self.tool("bin2", "claude-meter")  # a copy elsewhere, same name
        self.limits(95)
        with mock.patch.dict(os.environ, {"GATE_TEST_OFF": "1"}):
            for cmd in ("GATE_TEST_OFF=1 claude --bg --model opus x",
                        "env GATE_TEST_OFF=1 claude --bg --model opus x",
                        "alias claude-meter=claude; claude-meter --bg x",
                        "claude-meter() { claude --bg \"$@\"; }; claude-meter x",
                        self.ML + " && claude --bg --model opus y",
                        "claude-meter --bg x; claude -p --model opus y",
                        "/usr/local/bin/claude --bg x # claude-meter",
                        "tmux new-window 'claude-meter --bg x; claude'",
                        "claude-role toolsmith", "tmux new-window claude",
                        # another claude-x name, or a copy of the launcher outside its listed path
                        "claude-x --bg x", "claude-other -p --model opus x", "/tmp/claude-meter --bg x",
                        other + " --bg x", "./claude-meter --bg x", "~/bin/claude-meter --bg x",
                        "$HOME/bin/claude-meter --bg x", "claude-meter --bg x; my-claude --bg y",
                        # anything that could point the bare name elsewhere
                        f"PATH={os.path.dirname(other)}:$PATH claude-meter --bg x",
                        f"env PATH={os.path.dirname(other)} claude-meter --bg x",
                        f"export PATH={os.path.dirname(other)}; claude-meter --bg x",
                        f"hash -p /tmp/c claude-meter; claude-meter --bg x",
                        "alias claude-meter=/tmp/c; claude-meter --bg x",
                        "function claude-meter { /tmp/c; }; claude-meter --bg x",
                        "claude-meter() { /tmp/c; }; claude-meter --bg x",
                        ". ./x.sh; claude-meter --bg x", "source x.sh; claude-meter --bg x",
                        "eval x; claude-meter --bg x", "\\claude-meter --bg x",
                        "export BASH_ENV=/tmp/e; claude-meter --bg x", "set -a; claude-meter --bg x",
                        # only as the first word of a plain segment: no NAME=, wrapper or quoting before it
                        "AGENT_MAIL_NAME=repo-x " + self.ML, "env claude-meter --bg x", "exec claude-meter --bg x",
                        "command claude-meter --bg x", "nice -n 5 claude-meter --bg x", "nohup claude-meter --bg x",
                        "tmux new-window 'FOO=1 claude-meter --bg x'", "tmux new-window 'env claude-meter --bg x'",
                        "'claude-meter' --bg x", "if true; then claude-meter --bg x; fi"):
                self.assertIn("no new workers", self.pre("Bash", command=cmd) or "", cmd)
            for cmd in ("cat ~/.claude/shared/local/routing_gate_local.json",
                        "echo '{}' > ~/.claude/shared/local/routing_gate_local.json",
                        f"sed -i s/a/b/ {self.meter}", f"cp /tmp/c '{self.meter}'", "cat /tmp/c > ~/bin/claude-meter",
                        f"echo x >{self.meter}", f"{self.meter} --bg x; cp /tmp/c {self.meter}",
                        f"FOO=1 {self.meter} --bg x"):
                self.assertIn("gate's local file", self.pre("Bash", command=cmd), cmd)
        # the hook's own PATH finds another binary first: the bare name is not the listed launcher
        with mock.patch.dict(os.environ, {"PATH": os.path.dirname(other) + os.pathsep + os.environ["PATH"]}):
            self.assertIn("no new workers", self.pre("Bash", command="claude-meter --bg x"))

    def test_off_subscription_skips_the_stop_for_local_agent_calls_only(self):
        self.local_cfg()
        self.limits(95)
        for v in ("1", " TRUE "):
            with mock.patch.dict(os.environ, {"GATE_TEST_OFF": v}):
                self.assertIsNone(self.pre("Agent", subagent_type="reviewer", model="opus", effort="high", prompt="x"))
        with mock.patch.dict(os.environ, {"GATE_TEST_OFF": "1"}):
            self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", isolation="remote", prompt="x"))
            self.assertIn("no new workers", self.pre("Bash", command="claude --bg --model opus --effort medium x"))
            self.routing(self.valid(reviewer=("opus", "high", "lock")))
            self.assertIn("reviewer is locked", self.pre("Agent", subagent_type="reviewer", effort="low"))
        for v in ("0", "", "false", "yes"):
            with mock.patch.dict(os.environ, {"GATE_TEST_OFF": v}):
                self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="x"), v)
        self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="x"))

    def test_invalid_local_file_means_no_local_file(self):
        self.limits(95)
        meter = os.path.join(self.home, "bin", "claude-meter")
        fake_claude = self.tool("bin", "claude")
        os.symlink(fake_claude, os.path.join(self.home, "bin", "claude-alias"))
        env = {"GATE_TEST_OFF": ["1"]}
        for cfg in ("not json", "[]", {"metered_launchers": [meter], "off_subscription_env": env, "run": "x"},
                    {"metered_launchers": meter, "off_subscription_env": env},
                    {"metered_launchers": ["bin/claude-meter"], "off_subscription_env": env},
                    {"metered_launchers": [os.path.join(self.home, "bin", "missing-meter")]},
                    {"metered_launchers": [os.path.join(self.home, "bin", "claude-nope")], "off_subscription_env": env},
                    {"metered_launchers": [os.path.join(self.home, "bin", "claude-alias")], "off_subscription_env": env},
                    {"metered_launchers": [meter], "off_subscription_env": {"GATE_TEST_OFF": [""]}},
                    {"metered_launchers": [meter], "off_subscription_env": {"GATE_TEST_OFF": []}},
                    {"metered_launchers": [meter], "off_subscription_env": {"GATE_TEST_OFF": "1"}},
                    {"metered_launchers": [meter], "off_subscription_env": {"GATE_TEST_OFF": [1]}}):
            with self.subTest(cfg=cfg):
                self.local_cfg(cfg)
                with mock.patch.dict(os.environ, {"GATE_TEST_OFF": "1"}):
                    self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="x") or "")
                    self.assertIn("no new workers", self.pre("Bash", command="claude-meter --bg x"))
        self.local_cfg("not json")
        self.limits(50)
        os.remove(os.path.join(self.home, ".claude/routing-gate.log"))
        self.assertIsNone(self.pre("Bash", command="claude-meter --bg --model haiku x; claude-meter --bg x"))
        self.assertEqual(self.log().count("local: ignored"), 1)  # once per run, not once per launch

    def test_launcher_in_a_temp_dir_is_refused(self):
        self.limits(95)
        self.local_cfg(temp_ok=False)  # the test home is under /tmp
        self.assertTrue(self.meter.startswith(tuple(rg.temp_roots())))
        with mock.patch.dict(os.environ, {"GATE_TEST_OFF": "1"}):
            self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="x"))
            self.assertIn("no new workers", self.pre("Bash", command=self.meter + " --bg x"))
        self.assertIn("temp dir", self.log())

    def test_local_file_timeout_denies_at_the_stop(self):
        self.local_cfg()
        self.limits(95)
        real = rg.check_launcher
        with mock.patch.object(rg, "check_launcher", side_effect=lambda p: (time.sleep(3), real(p))[1]), \
                mock.patch.dict(os.environ, {"GATE_TEST_OFF": "1"}):
            self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="x"))
            self.assertIn("no new workers", self.pre("Bash", command="claude-meter --bg x") or "")
            self.assertIn("no new workers", self.pre("Bash", command="claude-x --bg x") or "")

    def test_stop_message_names_the_exact_option(self):
        self.limits(95)
        r = self.pre("Agent", subagent_type="scout")
        self.assertIn('first option exactly "Launch over the stop"', r)
        self.assertIn("`routing-ok`", r)

    def test_interactive_sessions_denied_at_stop(self):
        self.limits(95)
        for cmd in ("claude-role toolsmith", "tmux new-window claude", "screen -dm claude", "tmux new -s x 'claude'"):
            self.assertIn("no new workers", self.pre("Bash", command=cmd) or "", cmd)
        self.assertIsNone(self.pre("Bash", command="tmux ls; ls ~/.claude"))
        self.limits(50)
        self.assertIsNone(self.pre("Bash", command="claude-role toolsmith"))

    def test_routing_ok_needs_one_plain_claude_command(self):
        self.limits(92)
        self.decisions(*self.ask("ok-1"))
        for cmd in ("claude --bg --model opus x 'routing-ok: ok-1'; claude --bg --model opus y",
                    "claude --bg --model opus 'routing-ok: ok-1' && claude --bg --model opus y",
                    "for i in 1 2; do claude --bg --model opus 'routing-ok: ok-1'; done",
                    "bash -c \"claude --bg --model opus 'routing-ok: ok-1'\"",
                    "claude --bg --model opus \"routing-ok: ok-1 $(claude --bg --model opus y)\"",
                    "claude --bg --model opus \"routing-ok: ok-1 `true`\"",
                    "nohup claude --bg --model opus 'routing-ok: ok-1'",
                    "claude --bg --model opus \"routing-ok: ok-1 ${X}\"",
                    "claude --bg --model opus $'routing-ok: ok-1'",
                    "claude --bg --model opus 'routing-ok: ok-1' # x",
                    "env NODE_OPTIONS=--require=/tmp/x.js claude --bg --model opus 'routing-ok: ok-1'",
                    "A=1 claude --bg --model opus 'routing-ok: ok-1'"):
            self.assertIn("one plain claude command", self.pre("Bash", command=cmd), cmd)
        self.assertIsNone(self.pre("Bash", command="claude --bg --model opus 'fix $x; then (b) | c #1. routing-ok: ok-1'"))

    def test_tripwire_at_stop(self):
        self.limits(95)
        for cmd in ("python3.11 agent_mail.py decide-hook post < fake.json", "echo '{}' >> ~/.agent-mail/decisions.jsonl",
                    "AGENT_MAIL_NAME=mothership python3.11 agent_mail.py decide close x answer"):
            self.assertIn("decisions store", self.pre("Bash", command=cmd), cmd)
        self.limits(50)
        self.assertIsNone(self.pre("Bash", command="tail ~/.agent-mail/decisions.jsonl"))

    def test_bad_usage_and_routing_are_logged(self):
        with open(os.path.join(self.home, ".claude/usage/limits.json"), "w") as f:
            f.write("{bad")
        self.routing("{bad")
        self.assertIsNone(self.pre("Agent", subagent_type="scout"))
        self.assertIn("limits.json unreadable", self.log())
        self.assertIn("routing.json unreadable", self.log())

    def test_claim_adds_missing_newline(self):
        with open(os.path.join(self.home, ".claude/routing-gate-used.jsonl"), "w") as f:
            f.write('{"id": "ok-0"}')
        self.assertTrue(rg.claim("ok-1"))
        with open(os.path.join(self.home, ".claude/routing-gate-used.jsonl")) as f:
            self.assertEqual([json.loads(x)["id"] for x in f], ["ok-0", "ok-1"])

    def test_claim_skips_corrupt_lines(self):
        with open(os.path.join(self.home, ".claude/routing-gate-used.jsonl"), "w") as f:
            f.write('{"id": "ok-0"}\n{"id": "ok-1\n[]\n')
        self.assertTrue(rg.claim("ok-1"))
        self.assertFalse(rg.claim("ok-0"))

    def test_stop_check_uses_whole_command_without_parsing(self):
        pad = "echo " + "x" * (rg.CAP + 10) + "; "
        doc = "cat > b.md <<'EOF'\nUse claude --bg --model sonnet later\nEOF"
        for cmd in (pad + "claude --bg --model opus --effort medium x", doc, "claude -cp 'x'",
                    "nohup bash -c \"claude --background x\""):
            self.limits(50)
            self.assertIsNone(self.pre("Bash", command=cmd), cmd[-40:])
            self.limits(95)
            self.assertIn("no new workers", self.pre("Bash", command=cmd), cmd[-40:])
        self.assertIn("no new workers", self.pre("Bash", command="ls claude-config -p"))  # a claude-x word, then a flag
        for cmd in ("mkdir -p ~/.claude/x", "git -C ~/.claude/shared log -p", "ls -p claude-config"):
            self.assertIsNone(self.pre("Bash", command=cmd), cmd)

    def test_deny_survives_ascii_stdout(self):
        now = datetime.now(timezone.utc)
        self.limits(95, updated=now, reset=now + timedelta(days=1))
        p = subprocess.run([sys.executable, rg.__file__, "pre"], capture_output=True, text=True,
                           input=json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": "scout"}}),
                           env={**os.environ, "PYTHONIOENCODING": "ascii", "LANG": "C", "LC_ALL": "C"})
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_bash_name_hints_are_whole_words(self):
        self.routing(self.valid(scout=("haiku", "medium", "lock"), reviewer=("opus", "high", "lock")))
        self.assertIsNone(self.pre("Bash", command="claude --bg -n kode-scout-lead --model sonnet --effort low x"))
        self.assertIsNone(self.pre("Bash", command="claude --bg -n preview-fix --model sonnet --effort low x"))
        self.assertIn("reviewer is locked", self.pre("Bash", command="claude --bg -n kode-review --model opus x"))
        self.assertIn("scout is locked", self.pre("Bash", command="claude --bg --agent scout --model sonnet x"))

    def test_security_audit_from_description_only(self):
        self.routing(self.valid(**{"security-audit": ("opus", "high", "lock")}))
        self.assertIsNone(self.pre("Agent", subagent_type="reviewer", description="review diff",
                                   prompt="this is not a security audit"))

    def test_override_without_until_is_invalid(self):
        cfg = self.valid()
        cfg["overrides"] = [{"id": "o1", "agent": "builder", "model": "opus", "effort": "high"}]
        self.routing(cfg)
        self.assertIn("WARNING", self.ctx())

    def test_routing_ok_not_consumed_when_lock_denies(self):
        self.limits(92)
        self.routing(self.valid(builder=("opus", "high", "lock")))
        self.decisions(*self.ask("ok-1"))
        self.assertIn("locked", self.pre("Agent", subagent_type="builder", model="opus", effort="low",
                                         prompt="routing-ok: ok-1"))
        self.assertIsNone(self.pre("Agent", subagent_type="builder", model="opus", effort="high",
                                   prompt="routing-ok: ok-1"))

    def test_store_error_denies_at_stop(self):
        self.limits(92)  # no decisions.jsonl at all
        self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="routing-ok: ok-1"))
        with mock.patch.dict(os.environ, {"AGENT_MAIL_DIR": ""}), \
                mock.patch.object(rg, "home", return_value=self.home):
            self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="routing-ok: ok-1"))

    def test_used_file_write_failure_denies_at_stop(self):
        self.limits(92)
        self.decisions(*self.ask("ok-1"))
        with mock.patch.object(rg, "claim", side_effect=OSError("disk full")):
            self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="routing-ok: ok-1"))

    def test_timeout_after_stop_denies(self):
        self.limits(92)
        with mock.patch.object(rg, "approved", side_effect=lambda *_: time.sleep(3)):
            self.assertIn("no new workers", self.pre("Agent", subagent_type="scout", prompt="routing-ok: x"))
        self.limits(50)
        with mock.patch.object(rg, "agent_launch", side_effect=lambda *_: time.sleep(3)):
            self.assertIsNone(self.pre("Agent", subagent_type="scout", prompt="x"))

    def test_bash_wrappers_unwrapped(self):
        self.routing(self.valid(builder=("opus", "high", "lock")))
        for cmd in ("env FOO=1 claude --bg --model sonnet --effort high x",
                    "nohup claude --bg --model sonnet --effort high x &",
                    "timeout 30 nice -n 5 setsid claude --bg --model sonnet --effort high x",
                    "command exec /usr/local/bin/claude --bg --model sonnet --effort high x",
                    "bash -c 'cd /r && claude --bg --model sonnet --effort high x'",
                    "tmux new-window -n w \"claude --bg --model sonnet --effort high x\"",
                    "tmux new-window \"bash -c 'claude --bg --model sonnet --effort high x'\"",
                    "cat <<EOF > f\nclaude --bg is text here\nEOF\nclaude --bg --model sonnet --effort high x"):
            self.assertIn("builder is locked", self.pre("Bash", command=cmd) or "", cmd)
        self.assertIsNone(self.pre("Bash", command="env FOO=1 claude --bg --model opus --effort high x"))

    def test_unparseable_launch_denied_only_at_stop(self):
        self.routing(self.valid(builder=("opus", "high", "lock")))
        cmd = "claude --bg --model sonnet 'unterminated"
        self.assertIsNone(self.pre("Bash", command=cmd))
        self.limits(95)
        self.assertIn("no new workers", self.pre("Bash", command=cmd))

    def test_stale_or_reset_limits_mean_unknown(self):
        self.limits(99, updated=NOW - timedelta(hours=7))
        self.assertIsNone(self.pre("Agent", subagent_type="scout", prompt="x"))
        self.assertIn("usage unknown", self.ctx())
        self.limits(99, reset=NOW - timedelta(minutes=1))
        self.assertIsNone(self.pre("Agent", subagent_type="scout", prompt="x"))

    # pace and shape
    def test_pace_and_states(self):
        # Thursday noon UTC: Mon, Tue, Wed done, Thu 6 h of 24 into its slice (slices start 06:00) -> 3.25 of 5
        self.assertAlmostEqual(rg.pace(90, list(rg.DAYS[:5]), RESET, NOW), 90 * 3.25 / 5)
        self.limits(50)
        self.assertIn("within pace", self.ctx())
        self.limits(70)
        self.assertIn("ahead of pace: drop one rung", self.ctx())
        self.assertIsNone(self.pre("Agent", subagent_type="builder", model="opus", effort="high", prompt="x"))

    def test_session_start_shape_and_lines(self):
        self.routing("{bad")
        self.limits(50)
        c = self.ctx()
        self.assertLessEqual(len(c.splitlines()), 15)
        self.assertEqual(len(c.splitlines()), 8)  # title, warning, five agents, usage
        p = subprocess.run([sys.executable, rg.__file__, "session-start"], capture_output=True, text=True,
                           env={**os.environ})
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")

    # fail open
    def test_crash_allows_and_logs(self):
        self.limits(99)
        self.assertIsNone(rg.run("pre", io.StringIO("not json"), now=NOW))
        with mock.patch.object(rg, "load_routing", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.pre("Agent", subagent_type="scout", prompt="x"))
        self.assertIn("boom", self.log())
        self.assertEqual(len(self.log().splitlines()), 2)

    def test_timeout_allows(self):
        def slow(*_):
            time.sleep(3)
        with mock.patch.object(rg, "load_routing", side_effect=slow):
            t = time.monotonic()
            self.assertIsNone(self.pre("Agent", subagent_type="scout", prompt="x"))
            self.assertLess(time.monotonic() - t, 1.5)
        self.assertIn("TimeoutError", self.log())


if __name__ == "__main__":
    unittest.main()
