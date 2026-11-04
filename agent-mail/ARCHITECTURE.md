# agent-mail architecture

**TL;DR:** one Python file with two modes, and every session (Claude or Codex) loads the same MCP server. Messages are
files in a shared folder. The orchestrator's server starts a detached runner per Codex worker, the runner drives
`codex exec` / `codex exec resume`, and mails each turn's result back. Claude gets woken through the `claude/channel`
push; Codex reads mail with `inbox`. Tools, environment and registration are in [README.md](README.md).

## The parts

**TL;DR:** three kinds of process, one mail folder.

```mermaid
flowchart LR
    subgraph Claude["Claude orchestrator (AGENT_MAIL_NAME=project-manager)"]
        C[Claude Code]
        S1["agent_mail.py serve<br/>(stdio MCP)"]
        C <-->|"tools: send, codex_start, retire, list, inbox"| S1
        S1 -.->|"notifications/claude/channel<br/>(push)"| C
    end

    subgraph Mail["AGENT_MAIL_DIR (/repo/$USER/.agent-mail)"]
        IB["inbox/&lt;name&gt;/new → cur"]
        WR["workers/&lt;name&gt;.json<br/>workers/&lt;name&gt;/queue, taken, logs"]
    end

    subgraph Worker["Codex worker &lt;name&gt;"]
        R["agent_mail.py run &lt;name&gt;<br/>(detached runner, flock)"]
        X["codex exec --json<br/>codex exec resume &lt;thread&gt;"]
        S2["agent_mail.py serve<br/>(injected with -c per turn)"]
        R -->|"one turn per batch"| X
        X <-->|"tools: send, inbox, list"| S2
    end

    S1 -->|"write queue, spawn runner"| WR
    R -->|"claim queue → taken"| WR
    R -->|"turn_end / error"| IB
    S2 -->|"send (message)"| IB
    S1 -->|"poll 1 s, claim"| IB
```

- **`serve`**: the MCP server, one per session. Claude talks to it over stdio. In a Codex turn it is injected with
  `-c mcp_servers.agent-mail.*`, so Codex's `config.toml` is never touched.
- **`run <name>`**: the runner. It starts in its own session, so a Claude restart or `/mcp` reconnect never kills a
  turn. An `flock` makes sure only one runs per worker, and it exits when the queue is empty.
- **Mail folder**: all state. If any process dies, the files still say what is queued, running and delivered.

## One round trip

**TL;DR:** start, mail mid-turn, turn result, reply, next turn on the same thread.

```mermaid
sequenceDiagram
    autonumber
    participant C as Claude (orchestrator)
    participant S as its agent-mail server
    participant F as mail folder
    participant R as runner
    participant X as codex

    C->>S: codex_start(name, cwd, prompt)
    S->>F: workers/name.json (starting), prompt in queue
    S->>R: spawn (detached)
    S-->>C: returns at once
    R->>F: claim queue → taken
    R->>X: codex exec --json -- prompt
    X-->>R: thread.started (thread id)
    R->>F: mail owner "started, thread …"
    F-->>S: poll sees new mail
    S-->>C: channel push ← agent-mail: started…
    X->>F: send(to=orchestrator) via its own agent-mail
    S-->>C: channel push ← agent-mail: HELLO-MIDTURN
    X-->>R: turn ends, last message
    R->>F: mail owner turn_end, then delete taken/
    S-->>C: channel push ← agent-mail: TURN1-DONE
    C->>S: send(to=name, text)
    S->>F: queue the text
    S->>R: spawn (runner had exited)
    R->>X: codex exec resume thread -- "From orchestrator: text"
    X-->>R: turn ends
    R->>F: mail owner turn_end
    S-->>C: channel push ← agent-mail: SEVEN
```

This is the exact sequence the end-to-end test ran on the VDI.

## Worker states

**TL;DR:** silence never means success: every turn ends in `turn_end` or `error` mail.

```mermaid
stateDiagram-v2
    [*] --> starting: codex_start
    starting --> running: runner takes the lock
    running --> idle: turn ok → turn_end mail
    running --> failed: exit code, turn.failed, no last message, 4 h timeout → error mail
    idle --> running: send (queued mail)
    failed --> running: send (thread resumes)
    running --> failed: runner died → owner's server revives it, error mail, messages replayed
    idle --> [*]: retire
    failed --> [*]: retire
```

## Delivery rules

**TL;DR:** a rename claims a message, a lock serialises a worker, and a result is mailed before its input is deleted.

| Concern | How it is handled |
|---|---|
| Exactly-once inbox read | Each message is one file in `new/`; a reader claims it with `os.rename` to `cur/`. Two readers can't both win. |
| Push only when it lands | The server pushes only to a `claude-code` client whose parent was started with `--dangerously-load-development-channels server:agent-mail` (checked in `/proc/<ppid>/cmdline`). Otherwise the mail stays for `inbox`. |
| No lost turn | Queue files move to `taken/` for the turn and are deleted only after the result is mailed. A runner that dies leaves them there; the next runner puts them back and mails an `error` first. At-least-once, never zero. |
| Dead runner | The owner's server (push thread, `list`, `inbox`) checks the runner pid and starts a new one. |
| Full environment | Only the worker's owner spawns runners, so every turn gets the orchestrator's env (proxies, `UV_*`, `LOG_PATH`), never the filtered env Codex hands to MCP servers. While the owner is down, its workers' mail waits. |
| Flag injection | The prompt goes after `--`; names allow only letters, digits, `_` and `-` (no dots: `x.rec` would collide with lock files). |
| Restricted workers | `sandbox` defaults to `full` (bypass flag). A `read-only` or `workspace-write` worker can't start a looser one. |

## Mail folder layout

```
$AGENT_MAIL_DIR/
├── tmp/                              each message is written here, then renamed into place
├── inbox/<name>/new/<ns>-<id>.json   unread
├── inbox/<name>/cur/                 claimed
└── workers/
    ├── <name>.json                   record: state, owner, thread_id, cwd, sandbox, pid, turns
    ├── <name>.lock, <name>.rec.lock  runner lock, record lock
    └── <name>/
        ├── queue/                    messages waiting for the next turn
        ├── taken/                    messages in the running turn
        └── logs/                     runner.log; turn-<n>.jsonl, .last.md, .err
```

## Limits

`retire` keeps the Codex thread (`codex delete --force <id>` removes it), sender names are self-declared, the push
needs an interactive Claude with the development-channel flag (a `--bg` orchestrator polls `inbox`), and Kiro has no
MCP channel, so it is not covered.
