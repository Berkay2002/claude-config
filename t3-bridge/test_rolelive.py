#!/usr/bin/env python3.11
"""rolelive core with fake screens, a temp claude dir and hand-written transcripts. No tmux, no claude.
Run: python3.11 -B -m unittest -v test_rolelive
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rolelive  # noqa: E402

DATA = os.path.join(HERE, "testdata")
RULE = "\x1b[38;5;244m" + "─" * 40 + "\x1b[39m"
PERMISSION_SCREEN = "\n".join([
    "● Bash(echo perm-test > p.txt)", RULE, " Bash command", "",
    "   {cmd}", "", " Do you want to proceed?", " ❯ 1. Yes", "   2. Yes, and don't ask again for echo commands",
    "   3. No", "", " Esc to cancel"])


def wait_for(cond, timeout=3.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.05)
    return cond()


def screen_file(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return f.read()


class Fake(rolelive.LiveSession):
    """A LiveSession whose pane is a string and whose key presses are recorded."""

    def __init__(self, claude_dir, screen=""):
        super().__init__(role="toy", claude_dir=claude_dir)
        self.live = {"sessionId": "sid-1", "cwd": "/tmp/toy", "tmux": "test:@1.%1", "pid": 1, "status": "idle"}
        self.scr, self.keys, self.pasted, self.events = screen, [], [], []
        self.on_event = self.events.append

    def screen(self, escapes=False):
        return self.scr if escapes else rolelive.SGR.sub("", self.scr)

    def _keys(self, *keys):
        self.keys += keys

    def _paste(self, text, submit=True):
        self.pasted.append(text)
        return True

    def kinds(self):
        return [e["kind"] for e in self.events]


def assistant(*blocks, uid="u"):
    return {"type": "assistant", "uuid": uid, "message": {"id": "m", "role": "assistant", "content": list(blocks)}}


def result(tid, content="ok", tur=None):
    return {"type": "user", "uuid": "r-" + tid, "toolUseResult": tur,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def prompt(text, kind="human"):
    return {"type": "user", "turnPosition": 1, "origin": {"kind": kind},
            "message": {"role": "user", "content": text}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.s = Fake(self.dir)

    def tearDown(self):
        self.s.stopped = True
        self.tmp.cleanup()


class InputLine(unittest.TestCase):
    def test_empty_box(self):
        self.assertEqual(rolelive.input_line(screen_file("screen-empty.txt")), "")

    def test_draft(self):
        self.assertEqual(rolelive.input_line(screen_file("screen-draft.txt")), "someone is typing here")

    def test_multiline_draft(self):
        self.assertTrue(rolelive.input_line(screen_file("screen-draft-multiline.txt"))
                        .startswith("Call the AskUserQuestion tool"))

    def test_dialog_has_no_box(self):
        self.assertIsNone(rolelive.input_line(PERMISSION_SCREEN.format(cmd="echo hi")))


class Send(Base):
    def test_refuses_to_type_over_a_draft(self):
        self.s.scr = screen_file("screen-draft.txt")
        old, rolelive.DRAFT_WAIT = rolelive.DRAFT_WAIT, 0.2
        try:
            self.assertFalse(self.s.send("hello"))
        finally:
            rolelive.DRAFT_WAIT = old
        self.assertEqual(self.s.pasted, [])
        self.assertEqual(self.s.events[-1], {"kind": "send_failed", "text": "hello", "reason": "draft"})

    def test_types_into_an_empty_box(self):
        self.s.scr = screen_file("screen-empty.txt")
        self.assertTrue(self.s.send("hello"))
        self.assertEqual(self.s.pasted, ["hello"])
        self.assertEqual(self.s.sent[0][0], "hello")

    def test_lost_send_is_dropped_after_the_timeout(self):
        t0 = 1000.0
        self.s.sent, self.s.idle_since = [["lost", t0]], t0
        self.s._expire_sends(t0 + rolelive.SEND_TIMEOUT - 1)
        self.assertEqual(self.s.events, [])
        self.s._expire_sends(t0 + rolelive.SEND_TIMEOUT + 1)
        self.assertEqual(self.s.sent, [])
        self.assertEqual(self.s.events, [{"kind": "send_failed", "text": "lost", "reason": "timeout"}])

    def test_landed_send_is_ours(self):
        self.s.sent = [["hello  there", time.time()]]
        self.s._entry(prompt("hello there"))
        self.assertTrue(self.s.events[-1]["by_us"])
        self.assertEqual(self.s.sent, [])


class Dialogs(Base):
    def open(self, did, tool, inp, synthetic=True):
        self.s.dialogs[did] = {"dialog": "permission" if tool != "AskUserQuestion" else "question", "tool": tool,
                               "input": inp, "synthetic": synthetic}

    def test_allow_presses_yes_once(self):
        self.open("p1", "Bash", {"command": "echo perm-test > p.txt"})
        self.s.scr = PERMISSION_SCREEN.format(cmd="echo perm-test > p.txt")
        self.assertTrue(self.s.answer("p1", "allow"))
        self.assertEqual(self.s.keys, ["1"])

    def test_deny_presses_the_last_no_row(self):
        self.open("p1", "Bash", {"command": "echo perm-test > p.txt"})
        self.s.scr = PERMISSION_SCREEN.format(cmd="echo perm-test > p.txt")
        self.s.answer("p1", "deny")
        self.assertEqual(self.s.keys, ["3"])

    def test_wrapped_long_command_matches(self):
        cmd = "printf '%s\\n' " + " ".join(f"word{i}" for i in range(30))
        self.open("p1", "Bash", {"command": cmd})
        self.s.scr = PERMISSION_SCREEN.format(cmd=cmd[:50] + "\n   " + cmd[50:])
        self.assertTrue(self.s.answer("p1", "allow"))

    def test_other_dialog_on_screen_is_not_answered(self):
        self.open("p1", "Bash", {"command": "rm -rf build"})
        self.s.scr = PERMISSION_SCREEN.format(cmd="echo perm-test > p.txt")
        self.assertFalse(self.s.answer("p1", "allow"))
        self.assertEqual(self.s.keys, [])

    def test_command_only_in_the_history_above_the_dialog_is_not_enough(self):
        self.open("p1", "Bash", {"command": "echo perm-test > p.txt"})
        self.s.scr = PERMISSION_SCREEN.format(cmd="rm -rf build")   # the history line above still shows the echo
        self.assertFalse(self.s.answer("p1", "allow"))

    def test_dialog_whose_hint_contains_another_fails_closed(self):
        self.open("p1", "Bash", {"command": "echo x"})
        self.open("p2", "Bash", {"command": "echo x && rm -rf build"})
        self.s.scr = PERMISSION_SCREEN.format(cmd="echo x && rm -rf build")
        self.assertFalse(self.s.answer("p2", "allow"))

    def test_two_dialogs_with_the_same_hint_fail_closed(self):
        self.open("p1", "Bash", {"command": "echo x"})
        self.open("p2", "Bash", {"command": "echo x"})
        self.s.scr = PERMISSION_SCREEN.format(cmd="echo x")
        self.assertFalse(self.s.answer("p1", "allow"))
        self.assertEqual(self.s.keys, [])

    def test_input_without_strings_fails_closed(self):
        self.open("p1", "SomeTool", {"n": 3})
        self.s.scr = PERMISSION_SCREEN.format(cmd="SomeTool(3)")
        self.assertFalse(self.s.answer("p1", "allow"))

    def test_multi_question_answers(self):
        qs = [{"question": "Pick a colour", "options": [{"label": "Red"}, {"label": "Green"}, {"label": "Blue"}]},
              {"question": "Pick toppings", "multiSelect": True,
               "options": [{"label": "Cheese"}, {"label": "Ham"}, {"label": "Olives"}]}]
        self.open("q1", "AskUserQuestion", {"questions": qs}, synthetic=False)
        self.s.scr = "Pick a colour\n❯ 1. Red\n  2. Green\n  3. Blue\n  4. Type something.\nReview your answers"
        self.s.answer("q1", {"Pick a colour": "Blue", "Pick toppings": "Cheese, Olives"})
        self.assertEqual(self.s.keys, ["3", "1", "3", "Tab", "Enter"])

    FRUIT = [{"question": "Pick a fruit for the test", "options": [{"label": "Apple"}, {"label": "Pear"}]},
             {"question": "Pick toppings for the test", "multiSelect": True,
              "options": [{"label": "Salt"}, {"label": "Sugar"}]}]

    def test_question_dialog_on_a_real_screen_is_answered(self):
        self.open("q1", "AskUserQuestion", {"questions": self.FRUIT}, synthetic=False)
        self.s.scr = screen_file("screen-question.txt")
        self.assertTrue(self.s.answer("q1", {"Pick a fruit for the test": "Pear",
                                             "Pick toppings for the test": "Sugar"}))
        self.assertEqual(self.s.keys, ["2", "2", "Tab"])

    def test_question_echoed_above_a_permission_dialog_is_not_answered(self):
        self.open("q1", "AskUserQuestion", {"questions": self.FRUIT}, synthetic=False)
        self.s.scr = "● Pick a fruit for the test\n  ⎿ Apple\n" + PERMISSION_SCREEN.format(cmd="rm -rf build")
        self.assertFalse(self.s.answer("q1", {"Pick a fruit for the test": "Apple"}))
        self.assertEqual(self.s.keys, [])

    def test_yes_no_question_does_not_match_a_permission_dialog(self):
        self.open("q1", "AskUserQuestion", {"questions": [
            {"question": "Do you want to proceed?", "options": [{"label": "Yes"}, {"label": "No"}]}]}, synthetic=False)
        self.s.scr = PERMISSION_SCREEN.format(cmd="rm -rf build")
        self.assertFalse(self.s.answer("q1", "Yes"))
        self.assertEqual(self.s.keys, [])

    def test_synthetic_dialog_binds_to_its_tool_use_and_closes_on_the_result(self):
        self.s._ensure_turn()
        self.open("perm-1", "Write", {"file_path": "/tmp/toy/a.txt", "content": "x"})
        self.s._entry(assistant({"type": "tool_use", "id": "t1", "name": "Write",
                                 "input": {"file_path": "/tmp/toy/a.txt", "content": "x"}}))
        self.assertEqual(self.s.dialogs["perm-1"]["tool_use_id"], "t1")
        self.s._entry(result("t1"))
        self.assertIn({"kind": "dialog_closed", "id": "perm-1", "by": "elsewhere"}, self.s.events)

    def test_dialog_closes_at_turn_end(self):
        self.s._ensure_turn()
        self.open("perm-1", "Bash", {"command": "ls"})
        self.s._entry({"type": "system", "subtype": "turn_duration"})
        self.assertEqual(self.s.kinds()[-2:], ["dialog_closed", "turn_end"])

    def test_tool_use_written_after_the_decision_names_its_dialog(self):
        self.s._ensure_turn()
        self.open("perm-1", "Bash", {"command": "echo hi > a.txt"})
        self.s.scr = PERMISSION_SCREEN.format(cmd="echo hi > a.txt")
        self.s.answer("perm-1", "allow")
        self.s._entry(assistant({"type": "tool_use", "id": "t1", "name": "Bash",
                                 "input": {"command": "echo hi > a.txt"}}))
        self.assertEqual(self.s.events[-1]["dialog_id"], "perm-1")
        self.assertEqual(self.s.unbound, {})

    def test_dialog_from_the_spool_carries_the_tool_use_read_before_it(self):
        self.addCleanup(os.environ.__setitem__, "XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR", ""))
        os.environ["XDG_RUNTIME_DIR"] = self.dir
        self.s._ensure_turn()
        self.s._entry(assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}))
        threading.Thread(target=self.s._tail_spool, daemon=True).start()
        spool = os.path.join(self.dir, "t3-bridge", "sid-1.jsonl")
        self.assertTrue(wait_for(lambda: os.path.exists(spool)))
        time.sleep(0.3)
        with open(spool, "a") as f:
            f.write(json.dumps({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                                "tool_input": {"command": "ls"}, "tool_use_id": None}) + "\n")
        self.assertTrue(wait_for(lambda: self.s.events[-1]["kind"] == "dialog_open"))
        self.assertEqual(self.s.events[-1]["tool_use_id"], "t1")
        self.assertEqual(oct(os.stat(spool).st_mode & 0o777), "0o600")


class Subagents(Base):
    def agent_files(self, aid, tid, shape, lines, finished=False):
        d = self.s._subagent_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"agent-{aid}.meta.json"), "w") as f:
            json.dump({"toolUseId": tid, "description": "count files", "agentType": "general-purpose",
                       "requestShape": shape, "finished": finished}, f)
        with open(os.path.join(d, f"agent-{aid}.jsonl"), "w") as f:
            for e in lines:
                f.write(json.dumps(e) + "\n")

    def launch(self, tid):
        self.s._entry(prompt("go"))
        self.s._entry(assistant({"type": "tool_use", "id": tid, "name": "Agent",
                                 "input": {"description": "count files", "prompt": "run ls"}}))

    def test_foreground_agent(self):
        self.launch("ta")
        self.agent_files("a1", "ta", "foreground", [
            {"type": "user", "message": {"role": "user", "content": "run ls"}},
            assistant({"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": "ls"}}),
            result("c1", "a\nb")])
        self.s._poll_subagents()
        self.s._entry(result("ta", [{"type": "text", "text": "2 files"}], {"status": "completed", "agentId": "a1"}))
        ev = [e for e in self.s.events if e["kind"] not in ("turn_start",)]
        self.assertEqual([e["kind"] for e in ev],
                         ["tool_use", "subagent_start", "tool_use", "tool_result", "subagent_end", "tool_result"])
        self.assertEqual(ev[1]["tool_use_id"], "ta")
        self.assertFalse(ev[1]["background"])
        self.assertEqual([ev[2]["parent_tool_use_id"], ev[3]["parent_tool_use_id"]], ["ta", "ta"])
        self.assertEqual((ev[4]["status"], ev[4]["summary"]), ("completed", "2 files"))
        self.assertIsNone(ev[5]["parent_tool_use_id"])

    def test_background_agent_ends_on_an_attachment_notification(self):
        self.launch("tb")
        self.agent_files("b1", "tb", "background", [assistant({"type": "text", "text": "2 files"})])
        self.s._entry(result("tb", "launched", {"status": "async_launched", "agentId": "b1"}))
        self.s._poll_subagents()
        self.assertEqual(self.s.kinds()[-2:], ["subagent_start", "text"])
        self.assertTrue(self.s.events[-2]["background"])
        self.s._entry({"type": "attachment", "attachment": {"prompt": "<task-notification><task-id>b1</task-id>"
                       "<tool-use-id>tb</tool-use-id><status>completed</status><summary>Agent done</summary>"
                       "</task-notification>"}})
        self.assertEqual(self.s.events[-1]["kind"], "subagent_end")
        self.assertEqual(self.s.events[-1]["summary"], "Agent done")

    def test_background_agent_falls_back_to_meta_finished(self):
        self.launch("tc")
        self.agent_files("c1", "tc", "background", [assistant({"type": "text", "text": "3 files"})], finished=True)
        self.s._poll_subagents()
        self.s.agents["c1"]["finished_at"] = time.time() - 10
        self.s._poll_subagents()
        self.assertEqual(self.s.events[-1]["kind"], "subagent_end")
        self.assertEqual(self.s.events[-1]["summary"], "3 files")   # no summary given: the child's last text

    def test_agents_launched_before_attach_are_ignored(self):
        self.agent_files("old", "t-old", "foreground", [assistant({"type": "text", "text": "x"})])
        self.s._poll_subagents()
        self.assertEqual(self.s.events, [])


class Replay(Base):
    def write(self, *entries):
        p = self.s.transcript_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return os.path.getsize(p)

    def run_tail(self, since, n):
        self.s.state_key = "t3-toy"
        t = threading.Thread(target=self.s._tail_transcript, args=(since,), daemon=True)
        t.start()
        deadline = time.time() + 5
        while len(self.s.events) < n and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        self.s.stopped = True
        t.join(2)

    def test_replays_from_the_saved_offset(self):
        seen = self.write(prompt("one"), assistant({"type": "text", "text": "1"}),
                          {"type": "system", "subtype": "turn_duration"})
        self.s.state_key = "t3-toy"
        self.s.save_offset(seen)
        self.write(prompt("two"), assistant({"type": "text", "text": "2"}),
                   {"type": "system", "subtype": "turn_duration"})
        self.run_tail("saved", 3)
        self.assertEqual(self.s.kinds(), ["turn_start", "text", "turn_end"])
        self.assertEqual(self.s.events[0]["text"], "two")
        self.assertTrue(all(e["replay"] for e in self.s.events))
        self.assertEqual(self.s.events[-1]["offset"], os.path.getsize(self.s.transcript_path()))

    def test_no_saved_offset_starts_at_the_end(self):
        self.write(prompt("one"), {"type": "system", "subtype": "turn_duration"})
        self.run_tail("saved", 1)
        self.assertEqual(self.s.events, [])

    def test_offset_of_an_earlier_session_is_not_replayed(self):
        self.write(prompt("one"))
        os.makedirs(os.path.join(self.dir, "rolelive"))
        with open(os.path.join(self.dir, "rolelive", "t3-toy.json"), "w") as f:
            json.dump({"session_id": "older", "offset": 0}, f)
        self.run_tail("saved", 1)
        self.assertEqual(self.s.kinds(), ["notice"])

    def test_transcript_folder_replaces_every_non_alphanumeric_character(self):
        self.s.live["cwd"] = "/tmp/my_role dir+x"
        self.assertEqual(os.path.basename(os.path.dirname(self.s.transcript_path())), "-tmp-my-role-dir-x")
        self.s.live["cwd"] = "/tmp/" + "x" * 300   # claude hashes long names: found by session id
        p = os.path.join(self.dir, "projects", "-tmp-xx-hash", "sid-1.jsonl")
        os.makedirs(os.path.dirname(p))
        open(p, "w").close()
        self.assertEqual(self.s.transcript_path(), p)

    def test_state_file_is_private_and_atomic(self):
        self.s.state_key = "t3-toy"
        self.s.save_offset(42)
        self.assertEqual(self.s.saved_offset()["offset"], 42)
        self.assertEqual(os.stat(os.path.join(self.dir, "rolelive")).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(os.path.join(self.dir, "rolelive")), ["t3-toy.json"])


class Parsing(unittest.TestCase):
    def test_task_notification(self):
        n = rolelive.task_notification("<task-notification>\n<task-id>a1</task-id><tool-use-id>t1</tool-use-id>"
                                       "<status>failed</status><summary>boom</summary></task-notification>")
        self.assertEqual(n, {"task-id": "a1", "tool-use-id": "t1", "status": "failed", "summary": "boom"})

    def test_permission_hint(self):
        self.assertEqual(rolelive.permission_hint({"input": {"file_path": "/a/b/c.txt"}}), "c.txt")
        self.assertEqual(rolelive.permission_hint({"input": {"command": "ls  -la"}}), "ls-la")
        self.assertIsNone(rolelive.permission_hint({"input": {}}))


if __name__ == "__main__":
    unittest.main()
