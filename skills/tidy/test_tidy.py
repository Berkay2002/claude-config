#!/usr/bin/env python3.11
"""Classification and removal rules of tidy.py, on throwaway repos. Run: /usr/bin/python3.11 -m unittest -v test_tidy

Every test runs with HOME, git config and the claude CLI pointed into a temp dir (a fake `claude`), so nothing
here can reach the real machine's repos, sessions or `claude rm`.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tidy  # noqa: E402

FAKE = """#!/usr/bin/env python3.11
import os, sys
d = os.environ["FAKE_DIR"]
if os.path.exists(os.path.join(d, "fail")):
    sys.exit(1)
if sys.argv[1] == "agents":
    print(open(os.path.join(d, "agents.json")).read())
elif sys.argv[1] == "rm":
    open(os.path.join(d, "rm.log"), "a").write(" ".join(sys.argv[2:]) + "\\n")
"""
OLD = time.time() - 48 * 3600


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tidy-test-")
        self.env0 = dict(os.environ)
        h = os.path.join(self.tmp, "home")
        self.roots = os.path.join(h, "roots")
        os.makedirs(os.path.join(h, ".claude", "jobs"))
        os.makedirs(self.roots)
        gc = os.path.join(self.tmp, "gitconfig")
        with open(gc, "w") as f:
            f.write("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n"
                    "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")
        fake = os.path.join(self.tmp, "claude")
        with open(fake, "w") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        os.environ.update(HOME=h, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=gc, TIDY_CLAUDE=fake, FAKE_DIR=self.tmp)
        self.agents([])
        with open(os.path.join(h, ".claude", "tidy.json"), "w") as f:
            json.dump({"roots": [self.roots], "keep": ["pinned"], "min_age_hours": 24}, f)
        self.conf = tidy.load_conf()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.sh("git", "init", "-q", "--bare", self.origin)
        self.repo = os.path.join(self.roots, "repo")
        self.sh("git", "clone", "-q", self.origin, self.repo)
        self.commit(self.repo, "a")
        self.sh("git", "-C", self.repo, "push", "-q", "origin", "main")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        os.environ.clear()
        os.environ.update(self.env0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def sh(self, *cmd, cwd=None):
        return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout

    def commit(self, d, name, content="x"):
        with open(os.path.join(d, name), "w") as f:
            f.write(content)
        self.sh("git", "-C", d, "add", name)
        self.sh("git", "-C", d, "commit", "-q", "-m", name)
        return self.sh("git", "-C", d, "rev-parse", "HEAD").strip()

    def wt(self, name, *args, old=True):
        p = os.path.join(self.roots, name)
        self.sh("git", "-C", self.repo, "worktree", "add", "-q", *args, p, *([] if "-b" in args or "--detach" in args else ["--detach"]))
        if old:
            self.age(p)
        return p

    def age(self, p):
        admin = os.path.join(self.repo, ".git", "worktrees", os.path.basename(p))
        for f in ("HEAD", "index"):
            if os.path.exists(os.path.join(admin, f)):
                os.utime(os.path.join(admin, f), (OLD, OLD))

    def exclude(self, p, pattern):
        info = os.path.join(self.repo, ".git", "info", "exclude")  # shared by all worktrees of the repo
        os.makedirs(os.path.dirname(info), exist_ok=True)
        with open(info, "a") as f:
            f.write(pattern + "\n")

    def agents(self, rows):
        with open(os.path.join(self.tmp, "agents.json"), "w") as f:
            json.dump(rows, f)

    def job(self, sid, state, worktree=None, branch=None, updated="2020-01-01T00:00:00.000Z"):
        d = os.path.join(os.environ["HOME"], ".claude", "jobs", sid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump({"state": state, "worktreePath": worktree, "worktreeBranch": branch, "updatedAt": updated}, f)
        return {"id": sid, "cwd": self.repo, "kind": "background", "state": state, "name": sid, "startedAt": 0}

    def scan(self):
        wts, srows, _, errors = tidy.scan(self.conf)
        return {w["path"]: w for w in wts}, {s["id"]: s for s in srows}, errors

    def cls(self, path):
        w = self.scan()[0][path]
        return w["cls"], "; ".join(w["reasons"])

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tidy.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def registered(self):
        return [line[9:] for line in self.sh("git", "-C", self.repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


class Worktrees(Base):
    def test_clean_pushed_is_safe(self):
        p = self.wt("w")
        self.assertEqual(self.cls(p)[0], "safe")

    def test_main_working_tree_is_never_listed(self):
        self.assertNotIn(self.repo, self.scan()[0])

    def test_tracked_change_is_ask(self):
        p = self.wt("w")
        with open(os.path.join(p, "a"), "w") as f:
            f.write("changed")
        self.assertEqual(self.cls(p), ("ask", "1 tracked change(s)"))

    def test_untracked_is_ask_but_ignored_is_safe(self):
        p = self.wt("w")
        with open(os.path.join(p, "new.txt"), "w") as f:
            f.write("work")
        self.assertEqual(self.cls(p)[0], "ask")
        os.remove(os.path.join(p, "new.txt"))
        self.exclude(p, ".venv/")
        os.makedirs(os.path.join(p, ".venv"))
        with open(os.path.join(p, ".venv", "lib"), "w") as f:
            f.write("ignored")
        self.assertEqual(self.cls(p)[0], "safe")

    def test_unpushed_branch_commit_is_ask_until_pushed(self):
        p = self.wt("w", "-b", "feat")
        self.commit(p, "b")
        self.age(p)
        c, why = self.cls(p)
        self.assertEqual(c, "ask")
        self.assertIn("unpushed", why)
        self.sh("git", "-C", p, "push", "-q", "origin", "feat")
        self.assertEqual(self.cls(p)[0], "safe")

    def test_detached_unpushed_commit_is_ask(self):
        p = self.wt("w")
        self.commit(p, "b")
        self.age(p)
        c, why = self.cls(p)
        self.assertEqual(c, "ask")
        self.assertIn("detached", why)

    def test_patch_equivalent_to_default_branch_is_safe(self):
        p = self.wt("w", "-b", "feat")
        sha = self.commit(p, "b")
        self.sh("git", "-C", self.repo, "cherry-pick", sha)  # local main now has the same patch, unpushed
        self.age(p)
        self.assertEqual(self.cls(p)[0], "safe")

    def test_missing_directory_is_ask(self):
        p = self.wt("w")
        shutil.rmtree(p)
        self.assertEqual(self.cls(p)[0], "ask")

    def test_locked_is_keep_even_when_missing(self):
        p = self.wt("w")
        self.sh("git", "-C", self.repo, "worktree", "lock", "--reason", "busy", p)
        self.assertEqual(self.cls(p)[0], "keep")
        shutil.rmtree(p)
        self.assertEqual(self.cls(p)[0], "keep")

    def test_keep_list_by_basename(self):
        p = self.wt("pinned")
        self.assertEqual(self.cls(p), ("keep", "on keep list"))

    def test_process_cwd_inside_is_keep(self):
        p = self.wt("w")
        os.makedirs(os.path.join(p, "sub"))
        self.procs.append(subprocess.Popen(["sleep", "60"], cwd=os.path.join(p, "sub")))
        time.sleep(0.2)
        c, why = self.cls(p)
        self.assertEqual(c, "keep")
        self.assertIn("process cwd inside", why)

    def test_live_session_inside_is_keep_finished_is_not(self):
        p = self.wt("w")
        self.agents([{"pid": 1, "cwd": p, "kind": "interactive", "status": "idle", "name": "me", "sessionId": "x"}])
        self.assertEqual(self.cls(p)[0], "keep")
        self.agents([self.job("aaaa1111", "working", worktree=p)])
        self.assertEqual(self.cls(p)[0], "keep")
        self.agents([self.job("aaaa1111", "done", worktree=p)])
        self.assertEqual(self.cls(p)[0], "safe")

    def test_worktree_inside_live_job_dir_is_keep(self):
        jd = os.path.join(os.environ["HOME"], ".claude", "jobs", "bbbb2222", "tmp")
        os.makedirs(jd)
        p = os.path.join(jd, "w")
        self.sh("git", "-C", self.repo, "worktree", "add", "-q", "--detach", p)
        self.age(p)
        self.agents([self.job("bbbb2222", "blocked")])
        self.assertEqual(self.cls(p)[0], "keep")

    def test_recently_touched_is_keep(self):
        p = self.wt("w", old=False)
        c, why = self.cls(p)
        self.assertEqual(c, "keep")
        self.assertIn("touched", why)

    def test_nested_git_repo_is_ask(self):
        p = self.wt("w")
        self.exclude(p, "inner/")
        self.sh("git", "init", "-q", os.path.join(p, "inner"))
        c, why = self.cls(p)
        self.assertEqual(c, "ask")
        self.assertIn("nested git", why)

    def test_git_error_is_keep(self):
        p = self.wt("w")
        w = self.scan()[0][p]
        w["head"] = "0" * 40  # an object git cannot find
        tidy.classify(w, self.conf, tidy.proc_cwds(), [])
        self.assertEqual(w["cls"], "keep")

    def test_session_list_failure_is_keep(self):
        p = self.wt("w")
        open(os.path.join(self.tmp, "fail"), "w").close()
        c, why = self.cls(p)
        self.assertEqual(c, "keep")
        self.assertIn("session list unavailable", why)


class Sessions(Base):
    def test_session_actions(self):
        safe = self.wt("s-safe")
        dirty = self.wt("s-dirty")
        with open(os.path.join(dirty, "new"), "w") as f:
            f.write("work")
        self.sh("git", "-C", self.repo, "branch", "worktree-gone")
        self.sh("git", "-C", self.repo, "branch", "worktree-unpushed")
        tmpw = os.path.join(self.tmp, "u-wt")  # commit on the branch, then drop its worktree: branch alone holds it
        self.sh("git", "-C", self.repo, "worktree", "add", "-q", tmpw, "worktree-unpushed")
        self.commit(tmpw, "u")
        self.sh("git", "-C", self.repo, "worktree", "remove", tmpw)
        self.agents([
            self.job("r1", "done", worktree=safe),
            self.job("r2", "done", worktree=os.path.join(self.roots, "gone"), branch="worktree-gone"),
            self.job("a1", "done", worktree=dirty),
            self.job("a2", "done", branch="worktree-unpushed"),
            self.job("k1", "done", updated=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())),
            self.job("l1", "blocked"),
            self.job("l2", ""),
        ])
        s = self.scan()[1]
        self.assertEqual({k: v["action"] for k, v in s.items()},
                         {"r1": "rm", "r2": "rm", "a1": "ask", "a2": "ask", "k1": "keep", "l1": "live", "l2": "live"})
        self.assertIn("unpushed", s["a2"]["reason"])


class Apply(Base):
    def setup_mix(self):
        self.safe = self.wt("safe")
        os.makedirs(os.path.join(self.safe, "node_modules"))
        self.dirty = self.wt("dirty")
        with open(os.path.join(self.dirty, "new"), "w") as f:
            f.write("work")
        self.unpushed = self.wt("unpushed", "-b", "feat")
        self.commit(self.unpushed, "b")
        self.age(self.unpushed)
        self.missing = self.wt("missing")
        shutil.rmtree(self.missing)
        self.locked = self.wt("locked")
        self.sh("git", "-C", self.repo, "worktree", "lock", self.locked)
        self.agents([self.job("r1", "done"), self.job("a1", "done", worktree=self.dirty), self.job("l1", "working")])

    def test_apply_removes_only_safe_and_never_prunes_or_deletes_branches(self):
        self.setup_mix()
        branches = self.sh("git", "-C", self.repo, "branch", "--list")
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.safe))
        for p in (self.dirty, self.unpushed, self.locked):
            self.assertTrue(os.path.exists(p), p)
        self.assertIn(self.missing, self.registered())  # missing dir is Ask: no blanket prune
        self.assertNotIn(self.safe, self.registered())
        self.assertEqual(self.sh("git", "-C", self.repo, "branch", "--list"), branches)
        with open(os.path.join(self.tmp, "rm.log")) as f:
            self.assertEqual(f.read().split(), ["r1"])  # bare `claude rm <id>`, no discard/force flags
        with open(os.path.join(os.environ["HOME"], ".claude", "tidy.log")) as f:
            self.assertIn("removed " + self.safe, f.read())

    def test_auto_is_quiet_and_logs(self):
        self.setup_mix()
        code, out = self.run_main("--auto")
        self.assertEqual((code, out), (0, ""))
        with open(os.path.join(os.environ["HOME"], ".claude", "tidy.log")) as f:
            self.assertIn("== ", f.read())

    def test_apply_aborts_when_sessions_cannot_be_listed(self):
        self.setup_mix()
        open(os.path.join(self.tmp, "fail"), "w").close()
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("ABORT", out)
        self.assertTrue(os.path.exists(self.safe))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rm.log")))

    def test_apply_rechecks_before_removing(self):
        self.setup_mix()
        real_classify = tidy.classify
        calls = []

        def dirty_on_recheck(w, *a, **k):  # a file appears between the scan and the removal
            calls.append(w["path"])
            if w["path"] == self.safe and calls.count(self.safe) == 2:
                with open(os.path.join(self.safe, "late"), "w") as f:
                    f.write("x")
            return real_classify(w, *a, **k)
        tidy.classify = dirty_on_recheck
        try:
            code, out = self.run_main("--apply")
        finally:
            tidy.classify = real_classify
        self.assertTrue(os.path.exists(os.path.join(self.safe, "late")), out)
        self.assertIn("no longer safe", out)


class Backup(Base):
    def test_backup_saves_commits_and_changes_then_removes(self):
        p = self.wt("w")
        sha = self.commit(p, "b")
        with open(os.path.join(p, "a"), "w") as f:
            f.write("edited")
        with open(os.path.join(p, "brand-new"), "w") as f:
            f.write("untracked work")
        self.age(p)
        code, out = self.run_main("--backup", p)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(p))
        self.assertEqual(self.sh("git", "-C", self.repo, "rev-parse", "refs/tags/backup/wt-w").strip(), sha)
        patch = os.path.join(os.environ["HOME"], "worktree-backups", "repo-w.patch")
        with open(patch) as f:
            text = f.read()
        self.assertIn("brand-new", text)
        self.assertIn("edited", text)
        # a second worktree with the same name never overwrites the first backup
        p2 = self.wt("w")
        sha2 = self.commit(p2, "c")
        self.age(p2)
        code, out = self.run_main("--backup", p2)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sh("git", "-C", self.repo, "rev-parse", "refs/tags/backup/wt-w").strip(), sha)
        self.assertIn(sha2, self.sh("git", "-C", self.repo, "for-each-ref", "--format=%(objectname)", "refs/tags/backup/"))

    def test_backup_refuses_keep(self):
        p = self.wt("w", old=False)
        code, out = self.run_main("--backup", p)
        self.assertEqual(code, 1)
        self.assertIn("refused", out)
        self.assertTrue(os.path.exists(p))

    def test_backup_of_missing_dir_tags_head_and_unregisters(self):
        p = self.wt("w")
        sha = self.commit(p, "b")
        self.age(p)
        shutil.rmtree(p)
        code, out = self.run_main("--backup", p)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sh("git", "-C", self.repo, "rev-parse", "refs/tags/backup/wt-w").strip(), sha)
        self.assertNotIn(p, self.registered())


class Offline(Base):
    def test_delete_only_listed_rows_not_tied_to_live_sessions(self):
        rows = [{"id": "cse_dead", "title": "old"}, {"id": "cse_live", "title": "busy"}]
        calls = []
        saved = tidy.offline_rows, tidy.api, tidy.local_bridges
        tidy.offline_rows = lambda: [r for r in rows if r["id"] not in calls_ids(calls)]
        tidy.api = lambda m, p: (calls.append((m, p)), (200, {}))[1]
        tidy.local_bridges = lambda: {"cse_live": ("j1", "busy", True)}
        try:
            code, out = self.run_main("offline", "--delete", "cse_live", "cse_unknown", "https://claude.ai/code/session_dead")
        finally:
            tidy.offline_rows, tidy.api, tidy.local_bridges = saved
        self.assertEqual(calls, [("DELETE", "/v1/code/sessions/cse_dead")], out)
        self.assertIn("refused cse_live", out)
        self.assertIn("refused cse_unknown", out)
        self.assertEqual(code, 1)


def calls_ids(calls):
    return {p.split("/")[4] for _, p in calls}


if __name__ == "__main__":
    unittest.main()
