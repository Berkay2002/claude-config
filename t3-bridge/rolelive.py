#!/usr/bin/env python3.11
"""rolelive: attach to a live Claude Code session running in tmux and drive it as its user.

Core library (stdlib only, no front-end code) for front ends such as the T3 bridge or a web chat view. It never starts
a claude process on the session: input is typed into the session's tmux pane, output is read from its transcript jsonl
(and the subagent transcripts next to it), open dialogs come from a recording hook (t3-bridge-hook) in the live
session, and the session itself is found through <claude dir>/sessions/<pid>.json (name, sessionId, cwd, tmux pane).

    s = LiveSession.attach(role="probe-role", start_cmd="...")   # or session_id="..."
    s.on_event = print                                          # called from reader threads
    s.start()                     # start(since="saved", state_key="t3"): replay what this consumer has not seen yet
    s.send("hello"); s.interrupt()
    s.answer(dialog_id, {"Pick a colour": "Blue"})               # AskUserQuestion; "allow"/"deny" for permissions

Events (dicts, key "kind"; names and fields are stable, front ends map them):
    turn_start   origin ("human" typed in tmux/RC/a front end, "peer", "task-notification", ...), text, by_us, peer
    text / thinking / tool_use   block (the API content block), message (id, model, usage; no content), uuid;
                 tool_use also has dialog_id when it is the call of a dialog announced before its tool_use was read
    tool_result  block, tool (name of the tool_use it closes), tool_use_result (the CLI's structured result), uuid
    turn_end     reason ("done" | "interrupted"), last_text
    subagent_start  id (agent id), tool_use_id (the Agent call), description, prompt, agent_type, background
    subagent_end    id, tool_use_id, status ("completed" | "failed" | "stopped" | ...), summary
    dialog_open  id, dialog ("question" | "permission"), tool, input, tool_use_id (the tool_use it belongs to when
                 already read, else None: a permission dialog's tool_use may only be written once it is decided)
    dialog_closed id, by ("us" | "elsewhere")
    send_failed  text, reason ("draft": the pane's input line held someone's draft, "dialog": a dialog was open,
                 "timeout": the text never showed up as a prompt, "tmux": tmux refused)
    session      session_id, pane, switched (bool)   (attach, /clear or restart)
    notice       text   (anything the core did not understand: never dropped silently)
  Transcript events also carry parent_tool_use_id (None, or the Agent call a subagent's event belongs to), offset
  (main transcript byte offset after the line; save_offset() records it) and replay (True for lines written before
  start(since="saved") attached).

CLI (prints events as JSON lines; stdin lines are user input, or :interrupt, :answer ID JSON-or-text, :allow ID,
:deny ID, :model M [E], :effort E, :status):
    rolelive.py ROLE [--start CMD] [--tmux-session NAME]
"""
import glob, json, os, re, shlex, subprocess, sys, threading, time, uuid

# Transcript entry types that carry no turn content.
IGNORED_ENTRIES = {"queue-operation", "api-request", "api-request-blob", "api-request-shape",
                   "custom-title", "session-owner", "atis-latch", "tools-used", "last-prompt", "summary",
                   "file-history-snapshot", "file-history-delta", "agent-name", "agent-setting", "permission-mode",
                   "mode", "bridge-session", "progress", "pr-link"}
IGNORED_SYSTEM = {"stop_hook_summary", "local_command", "informational", "api_error", "compact_boundary",
                  "away_summary", "bridge_status", "scheduled_task_fire"}
AGENT_TOOLS = {"Agent", "Task"}
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
EFFORTS = set(EFFORT_ORDER)
SEND_TIMEOUT = 45          # seconds a sent text may stay unseen while the session is idle
DRAFT_WAIT = 5             # seconds to wait for someone's draft in the pane to clear before refusing a send
SGR = re.compile(r"\x1b\[[0-9;:]*m")
DIM = re.compile(r"\x1b\[2m.*?(?=\x1b\[(?:0|22)?m|$)", re.S)   # placeholder and suggestion text is dim


def default_claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def spool_dir():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "t3-bridge")


def find_session(claude_dir, role=None, session_id=None, tmux_session=None):
    """Newest live sessions/<pid>.json in tmux matching the role name or session ID (pid must be alive)."""
    best = None
    for f in glob.glob(os.path.join(claude_dir, "sessions", "*.json")):
        try:
            d = read_json(f)
            os.kill(int(d["pid"]), 0)
        except Exception:
            continue
        if not d.get("tmux") or (role and d.get("name") != role) or (session_id and d.get("sessionId") != session_id):
            continue
        if tmux_session and d["tmux"].split(":", 1)[0] != tmux_session:
            continue
        if best is None or d.get("updatedAt", 0) > best.get("updatedAt", 0):
            best = d
    return best


def input_line(screen):
    """The prompt box of a `tmux capture-pane -p -e` screen: "" when empty, the typed text, or None when there is no
    prompt box (a dialog has the screen). The box is a "❯\\u00a0" line between two rule lines; history lines use a
    plain space. Dim text (placeholders, suggestions) does not count as typed."""
    lines = screen.split("\n")
    plain = [SGR.sub("", ln) for ln in lines]
    for i in range(len(lines) - 1, 0, -1):
        if plain[i].startswith("❯ ") or plain[i].rstrip() == "❯":
            if not plain[i - 1].lstrip().startswith("─"):
                continue
            body = []
            for j in range(i, len(lines)):
                if j > i and plain[j].lstrip().startswith("─"):
                    return "\n".join(body).replace(" ", " ").strip()
                body.append(SGR.sub("", DIM.sub("", lines[j])).removeprefix("❯"))
            return None
    return None


def read_json(path):
    with open(path) as f:
        return json.load(f)


def strings(o):
    """Every string inside a JSON value."""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


def permission_hint(d):
    """What identifies a permission dialog on screen (whitespace removed): the command, the file name, the URL, or
    the longest string the tool got. None when the input has nothing usable."""
    inp = d.get("input") or {}
    path = inp.get("file_path") or inp.get("notebook_path")
    hint = (os.path.basename(path) if path else inp.get("command") or inp.get("url")
            or max((v for v in inp.values() if isinstance(v, str)), key=len, default=""))
    hint = re.sub(r"\s+", "", str(hint))[:200]   # shortcut: the TUI may shorten very long commands; 200 chars held
    return hint or None


def task_notification(text):
    """Fields of a <task-notification> prompt: task-id, tool-use-id, status, summary."""
    return {k: (m.group(1).strip() if (m := re.search(f"<{k}>(.*?)</{k}>", text, re.S)) else None)
            for k in ("task-id", "tool-use-id", "status", "summary")}


class LiveSession:
    def __init__(self, role=None, session_id=None, start_cmd=None, claude_dir=None, tmux_session=None):
        self.role, self.want_sid, self.start_cmd = role, session_id, start_cmd
        self.claude_dir = claude_dir or default_claude_dir()
        self.tmux_session = tmux_session
        self.on_event = lambda ev: None
        self.live = None
        self.lock = threading.RLock()
        self.turn = None                 # {"by_us", "last_text", "started"}
        self.sent = []                   # [text, sent_at] sent by us whose prompt entry hasn't shown yet
        self.dialogs = {}                # id -> {"dialog", "tool", "input", "synthetic", "tool_use_id"}
        self.unbound = {}                # answered dialog id -> (tool, input) until its tool_use shows up
        self.tool_names = {}             # tool_use id -> tool name, so tool_result can say which tool it closes
        self.tool_inputs = {}            # tool_use id -> (name, input) until its tool_result
        self.launches = {}               # Agent tool_use id -> its input (subagents start only for seen launches)
        self.agents = {}                 # agent id -> {"tool_use_id", "path", "f", "buf", "done", "background"}
        self.agent_text = {}             # Agent tool_use id -> the subagent's last text
        self.last_line_at = 0.0
        self.idle_since = None
        self.state_key = None
        self.stopped = False

    # ---- attach ----
    @classmethod
    def attach(cls, role=None, session_id=None, start_cmd=None, wait=60, claude_dir=None, tmux_session=None):
        s = cls(role, session_id, start_cmd, claude_dir, tmux_session)
        live = s._find()
        if live is None and start_cmd:
            # The role must outlive us: its own session, started through tmux by the role's start script.
            subprocess.run(shlex.split(start_cmd), start_new_session=True, stdin=subprocess.DEVNULL, timeout=wait)
            deadline = time.time() + wait
            while live is None and time.time() < deadline:
                time.sleep(0.5)
                live = s._find()
        if live is None:
            raise RuntimeError(f"no live tmux session for {role or session_id!r}")
        s.live = live
        return s

    def _find(self):
        return find_session(self.claude_dir, self.role, None if self.role else self.want_sid, self.tmux_session)

    @property
    def session_id(self):
        return self.live["sessionId"]

    @property
    def pane(self):
        return self.live["tmux"].rsplit(".", 1)[1]   # "sess:@win.%pane" -> "%pane"

    def status(self):
        return self.live.get("status")

    def transcript_path(self):
        return os.path.join(self.claude_dir, "projects", re.sub(r"[/.]", "-", self.live["cwd"]),
                            self.session_id + ".jsonl")

    def start(self, since=None, state_key=None):
        """since=None reads from now on; since="saved" first replays what state_key's consumer has not seen."""
        self.state_key = state_key
        self.emit({"kind": "session", "session_id": self.session_id, "pane": self.pane, "switched": False})
        threading.Thread(target=self._tail_transcript, args=(since,), daemon=True).start()
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

    # ---- seen offset (per consumer and session) ----
    def _state_path(self):
        return os.path.join(self.claude_dir, "rolelive", f"{self.state_key}.json")

    def saved_offset(self):
        try:
            d = read_json(self._state_path())
        except (OSError, ValueError):
            return None
        return d if self.state_key else None

    def save_offset(self, offset):
        """Record that the consumer has delivered everything up to this main transcript offset (atomic)."""
        if not self.state_key or offset is None:
            return
        p = self._state_path()
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"session_id": self.session_id, "offset": offset, "at": time.time()}, f)
        os.replace(tmp, p)

    # ---- input (tmux) ----
    def _tmux(self, *args, data=None):
        r = subprocess.run(["tmux", *args], input=data, capture_output=True)
        if r.returncode:
            self.notice(f"tmux {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
        return r.returncode == 0

    def screen(self, escapes=False):
        r = subprocess.run(["tmux", "capture-pane", "-p", *(["-e"] if escapes else []), "-t", self.pane],
                           capture_output=True)
        return r.stdout.decode(errors="replace") if r.returncode == 0 else ""

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

    def _ready_to_type(self):
        """Wait briefly for an empty prompt box. Returns None when ready, else the send_failed reason."""
        deadline = time.time() + DRAFT_WAIT
        empty = 0
        while True:
            with self.lock:
                dialog = bool(self.dialogs)
            box = input_line(self.screen(escapes=True))
            empty = empty + 1 if not dialog and box == "" else 0
            if empty == 2:   # twice, 0.3 s apart: keystrokes still on their way to the screen show up
                return None
            if empty:
                time.sleep(0.3)
                continue
            if time.time() >= deadline:
                return "dialog" if dialog or box is None else "draft"
            time.sleep(0.5)

    def send(self, text, files=()):
        """Real user input: typed into the pane. Files (images, PDFs) are attached as @path mentions.
        Never merges into someone's half-typed draft or answers an open dialog: emits send_failed instead."""
        if files:
            text = (text + "\n" if text else "") + " ".join("@" + p for p in files)
        reason = self._ready_to_type()
        if reason is None:
            with self.lock:
                self.sent.append([text, time.time()])
            if self._paste(text):
                return True
            with self.lock:
                self.sent = [s for s in self.sent if s[0] is not text]
            reason = "tmux"
        self.emit({"kind": "send_failed", "text": text, "reason": reason})
        return False

    def interrupt(self):
        self._keys("Escape")

    # A typed "/model M" or "/effort E" also saves the choice as the default for every new session (settings.json
    # "model", modelSettings.<model>.effortLevel). The /model picker's "s" key switches model and effort for this
    # session only, so set_model drives the picker and checks every screen before it presses a key.
    def _wait_screen(self, pattern, timeout=4.0, after="❯ /model"):
        """The screen once pattern shows below the last `after` line (older history doesn't count), else None."""
        deadline = time.time() + timeout
        while True:
            scr = self.screen().split(after)[-1]
            if re.search(pattern, scr):
                return scr
            if time.time() >= deadline:
                return None
            time.sleep(0.25)

    def set_model(self, model=None, effort=None):
        """Switch the live session's model (a family alias or id, e.g. "haiku" or a full model id) and/or effort
        for this session only. Refuses with a notice when anything on screen is not as expected."""
        if effort is not None and effort not in EFFORTS:
            self.notice(f"unknown effort {effort!r}; not applied")
            return False
        reason = "a turn is running" if self.status() != "idle" or self.turn else self._ready_to_type()
        if reason is not None:
            self.notice(f"model/effort change not applied: the session is busy ({reason}); try again when idle")
            return False
        settings = os.path.join(self.claude_dir, "settings.json")
        before = open(settings, "rb").read() if os.path.exists(settings) else None
        try:
            return self._picker(model, effort)
        finally:
            after = open(settings, "rb").read() if os.path.exists(settings) else None
            if after != before and before is not None:   # never leave a saved default behind
                with open(settings, "wb") as f:
                    f.write(before)
                self.notice("the model picker changed settings.json; restored it")

    def _picker(self, model, effort):
        def fail(why):
            if "Select model" in self.screen():
                self._keys("Escape")   # only ever into the picker: at the prompt it would interrupt a turn
            self.notice(f"model/effort change not applied: {why}")
            return False
        self._paste("/model")
        scr = self._wait_screen(r"Select model[\s\S]*s to use this session only")
        if scr is None:
            return fail("the /model picker did not show its 'this session only' key")
        rows = re.findall(r"^\s*(❯)?\s*(\d+)\.\s+(\S+)(.*)$", scr.split("Select model", 1)[1], re.M)
        cur = next((i for i, r in enumerate(rows) if r[0]), None)
        target = cur
        if model:
            words = re.split(r"[^a-z]+", model.lower())   # a picker row name ("Haiku") is a word of the alias or id
            ver = re.search(r"(\d+)[-.](\d+)", model)
            target = next((i for i, r in enumerate(rows) if r[2].lower() in words), None)
            if target is not None and ver and f"{ver.group(1)}.{ver.group(2)}" not in rows[target][3]:
                return fail(f"{model} is not the version the picker offers ({' '.join(rows[target][3].split())})")
        if cur is None or target is None:
            return fail(f"no picker row for {model!r}")
        self._keys(*(["Down"] * (target - cur) if target > cur else ["Up"] * (cur - target)))
        now = None
        if effort:
            for _ in range(len(EFFORT_ORDER)):
                m = re.search(r"(\w+) effort\b.*to adjust", self.screen())
                now = m.group(1).lower() if m else None
                if now == effort or now not in EFFORT_ORDER:
                    break
                self._keys("Right" if EFFORT_ORDER.index(effort) > EFFORT_ORDER.index(now) else "Left")
            if now != effort:
                return fail(f"the picker did not reach {effort} effort")
        scr = self.screen()
        sel = re.search(r"^\s*❯\s*\d+\.\s+(\S+)", scr.split("Select model", 1)[-1], re.M)
        if not sel or sel.group(1) != rows[target][2] or "s to use this session only" not in scr:
            return fail("the picker is not where expected")
        self._keys("s")
        scr = self._wait_screen(r"for this session only|Switch model\?|saved as your default")
        if scr and "Switch model?" in scr and re.search(r"❯ 1\. Yes, switch", scr):
            self._keys("Enter")   # the cache warning; the pick stays session-only
            scr = self._wait_screen(r"for this session only|saved as your default")
        done = re.search(r"Set model to (\S+).* for this session only(?: with (\w+) effort)?", scr or "")
        if not done or done.group(1) != rows[target][2] or (effort and (done.group(2) or "").lower() != effort):
            return fail("no 'for this session only' confirmation")
        return True

    def _on_screen(self, d):
        """The dialog d is what the pane shows now: never press keys into the wrong dialog. Fails closed: a permission
        dialog needs an identifying hint from its input on screen, and no other open dialog may share that hint."""
        scr = self.screen()
        flat = " ".join(scr.split())
        if d["dialog"] == "question":
            qs = d["input"].get("questions") or [{}]
            return scr if " ".join(str(qs[0].get("question", "")).split())[:40] in flat else None
        lines = scr.split("\n")
        yes = [i for i, ln in enumerate(lines) if re.match(r"\s*❯ 1\. Yes", ln)]
        if not yes:
            return None
        top = max([i for i, ln in enumerate(lines[:yes[-1]]) if ln.lstrip().startswith("─")], default=-1)
        region = re.sub(r"\s+", "", "".join(lines[top + 1:yes[-1]]))   # the dialog box only, not the history above
        hint = permission_hint(d)
        with self.lock:
            others = [permission_hint(o) for o in self.dialogs.values() if o is not d]
        twins = [h for h in others if h and hint and (h in hint or hint in h)]
        return scr if hint and hint in region and not twins else None

    def answer(self, dialog_id, choice):
        """Answer an open dialog in the TUI. question: {question: label | "a, b" | free text}; permission: allow/deny."""
        with self.lock:
            d = self.dialogs.get(dialog_id)
        if d is None:
            self.notice(f"no open dialog {dialog_id}")
            return False
        scr = self._on_screen(d)
        if scr is None:
            self.notice(f"dialog {dialog_id} ({d['tool']}) is not on the session's screen; not answered from here")
            return False
        with self.lock:
            self.dialogs.pop(dialog_id, None)
            if d["synthetic"] and not d.get("tool_use_id"):
                self.unbound[dialog_id] = (d["tool"], d["input"])
        if d["dialog"] == "permission":
            if choice == "allow":
                self._keys("1")   # "Yes" once; never the "don't ask again" rows, which can write settings files
            else:
                no = re.findall(r"(\d)\. No\b", scr)
                self._keys(no[-1] if no else "Escape")
        elif choice in ("deny", "cancel", None):
            self._keys("Escape")
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
                        self._keys(str(labels.index(p) + 1))   # single select: the number selects and moves on
                    if q.get("multiSelect"):
                        self._keys("Tab")
                else:   # free text goes in the "Type something" row right after the options
                    self._keys(str(len(labels) + 1))
                    self._paste(ans, submit=False)
                    self._keys("Enter")
            time.sleep(0.3)
            if "Review your answers" in self.screen():
                self._keys("Enter")   # review screen: "1. Submit answers" is selected
        self.emit({"kind": "dialog_closed", "id": dialog_id, "by": "us"})
        return True

    # ---- output: transcript ----
    def _turn_end(self, reason):
        t, self.turn = self.turn, None
        self.unbound.clear()
        for did in list(self.dialogs):   # a dialog never outlives its turn
            self.dialogs.pop(did)
            self.emit({"kind": "dialog_closed", "id": did, "by": "elsewhere"})
        if t is not None:
            self.emit({"kind": "turn_end", "reason": reason, "last_text": t["last_text"], **self._meta})

    def _turn_start(self, origin, text, by_us, peer=None):
        if self.turn is not None:
            self._turn_end("done")   # a new prompt means the previous turn is over (missed end marker)
        self.turn = {"by_us": by_us, "last_text": "", "started": time.time()}
        self.emit({"kind": "turn_start", "origin": origin, "text": text, "by_us": by_us, "peer": peer, **self._meta})

    def _ensure_turn(self):
        if self.turn is None:   # output without a visible prompt (e.g. attached mid-turn)
            self._turn_start("unknown", "", False)

    def _blocks(self, e, parent):
        """Assistant blocks and tool results of one entry, main transcript or a subagent's."""
        msg = e.get("message") or {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        meta = {**self._meta, "parent_tool_use_id": parent}
        if e.get("type") == "assistant":
            m = {k: v for k, v in msg.items() if k != "content"}
            for b in blocks:
                kind = b.get("type")
                if kind == "text" and parent is None:
                    self.turn["last_text"] = b.get("text", "")
                elif kind == "text":
                    self.agent_text[parent] = b.get("text", "")   # the summary when the end says nothing
                extra = {}
                if kind == "tool_use":
                    tid, call = b.get("id"), (b.get("name"), b.get("input"))
                    self.tool_names[tid] = b.get("name")
                    if b.get("name") in AGENT_TOOLS:
                        self.launches[tid] = b.get("input") or {}
                    self.tool_inputs[tid] = call
                    self._bind_dialogs()
                    did = next((k for k, d in self.dialogs.items() if d["synthetic"] and d.get("tool_use_id") == tid),
                               None) or next((k for k, c in self.unbound.items() if c == call), None)
                    if did:
                        self.unbound.pop(did, None)
                        extra = {"dialog_id": did}
                if kind in ("text", "thinking", "tool_use"):
                    self.emit({"kind": kind, "block": b, "message": m, "uuid": e.get("uuid"), **meta, **extra})
                elif kind != "redacted_thinking":
                    self.notice(f"assistant block type {kind!r} not understood")
            return True
        results = [b for b in blocks if b.get("type") == "tool_result"]
        for b in results:
            tid = b.get("tool_use_id")
            self.tool_inputs.pop(tid, None)
            for did in [k for k, d in self.dialogs.items() if k == tid or d.get("tool_use_id") == tid]:
                self.dialogs.pop(did)
                self.emit({"kind": "dialog_closed", "id": did, "by": "elsewhere"})
            if tid in self.launches:
                self._agent_result(tid, e.get("toolUseResult"), b)
            self.emit({"kind": "tool_result", "block": b, "tool_use_result": e.get("toolUseResult"),
                       "tool": self.tool_names.pop(tid, None), "uuid": e.get("uuid"), **meta})
        return bool(results)

    def _bind_dialogs(self):
        """PermissionRequest carries no tool_use_id: tie the dialog to the tool_use with the same tool and input, so
        its tool_result closes it. The tool_use may be written before the dialog opens or only once it is decided."""
        bound = {d.get("tool_use_id") for d in self.dialogs.values()}
        for d in self.dialogs.values():
            if d["synthetic"] and not d.get("tool_use_id"):
                d["tool_use_id"] = next((t for t, (name, inp) in self.tool_inputs.items()
                                         if t not in bound and name == d["tool"] and inp == d["input"]), None)
                bound.add(d["tool_use_id"])

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
        if et == "attachment":   # a background agent's result can arrive mid-turn as an attachment
            for text in strings(e):
                if "<task-notification>" in text:
                    self._task_notification(text)
            return
        if et not in ("user", "assistant"):
            self.notice(f"transcript entry type {et!r} not understood")
            return
        if et == "assistant" or any(b.get("type") == "tool_result" for b in
                                    (e.get("message") or {}).get("content") or [] if isinstance(b, dict)):
            self._ensure_turn()
            self._blocks(e, None)
            return
        content = (e.get("message") or {}).get("content")
        text = content if isinstance(content, str) else "\n".join(
            b.get("text", "") for b in content or [] if b.get("type") == "text")
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
            for i, (s, _) in enumerate(self.sent):
                if norm(s)[:60] in norm(body):   # prefix: the CLI rewrites @file mentions and pasted images
                    del self.sent[:i + 1]
                    by_us = True
                    break
        self._turn_start(kind, body, by_us, origin.get("name"))
        if "<task-notification>" in body:
            self._task_notification(body)

    def _task_notification(self, text):
        n = task_notification(text)
        if n["task-id"] in self.agents:
            self._agent_end(n["task-id"], n["status"] or "completed", n["summary"] or "")

    # ---- subagents ----
    def _subagent_dir(self):
        return os.path.join(os.path.dirname(self.transcript_path()), self.session_id, "subagents")

    def _poll_subagents(self):
        for m in glob.glob(os.path.join(self._subagent_dir(), "agent-*.meta.json")):
            aid = os.path.basename(m)[len("agent-"):-len(".meta.json")]
            if aid in self.agents:
                continue
            try:
                meta = read_json(m)
            except (OSError, ValueError):
                continue   # still being written
            tid = meta.get("toolUseId")
            if tid not in self.launches:
                continue   # launched before we attached (or its Agent call isn't read yet)
            inp = self.launches[tid]
            self.agents[aid] = {"tool_use_id": tid, "path": m[:-len(".meta.json")] + ".jsonl", "f": None,
                                "buf": b"", "done": False, "background": meta.get("requestShape") == "background"}
            self.emit({"kind": "subagent_start", "id": aid, "tool_use_id": tid,
                       "description": meta.get("description") or inp.get("description", ""),
                       "prompt": inp.get("prompt", ""), "agent_type": meta.get("agentType"),
                       "background": self.agents[aid]["background"], **self._meta})
        for aid, a in list(self.agents.items()):
            if a["done"] or not a.get("path"):
                continue
            self._drain_agent(aid, a)
            if a["background"]:   # fallback when no task-notification reaches the transcript
                try:
                    finished = read_json(a["path"][:-len(".jsonl")] + ".meta.json").get("finished")
                except (OSError, ValueError):
                    finished = False
                a.setdefault("finished_at", time.time()) if finished else a.pop("finished_at", None)
                if finished and time.time() - a["finished_at"] > 5:
                    self._agent_end(aid, "completed", "")

    def _drain_agent(self, aid, a):
        if a["f"] is None:
            if not os.path.exists(a["path"]):
                return
            a["f"] = open(a["path"], "rb")
        a["buf"] += a["f"].read()
        *lines, a["buf"] = a["buf"].split(b"\n")
        for ln in lines:
            if ln.strip():
                try:
                    e = json.loads(ln)
                    if e.get("type") in ("user", "assistant"):
                        self._blocks(e, a["tool_use_id"])   # no turn needed: background agents run between turns
                except Exception as ex:
                    self.notice(f"subagent transcript line not handled: {ex!r}")

    def _agent_result(self, tid, tur, block):
        """The Agent call's tool_result: a foreground subagent is done; a background one has only launched."""
        tur = tur if isinstance(tur, dict) else {}
        aid = next((k for k, a in self.agents.items() if a["tool_use_id"] == tid), None)
        if tur.get("status") == "async_launched":
            if aid:
                self.agents[aid]["background"] = True
            return
        self._poll_subagents()
        if aid is None:   # it ended before its transcript appeared (or we missed it): announce it now
            aid = tur.get("agentId") or tid
            inp = self.launches.get(tid, {})
            self.agents[aid] = {"tool_use_id": tid, "done": False, "background": False}
            self.emit({"kind": "subagent_start", "id": aid, "tool_use_id": tid,
                       "description": inp.get("description", ""), "prompt": inp.get("prompt", ""),
                       "agent_type": inp.get("subagent_type"), "background": False, **self._meta})
        content = block.get("content")
        summary = content if isinstance(content, str) else " ".join(
            c.get("text", "") for c in content or [] if isinstance(c, dict))
        self._agent_end(aid, tur.get("status") or ("failed" if block.get("is_error") else "completed"), summary)

    def _agent_end(self, aid, status, summary):
        a = self.agents[aid]
        if a["done"]:
            return
        if a.get("path"):
            self._drain_agent(aid, a)
        a["done"] = True
        if a.get("f"):
            a["f"].close()
        self.launches.pop(a["tool_use_id"], None)
        summary = summary or self.agent_text.get(a["tool_use_id"], "")
        self.agent_text.pop(a["tool_use_id"], None)
        self.emit({"kind": "subagent_end", "id": aid, "tool_use_id": a["tool_use_id"], "status": status,
                   "summary": summary[:2000], **self._meta})

    # ---- transcript reader ----
    _meta = {}

    def _tail_transcript(self, since):
        path, f, buf, pos, replay_end = None, None, b"", 0, 0
        first = True
        while not self.stopped or f and f.close():   # closes the file on the way out
            p = self.transcript_path()
            if p != path:
                if f:
                    f.close()
                path, f, buf = p, None, b""
            if f is None and os.path.exists(p):
                f = open(p, "rb")
                size = os.path.getsize(p)
                pos = 0
                if first:   # history from before the attach is not replayed, except what the consumer missed
                    pos = size
                    saved = self.saved_offset() if since == "saved" else None
                    if saved and saved.get("session_id") == self.session_id and saved.get("offset", -1) <= size:
                        pos = saved["offset"]
                    elif saved:
                        self.notice("the last-seen offset belongs to an earlier session (/clear?); not replayed")
                f.seek(pos)
                replay_end = size if first else 0
                first = False      # a transcript that appears later (new session) is read from its start
            elif f is None:
                first = False
            with self.lock:
                self._meta = {"parent_tool_use_id": None, "offset": pos, "replay": pos < replay_end}
                try:
                    self._poll_subagents()
                except Exception as ex:
                    self.notice(f"subagent poll failed: {ex!r}")
            chunk = f.read() if f else b""
            if not chunk:
                time.sleep(0.2)
                continue
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                replay = pos < replay_end
                pos += len(ln) + 1
                if not ln.strip():
                    continue
                self.last_line_at = time.time()
                with self.lock:
                    self._meta = {"offset": pos, "replay": replay}
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
                    os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
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
                 "input": ev.get("tool_input") or {}, "synthetic": not ev.get("tool_use_id")}
            with self.lock:
                self.dialogs[did] = d
                self._bind_dialogs()
                self._ensure_turn()
                self.emit({"kind": "dialog_open", "id": did, "tool_use_id": d.get("tool_use_id"),
                           **{k: d[k] for k in ("dialog", "tool", "input")}})

    # ---- session watch: /clear, restart, idle fallback for turn end, lost sends ----
    def _watch(self):
        gone = False
        while not self.stopped:
            time.sleep(1.0)
            live = self._find()
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
                idle = live.get("status") == "idle" and self.turn is None
                self.idle_since = (self.idle_since or time.time()) if idle else None
                self._expire_sends(time.time())
                # Fallback when no turn_duration marker came: idle with no new lines for a while.
                if (self.turn is not None and live.get("status") == "idle" and not self.dialogs
                        and time.time() - self.last_line_at > 15):
                    self._turn_end("done")

    def _expire_sends(self, now):
        """A sent text that never showed up as a prompt (lost keystrokes, a dialog ate it) is dropped from the queue
        once the session has been idle for SEND_TIMEOUT since the send: a queued prompt shows when its turn starts."""
        if self.idle_since is None:
            return
        keep = []
        for s in self.sent:
            if now - max(s[1], self.idle_since) > SEND_TIMEOUT:
                self.emit({"kind": "send_failed", "text": s[0], "reason": "timeout"})
            else:
                keep.append(s)
        self.sent = keep


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Print a live role session's events; stdin lines are input.")
    ap.add_argument("role")
    ap.add_argument("--start", help="command that starts the role in tmux when it isn't running")
    ap.add_argument("--tmux-session", help="only attach to a session running in this tmux session")
    a = ap.parse_args()
    s = LiveSession.attach(role=a.role, start_cmd=a.start, tmux_session=a.tmux_session)
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
        elif cmd == ":model":   # ":model M [E]", for this session only
            m, _, e = rest.partition(" ")
            s.set_model(m or None, e or None)
        elif cmd == ":effort":
            s.set_model(None, rest)
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
