#!/usr/bin/python3.11
"""rolelive: attach to a live Claude Code session running in tmux and drive it as its user.

Core library (stdlib only) for front ends such as the T3 bridge or a web chat view. It never starts a claude process
on the session: input is typed into the session's tmux pane, output is read from its transcript jsonl, open dialogs
come from a recording hook (t3-bridge-hook) in the live session, and the session itself is found through
~/.claude/sessions/<pid>.json (name, sessionId, cwd, tmux pane, status).

    s = LiveSession.attach(role="probe-role", start_cmd="...")   # or session_id="..."
    s.on_event = print                                          # called from reader threads
    s.start()
    s.send("hello"); s.interrupt()
    s.set_model("<model id>", persist_ok=True); s.set_effort("low", persist_ok=True)   # see set_model
    s.answer(dialog_id, {"Pick a colour": "Blue"})               # AskUserQuestion; "allow"/"deny" for permissions

Events (dicts, key "kind"):
    turn_start  origin ("human" typed in tmux/RC/a front end, "peer", "task-notification", ...), text, by_us, peer
    text / thinking / tool_use   block (the API content block), message (id, model, usage; no content), uuid
    tool_result  block, tool (name of the tool_use it closes, by block.tool_use_id), tool_use_result (the CLI's
                 structured result), uuid
    turn_end    reason ("done" | "interrupted"), last_text
    dialog_open  id, dialog ("question" | "permission"), tool, input
    dialog_closed id, by ("us" | "elsewhere")
    session     session_id, pane, switched (bool)   (attach, /clear or restart)
    notice      text   (anything the core did not understand: never dropped silently)

CLI (prints events as JSON lines; stdin lines are user input, or :interrupt, :model M, :effort E,
:answer ID JSON-or-text, :allow ID, :deny ID, :status; ":model ! M" accepts that /model saves M as the default):
    rolelive.py ROLE [--start CMD]
"""
import glob, json, os, re, shlex, subprocess, sys, threading, time, uuid

HOME = os.path.expanduser("~")
SESSIONS = os.path.join(HOME, ".claude", "sessions")
# Transcript entry types that carry no turn content.
IGNORED_ENTRIES = {"attachment", "queue-operation", "api-request", "api-request-blob", "api-request-shape",
                   "custom-title", "session-owner", "atis-latch", "tools-used", "last-prompt", "summary",
                   "file-history-snapshot", "agent-name", "agent-setting", "permission-mode", "mode",
                   "bridge-session", "progress", "pr-link"}
IGNORED_SYSTEM = {"stop_hook_summary", "local_command", "informational", "api_error", "compact_boundary",
                  "away_summary", "bridge_status", "scheduled_task_fire"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def spool_dir():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "t3-bridge")


def find_session(role=None, session_id=None):
    """Newest live sessions/<pid>.json in tmux matching the role name or session ID (pid must be alive)."""
    best = None
    for f in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            d = json.load(open(f))
            os.kill(int(d["pid"]), 0)
        except Exception:
            continue
        if not d.get("tmux") or (role and d.get("name") != role) or (session_id and d.get("sessionId") != session_id):
            continue
        if best is None or d.get("updatedAt", 0) > best.get("updatedAt", 0):
            best = d
    return best


class LiveSession:
    def __init__(self, role=None, session_id=None, start_cmd=None):
        self.role, self.want_sid, self.start_cmd = role, session_id, start_cmd
        self.on_event = lambda ev: None
        self.live = None
        self.lock = threading.RLock()
        self.turn = None                 # {"by_us", "last_text", "started"}
        self.sent = []                   # texts sent by us whose prompt entry hasn't shown yet
        self.dialogs = {}                # id -> {"dialog", "tool", "input"}
        self.tool_names = {}             # tool_use id -> tool name, so tool_result can say which tool it closes
        self.last_line_at = 0.0
        self.stopped = False

    # ---- attach ----
    @classmethod
    def attach(cls, role=None, session_id=None, start_cmd=None, wait=60):
        s = cls(role, session_id, start_cmd)
        live = find_session(role, session_id)
        if live is None and start_cmd:
            # The role must outlive us: its own session, started through tmux by the role's start script.
            subprocess.run(shlex.split(start_cmd), start_new_session=True, stdin=subprocess.DEVNULL, timeout=wait)
            deadline = time.time() + wait
            while live is None and time.time() < deadline:
                time.sleep(0.5)
                live = find_session(role, session_id)
        if live is None:
            raise RuntimeError(f"no live tmux session for {role or session_id!r}")
        s.live = live
        return s

    @property
    def session_id(self):
        return self.live["sessionId"]

    @property
    def pane(self):
        return self.live["tmux"].rsplit(".", 1)[1]   # "sess:@win.%pane" -> "%pane"

    def status(self):
        return self.live.get("status")

    def transcript_path(self):
        return os.path.join(HOME, ".claude", "projects", re.sub(r"[/.]", "-", self.live["cwd"]),
                            self.session_id + ".jsonl")

    def start(self):
        self.emit({"kind": "session", "session_id": self.session_id, "pane": self.pane, "switched": False})
        threading.Thread(target=self._tail_transcript, daemon=True).start()
        threading.Thread(target=self._tail_spool, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def close(self):
        self.stopped = True   # never touches tmux or the session

    def emit(self, ev):
        try:
            self.on_event(ev)
        except Exception as e:  # a front-end bug must not kill the reader threads
            sys.stderr.write(f"rolelive: on_event failed: {e!r}\n")

    def notice(self, text):
        self.emit({"kind": "notice", "text": text})

    # ---- input (tmux) ----
    def _tmux(self, *args, data=None):
        r = subprocess.run(["tmux", *args], input=data, capture_output=True)
        if r.returncode:
            self.notice(f"tmux {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
        return r.returncode == 0

    def _paste(self, text, submit=True):
        # Named buffer + bracketed paste (-p): newlines don't submit early and the user's paste buffer is untouched.
        ok = self._tmux("load-buffer", "-b", "rolelive", "-", data=text.encode())
        ok = ok and self._tmux("paste-buffer", "-d", "-p", "-b", "rolelive", "-t", self.pane)
        if ok and submit:
            time.sleep(0.3)
            ok = self._tmux("send-keys", "-t", self.pane, "Enter")
        return ok

    def _keys(self, *keys):
        for k in keys:
            self._tmux("send-keys", "-t", self.pane, k)
            time.sleep(0.15)

    def send(self, text, files=()):
        """Real user input: typed into the pane. Files (images, PDFs) are attached as @path mentions."""
        if files:
            text = (text + "\n" if text else "") + " ".join("@" + p for p in files)
        with self.lock:
            self.sent.append(text)
        return self._paste(text)

    def interrupt(self):
        self._keys("Escape")

    # Claude Code's /model and /effort save the choice as the user's default for every new session
    # (~/.claude/settings.json "model" and modelSettings.<model>.effortLevel), so they are refused unless the
    # caller accepts that side effect.
    def set_model(self, model, persist_ok=False):
        if not persist_ok:
            self.notice(f"model change to {model} not applied: /model would also change the default model of every "
                        "new session (settings.json)")
            return False
        return self._paste(f"/model {model or 'default'}")

    def set_effort(self, effort, persist_ok=False):
        if effort not in EFFORTS:
            self.notice(f"unknown effort {effort!r}; not sent")
            return False
        if not persist_ok:
            self.notice(f"effort change to {effort} not applied: /effort would also change the default effort of "
                        "every new session (settings.json)")
            return False
        return self._paste(f"/effort {effort}")

    def answer(self, dialog_id, choice):
        """Answer an open dialog in the TUI. question: {question: label | "a, b" | free text}; permission: allow/deny."""
        with self.lock:
            d = self.dialogs.pop(dialog_id, None)
        if d is None:
            self.notice(f"no open dialog {dialog_id}")
            return False
        if choice in ("deny", "cancel", None):
            self._keys("Escape")
        elif d["dialog"] == "permission":
            self._keys("1")   # "Yes" row
        else:
            qs = d["input"].get("questions", [])
            if isinstance(choice, str) and len(qs) == 1:
                choice = {qs[0].get("question"): choice}
            for q in qs:
                ans = str(choice.get(q.get("question"), ""))
                labels = [o.get("label") for o in q.get("options", [])]
                picks = [a.strip() for a in ans.split(",")] if q.get("multiSelect") else [ans]
                if ans and all(p in labels for p in picks):
                    for p in picks:
                        self._keys(str(labels.index(p) + 1))   # single select: the number selects and submits
                    if q.get("multiSelect"):
                        self._keys("Tab")
                else:   # free text goes in the "Type something" row right after the options
                    self._keys(str(len(labels) + 1))
                    self._paste(ans, submit=False)
                    self._keys("Enter")
            if len(qs) > 1:
                self._keys("Enter")   # review screen: Submit
        self.emit({"kind": "dialog_closed", "id": dialog_id, "by": "us"})
        return True

    # ---- output: transcript ----
    def _turn_end(self, reason):
        t, self.turn = self.turn, None
        if t is not None:
            self.emit({"kind": "turn_end", "reason": reason, "last_text": t["last_text"]})

    def _turn_start(self, origin, text, by_us, peer=None):
        if self.turn is not None:
            self._turn_end("done")   # a new prompt means the previous turn is over (missed end marker)
        self.turn = {"by_us": by_us, "last_text": "", "started": time.time()}
        self.emit({"kind": "turn_start", "origin": origin, "text": text, "by_us": by_us, "peer": peer})

    def _ensure_turn(self):
        if self.turn is None:   # output without a visible prompt (e.g. attached mid-turn)
            self._turn_start("unknown", "", False)

    def _entry(self, e):
        et = e.get("type")
        if et in IGNORED_ENTRIES or e.get("isSidechain"):
            return
        if et == "system":
            st = e.get("subtype")
            if st == "turn_duration":     # the CLI's own end-of-turn marker
                self._turn_end("done")
            elif st not in IGNORED_SYSTEM:
                self.notice(f"transcript system entry {st!r} not understood")
            return
        if et not in ("user", "assistant"):
            self.notice(f"transcript entry type {et!r} not understood")
            return
        msg = e.get("message") or {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        if et == "assistant":
            self._ensure_turn()
            meta = {k: v for k, v in msg.items() if k != "content"}
            for b in blocks:
                kind = b.get("type")
                if kind == "text":
                    self.turn["last_text"] = b.get("text", "")
                if kind == "tool_use":
                    self.tool_names[b.get("id")] = b.get("name")
                if kind in ("text", "thinking", "tool_use"):
                    self.emit({"kind": kind, "block": b, "message": meta, "uuid": e.get("uuid")})
                elif kind != "redacted_thinking":
                    self.notice(f"assistant block type {kind!r} not understood")
            return
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            self._ensure_turn()
            for b in results:
                if self.dialogs.pop(b.get("tool_use_id"), None) is not None:
                    self.emit({"kind": "dialog_closed", "id": b.get("tool_use_id"), "by": "elsewhere"})
                self.emit({"kind": "tool_result", "block": b, "tool_use_result": e.get("toolUseResult"),
                           "tool": self.tool_names.pop(b.get("tool_use_id"), None),
                           "uuid": e.get("uuid")})
            return
        text = content if isinstance(content, str) else "\n".join(
            b.get("text", "") for b in blocks if b.get("type") == "text")
        if text.startswith("[Request interrupted by user"):
            self._turn_end("interrupted")
            return
        origin = e.get("origin") or {}
        if not e.get("turnPosition") or (e.get("isMeta") and origin.get("kind") in (None, "human")):
            return   # skill bodies, local command output, /model echoes, ...
        kind = origin.get("kind") or e.get("turnOrigin") or "human"
        body = origin.get("body") or text
        by_us = False
        if kind == "human" and self.sent:
            # Our pastes come back as typed prompts, in order. A tmux/RC prompt racing ours is told apart by text.
            norm = lambda s: " ".join(s.split())
            for i, s in enumerate(self.sent):
                if norm(s)[:60] in norm(body):   # prefix: the CLI rewrites @file mentions and pasted images
                    del self.sent[:i + 1]
                    by_us = True
                    break
        self._turn_start(kind, body, by_us, origin.get("name"))

    def _tail_transcript(self):
        path, f, buf = None, None, ""
        first = True
        while not self.stopped:
            p = self.transcript_path()
            if p != path:
                path, f, buf = p, None, ""
            if f is None and os.path.exists(p):
                f = open(p)
                if first:
                    f.seek(0, 2)   # history from before the attach is not replayed
                first = False      # a transcript that appears later (new session) is read from its start
            elif f is None:
                first = False
            chunk = f.read() if f else ""
            if not chunk:
                time.sleep(0.2)
                continue
            buf += chunk
            *lines, buf = buf.split("\n")
            for ln in lines:
                if not ln.strip():
                    continue
                self.last_line_at = time.time()
                with self.lock:
                    try:
                        self._entry(json.loads(ln))
                    except Exception as ex:
                        self.notice(f"transcript line not handled: {ex!r}")

    # ---- output: dialogs (hook spool) ----
    def _tail_spool(self):
        os.makedirs(spool_dir(), mode=0o700, exist_ok=True)
        path, f = None, None
        while not self.stopped:
            p = os.path.join(spool_dir(), self.session_id + ".jsonl")
            if p != path:
                if not os.path.exists(p):
                    open(p, "a").close()
                path, f = p, open(p)
                f.seek(0, 2)   # only dialogs that open from now on
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                self.notice("unreadable hook spool line")
                continue
            tool, hook = ev.get("tool_name"), ev.get("hook_event_name")
            if hook == "PermissionRequest" and tool == "AskUserQuestion":
                continue   # PreToolUse already announced it
            did = ev.get("tool_use_id") or "perm-" + uuid.uuid4().hex[:12]
            d = {"dialog": "question" if tool == "AskUserQuestion" else "permission", "tool": tool,
                 "input": ev.get("tool_input") or {}}
            with self.lock:
                self.dialogs[did] = d
                self._ensure_turn()
                self.emit({"kind": "dialog_open", "id": did, **d})

    # ---- session watch: /clear, restart, idle fallback for turn end ----
    def _watch(self):
        gone = False
        while not self.stopped:
            time.sleep(1.0)
            live = find_session(self.role, None if self.role else self.want_sid)
            if live is None:
                if not gone:
                    self.notice("the live session is gone (exited?); waiting for it to come back")
                gone = True
                continue
            gone = False
            with self.lock:
                switched = live["sessionId"] != self.live["sessionId"] or live["tmux"] != self.live["tmux"]
                self.live = live
                if switched:
                    self._turn_end("done")
                    self.emit({"kind": "session", "session_id": self.session_id, "pane": self.pane,
                               "switched": True})
                # Fallback when no turn_duration marker came: idle with no new lines for a while.
                if (self.turn is not None and live.get("status") == "idle" and not self.dialogs
                        and time.time() - self.last_line_at > 15):
                    self._turn_end("done")


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Print a live role session's events; stdin lines are input.")
    ap.add_argument("role")
    ap.add_argument("--start", help="command that starts the role in tmux when it isn't running")
    a = ap.parse_args()
    s = LiveSession.attach(role=a.role, start_cmd=a.start)
    out = threading.Lock()

    def show(ev):
        with out:
            print(json.dumps(ev, separators=(",", ":"))[:2000], flush=True)
    s.on_event = show
    s.start()
    for line in sys.stdin:
        line = line.rstrip("\n")
        cmd, _, rest = line.partition(" ")
        if cmd == ":interrupt":
            s.interrupt()
        elif cmd in (":model", ":effort"):   # ":model ! M" accepts that it changes the saved default
            (s.set_model if cmd == ":model" else s.set_effort)(rest.removeprefix("! "), rest.startswith("! "))
        elif cmd in (":allow", ":deny"):
            s.answer(rest, cmd[1:])
        elif cmd == ":answer":
            did, _, choice = rest.partition(" ")
            try:
                choice = json.loads(choice)
            except ValueError:
                pass
            s.answer(did, choice)
        elif cmd == ":status":
            show({"kind": "status", "status": s.status(), "session_id": s.session_id, "pane": s.pane})
        elif line:
            s.send(line)
    s.close()


if __name__ == "__main__":
    _cli()
