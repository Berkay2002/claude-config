#!/usr/bin/env python3.11
"""t3-tmux-bridge with a fake live session: checks the stream-json frames T3 gets. No tmux, no claude, no T3.
Run: python3.11 -B -m unittest -v test_t3_bridge
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "t3-tmux-bridge")
tb = SourceFileLoader("t3_tmux_bridge", BRIDGE).load_module()


class FakeSession:
    session_id, pane = "sid-1", "%1"

    def __init__(self):
        self.live = {"cwd": "/tmp/toy", "pid": 1, "version": "x"}
        self.dialogs, self.sent, self.calls, self.offsets = {}, [], [], []

    def send(self, text, files=()):
        self.calls.append(("send", text))

    def answer(self, did, choice):
        self.calls.append(("answer", did, choice))

    def set_model(self, model=None, effort=None):
        self.calls.append(("set_model", model, effort))

    def interrupt(self):
        self.calls.append(("interrupt",))

    def save_offset(self, offset):
        self.offsets.append(offset)


def wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)
    return cond()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = io.StringIO()
        self.s = FakeSession()
        self.b = tb.Bridge({"--bridge-role": ["toy"], "--bridge-claude-dir": [self.tmp.name], "--model": ["haiku"]},
                           [], out=self.out, session=self.s)

    def tearDown(self):
        self.b.logf.close()
        self.tmp.cleanup()

    def frames(self):
        f = [json.loads(ln) for ln in self.out.getvalue().splitlines()]
        self.out.seek(0)
        self.out.truncate()
        return f

    @staticmethod
    def shape(frames):
        """Fixture-style summary: type/subtype and parent_tool_use_id."""
        return [(f["type"], f.get("subtype") or (f["message"]["content"][0]["type"]
                                                 if f["type"] in ("assistant", "user") else None),
                 f.get("parent_tool_use_id")) for f in frames]

    def ev(self, kind, **kw):
        base = {"parent_tool_use_id": None, "offset": 10, "replay": False}
        self.b.on_event({**base, "kind": kind, **kw})

    def t3_turn(self, text="hi", uid="t3-1"):
        self.b.on_user({"type": "user", "uuid": uid, "message": {"role": "user", "content": text}})
        self.ev("turn_start", origin="human", text=text, by_us=True, peer=None)

    def end_turn(self, text="done"):
        self.ev("turn_end", reason="done", last_text=text)


class Turns(Base):
    def test_t3_turn(self):
        self.t3_turn()
        self.ev("text", block={"type": "text", "text": "ok"}, message={"id": "m"}, uuid="u1")
        self.end_turn("ok")
        f = self.frames()
        self.assertEqual(self.shape(f), [("system", "init", None), ("assistant", "text", None),
                                         ("result", "success", None)])
        self.assertEqual(f[0]["user_message_uuids"], ["t3-1"])
        self.assertEqual(f[-1]["user_message_uuids"], ["t3-1"])
        self.assertEqual(f[-1]["result"], "ok")
        self.assertTrue(wait_for(lambda: ("send", "hi") in self.s.calls))
        self.assertEqual(self.s.offsets, [10])

    def test_events_before_t3_can_route_them_are_held_then_shown_as_wake_turns(self):
        self.ev("turn_start", origin="human", text="typed while away", by_us=False, peer=None, replay=True)
        self.ev("text", block={"type": "text", "text": "answer"}, message={"id": "m"}, uuid="u1", replay=True)
        self.ev("turn_end", reason="done", last_text="answer", offset=5, replay=True)
        self.assertEqual(self.frames(), [])
        self.t3_turn()
        self.end_turn()
        f = self.frames()
        self.assertEqual([x["type"] for x in f], ["system", "result", "system", "assistant", "result",
                                                 "system", "assistant", "assistant", "result"])
        self.assertIn("1 turn(s) ran while T3 was not attached", f[3]["message"]["content"][0]["text"])
        self.assertIn("(typed in tmux/RC, earlier) typed while away", f[6]["message"]["content"][0]["text"])
        self.assertNotIn("user_message_uuids", f[5])
        self.assertEqual(self.s.offsets[-1], 10)   # the newest delivered offset wins
        self.b.on_event({"kind": "turn_end", "reason": "done", "last_text": "", "offset": 7})
        self.assertEqual(self.s.offsets[-1], 10)

    def test_task_notification_wake_turn_shows_the_summary_not_the_xml(self):
        self.t3_turn()
        self.end_turn()
        self.frames()
        self.ev("turn_start", origin="task-notification", by_us=False, peer=None,
                text="<task-notification><task-id>a1</task-id><status>completed</status>"
                     "<summary>Agent done</summary></task-notification>")
        f = self.frames()
        self.assertEqual(f[1]["message"]["content"][0]["text"], "› (background task completed: Agent done)")

    def test_send_failed_closes_the_unlanded_t3_turn_with_a_notice(self):
        self.b.on_user({"type": "user", "uuid": "t3-1", "message": {"role": "user", "content": "hi"}})
        self.ev("send_failed", text="hi", reason="draft")
        f = self.frames()
        self.assertEqual([x["type"] for x in f], ["system", "assistant", "result"])
        self.assertIn("someone has text typed in the tmux pane", f[1]["message"]["content"][0]["text"])
        self.assertIsNone(self.b.turn)


class Subagents(Base):
    def test_foreground_agent_frames_follow_the_sdk_order(self):
        self.t3_turn()
        self.frames()
        self.ev("tool_use", block={"type": "tool_use", "id": "ta", "name": "Agent", "input": {}}, message={})
        self.ev("subagent_start", id="a1", tool_use_id="ta", description="count", prompt="run ls",
                agent_type="general-purpose", background=False)
        self.ev("tool_use", block={"type": "tool_use", "id": "c1", "name": "Bash", "input": {}}, message={},
                parent_tool_use_id="ta")
        self.ev("tool_result", block={"type": "tool_result", "tool_use_id": "c1", "content": "a"},
                parent_tool_use_id="ta", tool="Bash")
        self.ev("subagent_end", id="a1", tool_use_id="ta", status="completed", summary="2 files")
        self.ev("tool_result", block={"type": "tool_result", "tool_use_id": "ta", "content": "2 files"},
                tool="Agent", tool_use_result={"status": "completed", "agentId": "a1"})
        f = self.frames()
        # The order of the T3 replay fixture `subagent` (task_progress frames aside).
        self.assertEqual(self.shape(f), [
            ("assistant", "tool_use", None), ("system", "task_started", None), ("user", "text", "ta"),
            ("assistant", "tool_use", "ta"), ("user", "tool_result", "ta"), ("system", "task_notification", None),
            ("user", "tool_result", None)])
        self.assertEqual((f[1]["task_id"], f[1]["tool_use_id"], f[1]["is_backgrounded"]), ("a1", "ta", False))
        self.assertEqual((f[5]["status"], f[5]["summary"]), ("completed", "2 files"))

    def test_background_agent_announces_before_the_async_result_and_clears_the_roster(self):
        self.t3_turn()
        self.frames()
        self.ev("tool_use", block={"type": "tool_use", "id": "tb", "name": "Agent", "input": {}}, message={})
        self.ev("tool_result", block={"type": "tool_result", "tool_use_id": "tb", "content": "launched"},
                tool="Agent", tool_use_result={"status": "async_launched", "agentId": "b1", "description": "bg",
                                               "prompt": "p"})
        self.ev("subagent_start", id="b1", tool_use_id="tb", description="bg", prompt="p", agent_type="x",
                background=True)
        self.end_turn()
        self.ev("text", block={"type": "text", "text": "child"}, message={}, parent_tool_use_id="tb")
        self.ev("subagent_end", id="b1", tool_use_id="tb", status="completed", summary="ok")
        f = self.frames()
        # The order of the T3 replay fixture `claude_background_subagent_lifecycle`.
        self.assertEqual(self.shape(f), [
            ("assistant", "tool_use", None), ("system", "background_tasks_changed", None),
            ("system", "task_started", None), ("user", "tool_result", None), ("result", "success", None),
            ("assistant", "text", "tb"), ("system", "init", None), ("system", "task_notification", None),
            ("system", "background_tasks_changed", None)])
        self.assertEqual(f[1]["tasks"], [{"task_id": "b1", "task_type": "local_agent", "description": "bg"}])
        self.assertTrue(f[2]["is_backgrounded"])
        self.assertEqual(f[-1]["tasks"], [])


class Dialogs(Base):
    def open_dialog(self, dialog="permission"):
        self.t3_turn()
        self.ev("dialog_open", id="perm-1", dialog=dialog, tool="Bash", input={"command": "ls"})
        f = self.frames()[-1]
        self.assertEqual(f["request"]["subtype"], "can_use_tool")
        return f["request_id"]

    def respond(self, rid, subtype="success", **body):
        self.b.on_control_response({"type": "control_response",
                                    "response": {"subtype": subtype, "request_id": rid, "response": body}})

    def answers(self):
        return [c for c in self.s.calls if c[0] == "answer"]

    def test_human_allow_is_pressed(self):
        self.respond(self.open_dialog(), behavior="allow", decisionClassification="user_temporary")
        self.assertEqual(self.answers(), [("answer", "perm-1", "allow")])

    def test_allow_for_session_is_pressed_once_with_a_notice(self):
        self.respond(self.open_dialog(), behavior="allow", decisionClassification="user_permanent")
        self.assertEqual(self.answers(), [("answer", "perm-1", "allow")])
        self.assertIn("'always allow' is not applied", self.out.getvalue())

    def test_human_deny_is_pressed(self):
        self.respond(self.open_dialog(), behavior="deny", decisionClassification="user_reject")
        self.assertEqual(self.answers(), [("answer", "perm-1", "deny")])

    def test_auto_allow_without_a_person_presses_nothing(self):
        self.respond(self.open_dialog(), behavior="allow", updatedInput={"command": "ls"})
        self.assertEqual(self.answers(), [])
        self.assertIn("without a person deciding", self.out.getvalue())

    def test_error_response_presses_nothing(self):
        self.respond(self.open_dialog(), subtype="error")
        self.assertEqual(self.answers(), [])

    def test_question_answers_are_passed_on(self):
        rid = self.open_dialog("question")
        self.respond(rid, behavior="allow", updatedInput={"answers": {"Pick": "Blue"}})
        self.assertEqual(self.answers(), [("answer", "perm-1", {"Pick": "Blue"})])

    def test_dialog_answered_elsewhere_is_cancelled_in_t3(self):
        rid = self.open_dialog()
        self.ev("dialog_closed", id="perm-1", by="elsewhere")
        self.assertEqual(self.frames(), [{"type": "control_cancel_request", "request_id": rid}])

    def test_call_written_after_its_dialog_keeps_the_dialog_id(self):
        self.open_dialog()
        self.ev("tool_use", block={"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}, message={},
                dialog_id="perm-1")
        self.ev("tool_result", block={"type": "tool_result", "tool_use_id": "t1", "content": ""}, tool="Bash")
        f = self.frames()
        self.assertEqual(f[0]["message"]["content"][0]["id"], "perm-1")
        self.assertEqual(f[1]["message"]["content"][0]["tool_use_id"], "perm-1")

    def test_dialog_for_a_call_t3_has_uses_its_id(self):
        self.t3_turn()
        self.ev("dialog_open", id="perm-2", dialog="permission", tool="Bash", input={}, tool_use_id="t9")
        self.assertEqual(self.frames()[-1]["request"]["tool_use_id"], "t9")


class Controls(Base):
    def control(self, subtype, **req):
        self.b.on_control({"type": "control_request", "request_id": "r", "request": {"subtype": subtype, **req}})

    def test_model_and_effort_apply_only_when_t3_changes_them(self):
        self.control("set_model", model="haiku")                     # T3's spawn pick: already what it asked for
        self.control("apply_flag_settings", settings={"effortLevel": None})
        time.sleep(0.1)
        self.assertEqual(self.s.calls, [])
        self.control("set_model", model="sonnet")
        self.control("apply_flag_settings", settings={"effortLevel": "low"})
        self.control("apply_flag_settings", settings={"effortLevel": "low"})
        self.assertTrue(wait_for(lambda: len(self.s.calls) == 2))
        self.assertEqual(set(self.s.calls), {("set_model", None, "low"), ("set_model", "sonnet", None)})

    def test_unknown_control_gets_an_error(self):
        self.control("rewind_files")
        self.assertEqual(json.loads(self.out.getvalue().splitlines()[-1])["response"]["subtype"], "error")


class Deploys(Base):
    def test_code_change_exits_once_idle(self):
        code = os.path.join(self.tmp.name, "code.py")
        open(code, "w").close()
        exited = []
        self.b.code_files = lambda: [code]
        self.b.exit = lambda: exited.append(True)
        self.b.turn = {"t3_uuid": "x"}
        old, tb.DEPLOY_POLL = tb.DEPLOY_POLL, 0.05
        try:
            import threading
            threading.Thread(target=self.b.watch_deploys, daemon=True).start()
            time.sleep(0.1)
            os.utime(code, ns=(time.time_ns(), time.time_ns() + 10**9))
            time.sleep(0.3)
            self.assertEqual(exited, [])   # a turn is open
            self.b.turn = None
            self.assertTrue(wait_for(lambda: exited))
        finally:
            tb.DEPLOY_POLL = old


class Argv(unittest.TestCase):
    def test_strip_bridge_flags(self):
        self.assertEqual(tb.strip_bridge_flags(["--version", "--bridge-role", "r", "--bridge-start=x y",
                                                "--bridge-claude-dir", "/d", "--bridge-tmux-session", "t", "-p"]),
                         ["--version", "-p"])

    def test_probe_execs_the_real_claude_without_bridge_flags(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "claude")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\necho \"$@\"\n")
            os.chmod(fake, 0o700)
            r = subprocess.run([sys.executable, BRIDGE, "--version", "--bridge-role", "toy"], capture_output=True,
                               text=True, env={**os.environ, "T3_BRIDGE_CLAUDE": fake})
            self.assertEqual(r.stdout.strip(), "--version")

    def test_real_claude_next_to_the_link(self):
        with tempfile.TemporaryDirectory() as d:
            link, near = os.path.join(d, "t3-tmux-bridge"), os.path.join(d, "claude")
            os.symlink(BRIDGE, link)
            with open(near, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(near, 0o700)
            env = {k: v for k, v in os.environ.items() if k != "T3_BRIDGE_CLAUDE"}
            old_argv, old_env = sys.argv, os.environ
            try:
                sys.argv, os.environ = [link], env
                self.assertEqual(tb.real_claude(), near)
            finally:
                sys.argv, os.environ = old_argv, old_env

    def test_log_dir_and_file_are_private(self):
        with tempfile.TemporaryDirectory() as d:
            b = tb.Bridge({"--bridge-role": ["toy"], "--bridge-claude-dir": [d]}, [], out=io.StringIO(),
                          session=FakeSession())
            b.logf.close()
            self.assertEqual(os.stat(os.path.join(d, "t3-bridge")).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(os.path.join(d, "t3-bridge", "bridge-toy.log")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
