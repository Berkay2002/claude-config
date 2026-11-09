#!/usr/bin/env python3.11
"""Routing gate: enforces the Budget page's routing.json (locks, overrides, the weekly stop) on worker launches.

`routing_gate.py session-start` prints the effective routing table and the weekly pace as SessionStart context.
`routing_gate.py pre` is a PreToolUse hook for Agent and Bash (`claude --bg`, `claude -p --model`): it denies a launch
that breaks a lock or an active override, and every launch once weekly usage is at stop_at, unless the prompt names
`routing-ok: <id>` of a decision the user answered (each id works once, recorded in ~/.claude/routing-gate-used.jsonl).
Machine-local launchers metered outside the weekly quota come from ~/.claude/shared/local/routing_gate_local.json (see
load_local): their launches meet locks and overrides but never the stop, and when its off_subscription_env says this
session is not on the subscription, local Agent calls skip the stop too; a `claude` or any other claude-x launch still
meets it.
Everything else allows, and so does any error or timeout before the stop applies: it prints nothing (an explicit allow
would bypass the user's permission prompts) and logs one line to ~/.claude/routing-gate.log. Once the stop applies, an
error or timeout denies. It writes no files but those two.
Accepted gap: the same user can rewrite this gate, the local file or a launcher it names; the tripwire is a speed bump.
"""
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

BUDGET_S = 1.0
STALE_S = 6 * 3600
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
AGENTS = ("scout", "scout-lead", "builder", "reviewer", "security-audit")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULTS = {  # the contract's defaults when routing.json is missing or invalid (budget-v3.md, notes 2026-11-07)
    "agents": {
        "scout": {"model": "haiku", "effort": "medium", "enforce": "default"},
        "scout-lead": {"model": "sonnet", "effort": "medium", "enforce": "default"},
        "builder": {"model": "opus", "effort": "medium", "enforce": "default"},
        "reviewer": {"model": "opus", "effort": "medium", "enforce": "default"},
        "security-audit": {"model": "fable", "effort": "high", "enforce": "default"},  # Fable 5.2 rule, 2026-11-09
    },
    "overrides": [],
    "rules": {"stop_at": 90, "working_days": ["mon", "tue", "wed", "thu", "fri"]},
}
OK_RE = re.compile(r"routing-ok:\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,80})")
OK_LABEL = "Launch over the stop"  # the approving option's exact label in the AskUserQuestion dialog
CLOSERS = {"answer": "answered", "withdraw": "withdrawn", "expire": "expired", "supersede": "superseded"}


class Invalid(Exception):
    pass


def home():
    return os.environ.get("HOME") or os.path.expanduser("~")


def log(msg):
    try:
        with open(os.path.join(home(), ".claude", "routing-gate.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass  # a log failure must never change the decision


def parse_ts(s):
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError(f"no zone offset: {s!r}")
    return t


# ---------- routing.json ----------

def load_routing():
    """(config, warning). Missing file: the defaults, no warning. Invalid file: the defaults and one warning."""
    path = os.path.join(home(), ".claude", "shared", "local", "routing.json")
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return DEFAULTS, None
    except (OSError, ValueError) as e:
        log(f"routing.json unreadable: {e.__class__.__name__}: {e}")
        return DEFAULTS, f"routing.json is unreadable ({e.__class__.__name__}): using the defaults"
    try:
        return validate(raw), None
    except (Invalid, ValueError, TypeError, AttributeError, KeyError) as e:
        log(f"routing.json invalid: {e}")
        return DEFAULTS, f"routing.json is invalid ({e}): using the defaults"


FABLE_AGENTS = ("reviewer", "security-audit")  # critical reviews may run on Fable (rule of 2026-11-09)


def check_pick(p, where, agent=None):
    models = MODELS + ("fable",) if agent in FABLE_AGENTS else MODELS
    if not isinstance(p, dict) or p.get("model") not in models or p.get("effort") not in EFFORTS:
        raise Invalid(f"{where}: model/effort")


def validate(raw):
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise Invalid("version is not 1")
    agents = dict(DEFAULTS["agents"])
    for name, p in (raw.get("agents") or {}).items():
        if name not in AGENTS:
            raise Invalid(f"unknown agent {name!r}")
        check_pick(p, name, name)
        if p.get("enforce", "default") not in ("default", "lock"):
            raise Invalid(f"{name}: enforce")
        agents[name] = {"model": p["model"], "effort": p["effort"], "enforce": p.get("enforce", "default")}
    overrides = []
    for o in raw.get("overrides") or []:
        if not isinstance(o, dict) or o.get("agent") not in AGENTS:
            raise Invalid("override agent")
        check_pick(o, f"override {o.get('id')}", o.get("agent"))
        parse_ts(o["until"])
        overrides.append(o)
    rules = raw.get("rules") or {}
    stop = rules.get("stop_at", 90)
    days = rules.get("working_days", DEFAULTS["rules"]["working_days"])
    if type(stop) is not int or not 1 <= stop <= 100:
        raise Invalid("stop_at")
    if not isinstance(days, list) or not days or any(d not in DAYS for d in days):
        raise Invalid("working_days")
    return {"agents": agents, "overrides": overrides, "rules": {"stop_at": stop, "working_days": days}}


def effective(cfg, agent, now):
    """(pick, enforced, until): an active override wins, then a lock; a lapsed override is ignored."""
    for o in cfg["overrides"]:
        if o["agent"] == agent:
            until = parse_ts(o["until"])
            if now < until:
                return o, True, until
    p = cfg["agents"][agent]
    return p, p["enforce"] == "lock", None


# ---------- usage ----------

def load_usage(now):
    """(percent, resetsAt) when limits.json is fresh and its week has not reset yet, else None (usage unknown)."""
    try:
        with open(os.path.join(home(), ".claude", "usage", "limits.json")) as f:
            d = json.load(f)
        upd, wk = parse_ts(d["updatedAt"].replace("Z", "+00:00")), d["week"]
        pct, reset = float(wk["percentUsed"]), parse_ts(wk["resetsAt"].replace("Z", "+00:00"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        log(f"limits.json unreadable, usage unknown: {e.__class__.__name__}: {e}")
        return None
    if (now - upd).total_seconds() > STALE_S or reset <= now:
        return None
    return pct, reset


def pace(stop_at, days, reset, now):
    """stop_at x working days gone / working days in the week [reset - 7 d, reset). Each 24 h slice of the week counts
    by the local weekday it starts on; the current working slice counts by the fraction gone."""
    start = reset - timedelta(days=7)
    total = gone = 0.0
    for i in range(7):
        s = start + timedelta(days=i)
        if DAYS[s.astimezone().weekday()] in days:
            total += 1
            gone += min(max((now - s).total_seconds() / 86400, 0.0), 1.0)
    return stop_at * gone / total if total else float(stop_at)


def local(t):
    return t.astimezone().strftime("%a %m-%d %H:%M")


def fmt(p):
    return f"{p['model']}@{p['effort']}"


# ---------- session-start ----------

def session_start(now):
    cfg, warn = load_routing()
    lines = ["Routing (Budget page, ~/.claude/shared/local/routing.json; wins over the CLAUDE.md ladders):"]
    if warn:
        lines.append(f"WARNING: {warn}")
    for a in AGENTS:
        p, enforced, until = effective(cfg, a, now)
        tag = f"override until {local(until)}" if until else ("lock" if enforced else "default")
        lines.append(f"  {a}: {fmt(p)} ({tag})")
    stop = cfg["rules"]["stop_at"]
    u = load_usage(now)
    if u is None:
        lines.append(f"Weekly usage unknown (limits.json missing or older than 6 h): stop at {stop}% not checked.")
    else:
        pct, reset = u
        pc = pace(stop, cfg["rules"]["working_days"], reset, now)
        state = ("stopped: weekly >= stop_at, no new workers" if pct >= stop else
                 "ahead of pace: drop one rung on new launches" if pct > pc else "within pace")
        lines.append(f"Weekly {pct:.0f}% vs pace {pc:.0f}% (stop at {stop}%, resets {local(reset)}): {state}.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines[:15])}}


# ---------- pre ----------

def family(m):
    m = (m or "").lower()
    return next((f for f in (*MODELS, "fable") if f in m), m or None)


def frontmatter(agent):
    """model/effort defaults from ~/.claude/agents/<agent>.md (the installed agents)."""
    out = {}
    try:
        with open(os.path.join(home(), ".claude", "agents", f"{agent}.md")) as f:
            text = f.read(4096)
    except OSError:
        return out
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() in ("model", "effort"):
            out[k.strip()] = v.strip()
    return out


def agent_launch(ti):
    st = ti.get("subagent_type") or ""
    if st in ("", "general-purpose", "reviewer") and re.search(r"security[ -]audit", ti.get("description") or "", re.I):
        agent = "security-audit"
    elif st in AGENTS:
        agent = st
    else:
        agent = None
    fm = frontmatter(st) if st else {}
    return agent, ti.get("model") or fm.get("model"), ti.get("effort") or fm.get("effort"), ti.get("prompt") or ""


CAP = 65536  # bytes of command or prompt the gate looks at
# At the stop, with no parsing: a `claude` word (not .claude or claude-x, so not a local launcher) and a launch flag
# anywhere in the command.
STOP_RE = re.compile(r"(?<![\w.-])claude(?![\w.-])(?=.*(?:--bg\b|--background\b|--print\b|(?<![\w-])-[A-Za-z]*p[A-Za-z]*\b))",
                     re.S)
CLAUDE_WORD = re.compile(r"(?<![\w.-])claude(?![\w.-])")
INTERACTIVE = re.compile(r"claude-role|(?<![\w.-])(?:tmux|screen)(?![\w.-])")  # tmux/screen count with a claude word
TRIPWIRE = ("decide-hook", "decisions.jsonl", "AGENT_MAIL_NAME=mothership",  # forging an approval
            "routing_gate_local")  # or a local file that lifts the stop
LAUNCHY = re.compile(r"claude\b[^\n]*?(?:--bg\b|--background\b|--print\b|\s-p\b)")
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
WRAPPERS = {"env", "nohup", "command", "exec", "nice", "setsid", "timeout", "stdbuf"}
SHELLS = {"bash", "sh", "zsh", "dash"}


def strip_heredocs(cmd):
    """Drops heredoc bodies (data, not commands); the `<<EOF` marker stays on its line."""
    out, ends = [], []
    for line in cmd.split("\n"):
        if ends:
            tabs, word = ends[0]
            if (line.lstrip("\t") if tabs else line) == word:
                ends.pop(0)
            continue
        out.append(line)
        ends = [(m.group(1) == "-", m.group(3)) for m in HEREDOC.finditer(line)]
    return "\n".join(out)


def segments(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    segs, cur = [], []
    for t in lex:
        if t and set(t) <= set(";&|()\n"):
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    return segs + [cur]


def unwrap(seg):
    """The command under env/nohup/timeout/... prefixes; ("sh", text) for `bash -c text` or `tmux new-window text`."""
    i = 0
    while i < len(seg):
        w = os.path.basename(seg[i])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]) or seg[i].startswith("-"):
            i += 1  # VAR=x, or an option of a wrapper
        elif w in ("nice", "timeout", "stdbuf") or (w == "env" and i + 1 < len(seg) and seg[i + 1] in ("-u", "-C")):
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or re.match(r"^[0-9.]+[smhd]?$", seg[i])):
                i += 2 if seg[i] in ("-n", "-s", "-k", "-u", "-C") else 1  # option values and the duration
        elif w in WRAPPERS:
            i += 1
        elif w in SHELLS:
            rest = seg[i + 1:]
            return ("sh", rest[rest.index("-c") + 1]) if "-c" in rest and rest.index("-c") + 1 < len(rest) else None
        elif w == "tmux":
            sub = next((a for a in seg[i + 1:] if not a.startswith("-")), "")
            if sub in ("new-window", "neww", "new-session", "new", "split-window", "splitw", "respawn-pane"):
                return ("sh", seg[-1])  # tmux runs its last argument as a shell command
            return None
        else:
            return ("cmd", seg[i:])
    return None


def claude_args(argv):
    """(agent, model, effort, text) for argv = claude ... when it launches a worker, else None."""
    name = os.path.basename(argv[0])  # a metered launcher's name anywhere still meets its lock
    if name != "claude" and name not in {os.path.basename(p) for p in load_local()["launchers"]}:
        return None
    flags, pos, j, args = {}, [], 0, argv[1:]
    while j < len(args):
        a = args[j]
        k, eq, v = a.partition("=")
        if k in ("--model", "--effort", "--name", "-n", "--agent"):
            flags[k] = v if eq else (args[j + 1] if j + 1 < len(args) else "")
            j += 1 if eq else 2
            continue
        if a.startswith("-"):
            flags[a] = ""
        else:
            pos.append(a)
        j += 1
    bg = "--bg" in flags or "--background" in flags
    if not (bg or (("-p" in flags or "--print" in flags) and "--model" in flags)):
        return None
    if flags.get("--agent") in AGENTS:
        return flags["--agent"], flags.get("--model"), flags.get("--effort"), " ".join(pos)
    words = re.split(r"[-_.]", (flags.get("--name") or flags.get("-n") or "").lower())
    pairs = set(zip(words, words[1:]))
    agent = ("scout-lead" if ("scout", "lead") in pairs else "reviewer" if {"review", "reviewer"} & set(words)
             else "scout" if "scout" in words else "builder")
    return agent, flags.get("--model"), flags.get("--effort"), " ".join(pos)


def bash_launch(cmd, depth=0):
    """[(agent, model, effort, text)], one per `claude --bg` or `claude -p --model` launch in the command (so a first
    launch cannot hide a second from its lock); agent, model and effort are None when a segment looks like one but
    cannot be parsed; [] when there is no launch."""
    try:
        segs = segments(strip_heredocs(cmd))
    except ValueError:
        return [(None, None, None, cmd)] if LAUNCHY.search(cmd) else []  # unreadable: only the stop applies
    hits = []
    for seg in segs:
        u = unwrap(seg)
        if u is None:
            continue
        kind, val = u
        if kind == "cmd":
            hit = claude_args(val)
            hits += [hit] if hit else []
        elif depth < 2:  # bash -c / tmux text, recursing into at most two such layers
            hits += bash_launch(val, depth + 1)
        elif LAUNCHY.search(val):
            hits.append((None, None, None, val))
    return hits


def mail_dir():
    d = os.environ.get("AGENT_MAIL_DIR")
    if d:
        return d
    cfg = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or home(), ".claude.json")
    with open(cfg) as f:
        return ((json.load(f).get("mcpServers") or {}).get("agent-mail") or {}).get("env", {}).get("AGENT_MAIL_DIR")


def used_path():
    return os.path.join(home(), ".claude", "routing-gate-used.jsonl")


def approved(text, week_start):
    """The `routing-ok: <id>` id when it names a decision about the weekly stop that the user answered yes (not
    no/wait/cancel) during the current usage week, in the AskUserQuestion dialog (the hook's answer, marked
    via=hook, on a decision the hook opened or touched) or on Mothership (by mothership or user, answered_in
    council#N or page). An answer a role wrote itself does not count. Any error: None (not approved)."""
    try:
        m = OK_RE.search(text or "")
        if not m:
            return None
        want, d = m.group(1), None
        with open(os.path.join(mail_dir(), "decisions.jsonl"), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("id") != want:
                    continue
                ev = e.get("event")
                if ev == "open" and d is None:
                    d = {"status": "open", "question": str(e.get("question") or ""), "tid": None}
                if d is None:
                    continue
                if ev in ("open", "touch") and e.get("tool_use_id"):
                    d["tid"] = e["tool_use_id"]
                if ev in CLOSERS:  # agent-mail's fold: the last closing event is the status
                    d.update(status=CLOSERS[ev], ts=e.get("ts"), answer=str(e.get("answer") or ""),
                             answered_in=str(e.get("answered_in") or ""), by=e.get("by"), via=e.get("via"))
        if not d or d["status"] != "answered":
            return None
        dialog = d["via"] == "hook" and d["tid"]
        page = d["by"] in ("mothership", "user") and re.fullmatch(r"council#[0-9]+|page", d["answered_in"])
        ok = ((dialog or page)
              and "routing-ok" in d["question"]
              and d["answer"].startswith(OK_LABEL)
              and parse_ts(d["ts"]) >= week_start)
        return want if ok else None
    except Exception as e:
        log(f"pre: routing-ok not approved after error: {e.__class__.__name__}: {e}")
        return None


def claim(i):
    """Marks routing-ok id i used; False when it was used before. Check and append under one flock."""
    with open(used_path(), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        text = f.read()
        for line in text.splitlines():
            try:
                if json.loads(line).get("id") == i:
                    return False
            except (ValueError, AttributeError):
                continue  # a line cut short by a crash: skip it, like agent-mail's read_jsonl
        f.write(("" if not text or text.endswith("\n") else "\n") + json.dumps({"id": i, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
        f.flush()
        return True


def stop_hit(cmd):
    """At the stop, with no parsing: a claude word with a launch flag, claude-role, or tmux/screen with a claude word."""
    m = INTERACTIVE.search(cmd)
    return bool(STOP_RE.search(cmd) or (m and (m.group() == "claude-role" or CLAUDE_WORD.search(cmd))))


def plain_claude(cmd):
    """True when cmd is one plain `claude ...` (first token claude, no VAR=val or env before it): no ` or $ outside
    single quotes (they expand inside double quotes too), no ; & | ( ) < > # or newline outside quotes."""
    q = None
    for i, c in enumerate(cmd):
        if q == "'":
            q = None if c == "'" else q
        elif c == "\\":
            return False  # escapes make quoting ambiguous: not plain
        elif c in "`$":
            return False
        elif q == '"':
            q = None if c == '"' else q
        elif c in "'\"":
            q = c
        elif c in ";&|()<>#\n":
            return False
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    return bool(toks) and toks[0] == "claude"


LAUNCHER_RE = re.compile(r"claude-[A-Za-z0-9_.+-]+")  # a claude-x name: STOP_RE never matches it, LAUNCHY does
LOCAL_KEYS = {"metered_launchers", "off_subscription_env"}
LAUNCH_FLAG = re.compile(r"--bg\b|--background\b|--print\b|(?<![\w-])-[A-Za-z]*p[A-Za-z]*\b")
TOKEN = re.compile(r"[^\s'\"`;&|()<>=]+")
CLAUDE_ISH = re.compile(r"[\w.+-]*claude[\w.+-]*")
# Anything that could point a bare launcher name at another binary: no exemption from the stop then.
NO_EXEMPT = re.compile(r"PATH|ENV|\b(?:alias|hash|function|enable|source|eval|export|declare|typeset|set|shopt|bash|sh|zsh|dash|ksh|fish|sudo|su|ssh|env|exec|command|nice|nohup|xargs|python\d*(?:\.\d+)?|perl)\b|--rcfile|--init-file|ZDOTDIR|\(\s*\)|(?:^|[\s;&|(])\.\s|\\")
# The start of a plain segment (no NAME= or wrapper since the last separator), or of tmux/screen's quoted inner command.
SEG_START = re.compile(r"(?:^|[;&|()\n`{}])\s*(?:(?:tmux|screen)\b[^;&|()\n`'\"]*['\"]\s*)?$")
_local = None  # this run's config from load_local(); reset by run()


def temp_roots():
    """Agent-writable temp dirs a metered launcher must not sit in (the scratchpad is under /tmp)."""
    import tempfile
    dirs = {"/tmp", "/var/tmp", "/dev/shm", tempfile.gettempdir(), os.environ.get("TMPDIR") or "/tmp"}
    return {os.path.realpath(d) for d in dirs}


def check_launcher(p):
    """The realpath of a metered launcher entry, or raise Invalid."""
    if not isinstance(p, str) or not os.path.isabs(p) or not LAUNCHER_RE.fullmatch(os.path.basename(p)):
        raise Invalid(f"metered launcher must be an absolute path to a claude-x name: {p!r}")
    r = os.path.realpath(p)
    c = shutil.which("claude")
    if not os.path.isfile(r) or os.path.basename(r) == "claude" or (c and os.path.realpath(c) == r):
        raise Invalid(f"metered launcher is missing or is claude itself: {p!r}")
    if any(r == t or r.startswith(t + os.sep) or p.startswith(t + os.sep) for t in temp_roots()):
        raise Invalid(f"metered launcher sits in a temp dir: {p!r}")
    return r


def load_local():
    """This machine's metered launchers and subscription test from ~/.claude/shared/local/routing_gate_local.json
    (machine-only, not in git), as {"launchers": {listed path: realpath}, "env": {VAR: [values]}}:
    "metered_launchers", absolute paths of claude-x launchers metered outside the weekly quota, and
    "off_subscription_env", {VAR: [values]}: the session is off the subscription when the hook's own VAR (which a Bash
    command cannot change) is one of its values, ignoring case and spaces. Data only, never run. A missing file, or one
    that is invalid in any way, counts as absent (logged once per run): the gate then behaves as if it is not there: a launcher it named meets the stop, and below
    the stop no lock check, like any other claude-x tool."""
    global _local
    if _local is None:
        _local = {"launchers": {}, "env": {}}
        path = os.path.join(home(), ".claude", "shared", "local", "routing_gate_local.json")
        if os.path.isfile(path):  # not a fifo that blocks the open
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict) or set(raw) - LOCAL_KEYS:
                    raise Invalid(f"want an object with only {sorted(LOCAL_KEYS)}")
                ls, env = raw.get("metered_launchers", []), raw.get("off_subscription_env", {})
                if not isinstance(ls, list) or not isinstance(env, dict):
                    raise Invalid("metered_launchers must be a list, off_subscription_env an object")
                for k, vs in env.items():
                    if not (k and isinstance(vs, list) and vs and all(isinstance(v, str) and v.strip() for v in vs)):
                        raise Invalid(f"off_subscription_env {k!r} needs a list of non-empty strings")
                _local = {"launchers": {p: check_launcher(p) for p in ls},
                          "env": {k: {v.strip().lower() for v in vs} for k, vs in env.items()}}
            except TimeoutError:
                raise  # the run's one timer: denies at the stop, allows below it, as for any other step
            except Exception as e:
                log(f"local: ignored {path}: {e.__class__.__name__}: {e}".replace("\n", " "))
    return _local


def off_subscription():
    """True when the hook's environment matches the local off_subscription_env."""
    return any(os.environ.get(k, "").strip().lower() in vs for k, vs in load_local()["env"].items())


def metered(tok):
    """True when a command word is a metered launcher: its exact listed path, or a bare name that resolves on the
    hook's own PATH to the same realpath."""
    ls = load_local()["launchers"]
    if "/" in tok:
        return tok in ls
    w = shutil.which(tok)
    return bool(w) and os.path.isabs(w) and os.path.realpath(w) in ls.values()


def exempt(cmd, m):
    """True when token m is a metered launcher in a launch that skips the stop: the first word of a plain segment (no
    NAME= or wrapper before it; inside tmux/screen quotes the same), nothing before it in the command could redirect
    its name (the brief after it may say anything), and a launch flag follows."""
    pre = cmd[:m.start()]
    return bool(SEG_START.search(pre) and not NO_EXEMPT.search(pre) and LAUNCH_FLAG.search(cmd, m.end())
                and metered(m.group()))


def unmetered_launch(cmd):
    """At the stop, with no parsing: a claude-x word (or any word with claude in it, but not claude itself, which
    STOP_RE covers) with a launch flag after it, unless exempt()."""
    for m in TOKEN.finditer(cmd):
        base = os.path.basename(m.group()).replace("\\", "")  # \claude-x runs claude-x
        if CLAUDE_ISH.fullmatch(base) and base != "claude" and not base.startswith(".") \
                and LAUNCH_FLAG.search(cmd, m.end()) and not exempt(cmd, m):
            return True
    return False


def local_trip(cmd):
    """At the stop: the command names a metered launcher's file other than as an exempt launch (e.g. to rewrite it)."""
    ls = load_local()["launchers"]
    names = {os.path.basename(p) for p in ls}
    ok = {m.start() for m in TOKEN.finditer(cmd) if m.group() in ls and exempt(cmd, m)}
    if any("/" in m.group() and os.path.basename(m.group()).replace("\\", "") in names and m.start() not in ok
           for m in TOKEN.finditer(cmd)):
        return True
    return any(i not in ok for p in {*ls, *ls.values()} for i in (m.start() for m in re.finditer(re.escape(p), cmd)))


def decide(ev, now, state):
    """A deny reason, or None to allow. Sets state["stop"] once the weekly stop applies, so a later error or timeout
    denies instead of letting the launch through. At the stop a Bash command is judged on the whole command with
    regexes, no parsing; below it the parser only serves locks and overrides, and allows when in doubt."""
    tool, ti = ev.get("tool_name"), ev.get("tool_input") or {}
    if tool == "Bash":
        full = str(ti.get("command") or "")
        trip = any(t in full for t in TRIPWIRE)
        if "claude" not in full and not trip:
            return None
        cmd = full[:CAP]
    elif tool != "Agent":
        return None
    cfg, warn = load_routing()
    u = load_usage(now)
    stopped = u is not None and u[0] >= cfg["rules"]["stop_at"]
    ok = None
    if stopped:
        pct, reset = u
        head = (f"routing: weekly {pct:.0f}% ≥ stop_at {cfg['rules']['stop_at']}%: no new workers until the reset "
                f"({local(reset)}).")
        ask = (" If the user explicitly asked for this, ask him with AskUserQuestion: put `routing-ok` and the weekly % "
               f"in the question and make the first option exactly \"{OK_LABEL}\"; if he picks it, relaunch with "
               "`routing-ok: <decision id>` in the prompt (one launch per id). Or he can raise stop_at on the Budget "
               "page.")
        state["stop"] = head + ask  # set before anything reads the local file, so its error or timeout denies
        if tool == "Bash" and (trip or local_trip(full)):
            return (head + " This command touches the decisions store, its hook or the gate's local files, which is "
                    "not allowed at the stop.")
        if not (tool == "Agent" or stop_hit(full) or unmetered_launch(full)):
            state["stop"] = None
        if tool == "Agent" and (ev.get("agent_id") or (ti.get("isolation") != "remote" and off_subscription())):
            state["stop"] = None  # a running subagent finishes its task, local subagents off the subscription run on;
            # locks still apply
        elif state["stop"]:
            if tool == "Agent":
                state["stop"] += (" Weekly stop: finish without new subagents and note the skipped step (e.g. the "
                                  "review) in your report.")
            if tool == "Bash" and OK_RE.search(full) and not plain_claude(full):
                return ("routing: routing-ok needs one plain claude command: first word claude (no env or VAR=val "
                        "before it, no wrappers or loops), no $ or ` outside single quotes, no ; & | ( ) < > # or "
                        "newline outside quotes. " + head)
            ok = approved(full if tool == "Bash" else str(ti.get("prompt") or ""), reset - timedelta(days=7))
            if not ok:
                return state["stop"]
    if tool == "Bash" and not ok and not LAUNCHY.search(cmd):
        return None
    for agent, model, effort, _ in (bash_launch(cmd) if tool == "Bash" else [agent_launch(ti)]):
        if agent is None:
            continue
        p, enforced, until = effective(cfg, agent, now)
        if enforced and (family(model) != p["model"] or (effort or "").lower() != p["effort"]):
            why = f"has an override until {local(until)}" if until else "is locked"
            return (f"routing: {agent} {why} to {fmt(p)} (Budget page); "
                    f"relaunch with model {p['model']}, effort {p['effort']}")
    if ok and not claim(ok):  # single use; an error here raises while state["stop"] is set, so it denies
        return state["stop"]
    state["stop"] = None
    return None


def deny(reason):
    log(f"deny: {reason}")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def run(phase, stdin, now=None):
    """The hook body: returns the JSON to print, or None. An error or timeout allows and logs one line, unless the
    weekly stop already applied to this launch: then it denies."""
    def alarm(*_):
        raise TimeoutError(f"gave up after {BUDGET_S} s")
    global _local
    state, _local = {"stop": None}, None
    signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, BUDGET_S)
    try:
        now = now or datetime.now(timezone.utc)
        if phase == "session-start":
            return session_start(now)
        if phase == "pre":
            reason = decide(json.loads(stdin.read()), now, state)
            return deny(reason) if reason else None
        raise ValueError(f"phase must be session-start or pre, got {phase!r}")
    except BaseException as e:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if state["stop"]:
            log(f"{phase}: denied at the stop after error: {e.__class__.__name__}: {e}".replace("\n", " "))
            return deny(state["stop"])
        log(f"{phase}: allowed after error: {e.__class__.__name__}: {e}".replace("\n", " "))
        return None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_IGN)


def main(argv):
    out = run(argv[1] if len(argv) > 1 else "", sys.stdin)
    if out is not None:
        try:
            sys.stdout.write(json.dumps(out) + "\n")
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
