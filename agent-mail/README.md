# agent-mail

One stdio MCP server (`agent_mail.py`, Python 3.11 stdlib, one file) that Claude Code and Codex CLI both load. A Claude
orchestrator uses it to start Codex workers, message them by name, and be woken by their messages and turn results,
the way `claude --bg` + SendMessage work between Claude sessions. No Claude mod, no hooks, no Monitor. Diagrams:
[ARCHITECTURE.md](ARCHITECTURE.md).

## Tools

| Tool | What it does |
|---|---|
| `send(to, text)` | `to` is a Codex worker: the text is queued (and its runner started when you own it). Any other name: written to that inbox. |
| `codex_start(name, cwd, prompt, model?, effort?, sandbox?)` | Starts a Codex worker. Returns at once; the thread id and every turn result arrive as mail from the worker. |
| `retire(name, force?, delete_thread?)` | Deletes a finished worker's record, queue and logs (owner only; its empty lock files stay and are reused). Refused while a turn runs ("retry after its turn_end") and while `queue/` or `taken/` holds mail, unless `force`. The Codex thread is kept unless `delete_thread` (then `codex delete --force` runs after the state is gone; a failed delete is reported, the worker is still retired). |
| `list()` | Workers (state, thread id, cwd, last message's first line) and this inbox's unread count. |
| `codex_usage()` | Codex's live quota from `codex app-server` (`account/rateLimits/read`, about 1.5 s): plan, 5-hour and weekly percent with reset times, banked resets (each zeroes both windows) with expiry and whether they lapse before the weekly reset, `spare_week_percent_per_day` and `use_before_lapse_percent_per_day`. Read-only: it never spends a reset. |
| `inbox()` | Claims and returns this instance's unread mail (for clients without push, e.g. Codex). |

Mail kinds: `message`, `turn_end` (the worker's last message), `error` (nonzero exit, `turn.failed`, no last
message, a turn over 4 hours, or a runner that died; the text names the cause and the log). Silence never means
success. Names (workers and inboxes) are letters, digits, `_` and `-`.

## Environment (nothing defaults to `~`)

- `AGENT_MAIL_DIR`: absolute mail root, **on local disk**, never on a network-shared home. Missing: every tool returns a clear error and nothing is created.
- `AGENT_MAIL_NAME`: who this instance is. Without it `send` still works (from `unknown`) but there is no inbox,
  no push and no `claude/channel` capability. An invalid name makes every tool fail.
- `AGENT_MAIL_CODEX`: the codex binary (default `codex` on PATH). `CODEX_HOME`: passed through to every worker turn
  (set it explicitly: a Claude and a shell with different `HOME`s would otherwise find different Codex homes).
- `AGENT_MAIL_POLL`: inbox poll seconds (default 1).

## Register (once, by hand)

Claude Code, user scope. The name is not in the registration: it comes from the launching shell, so each session can
have its own.

```
claude mcp add --scope user agent-mail -e AGENT_MAIL_DIR=<local disk>/.agent-mail -e CODEX_HOME=<your CODEX_HOME> \
  -- python3.11 <this checkout>/agent-mail/agent_mail.py serve
```

Orchestrator launch (push needs the development-channel flag and a startup confirmation; `--bg` sessions drop it, so
a background Claude reads mail with `inbox` instead):

```
alias claude-orch='AGENT_MAIL_NAME=<your name> claude --dangerously-load-development-channels server:agent-mail'
```

Claude's variadic flags swallow a trailing prompt: put the prompt before `--mcp-config`, `--channels` and
`--dangerously-load-development-channels`.

Codex: never register it in `config.toml` (`codex mcp add`): that file is shared by every Codex session and job, and one
fixed `AGENT_MAIL_NAME` would let every Codex session claim the same inbox. Codex workers get the server injected per
launch with `-c`. For an interactive Codex session you want an inbox in, do the same with an alias (Codex filters the
environment it hands to MCP servers, so the name goes into the `-c` flags):

```
alias codex-mail='codex -c mcp_servers.agent-mail.command="python3.11" \
  -c mcp_servers.agent-mail.args="[\"<this checkout>/agent-mail/agent_mail.py\",\"serve\"]" \
  -c mcp_servers.agent-mail.env.AGENT_MAIL_DIR="<local disk>/.agent-mail" \
  -c mcp_servers.agent-mail.env.AGENT_MAIL_NAME="<name>"'
```

## How it works

- Mail: one JSON file per message in `inbox/<name>/new/`, written atomically; a reader claims it by renaming it to
  `cur/`, so it is delivered exactly once even with two readers, and mail sent while no reader ran is delivered when
  one starts. Push to Claude is a `notifications/claude/channel` notification, sent only when the client is
  `claude-code` and that Claude was started with `--dangerously-load-development-channels server:agent-mail`
  (otherwise Claude drops the push, so the mail stays for `inbox`). Codex is `codex-mcp-client` and only gets `inbox`.
- Workers: `workers/<name>.json` (state `starting | running | idle | failed`), `workers/<name>/queue/`, logs per turn
  in `workers/<name>/logs/`. A detached runner (`agent_mail.py run <name>`, one at a time per worker via `flock`)
  runs `codex exec --json`, later turns `codex exec resume <thread_id>`, several queued messages joined into one
  turn, each prefixed `From <sender>:`. It exits when the queue is empty. Only the worker's owner starts runners
  (its `send`, or its agent-mail instance noticing queued mail within a second), so every turn runs with the owner's
  full environment (proxies, `UV_*`, `LOG_PATH`), never Codex's filtered one. Trade-off: while the owner's session is
  down, mail to its workers waits in the queue until it is back.
- A runner that dies mid-turn takes its codex with it. The owner's push thread, `list` or `inbox` notices the dead
  runner and starts a new one, which mails the owner an `error` and runs the interrupted messages again. A turn is
  delivered at least once: a crash in the instant between mailing the result and clearing the turn replays it.
- Every turn re-injects the agent-mail server with `-c mcp_servers.agent-mail.*` (command, args, name, dir, plus
  `CODEX_HOME` and `AGENT_MAIL_CODEX`, because Codex hands MCP servers a filtered env).

## Permissions: workers run unsandboxed by default

`codex_start` defaults to `sandbox: full`, i.e. `--dangerously-bypass-approvals-and-sandbox` on every turn, the same
trust level as our Claude workers (`bypassPermissions`). Reason: workers need network (package installs) and a sandbox
that blocks it makes them useless. Under the bypass flag MCP tool calls are allowed. `read-only` and
`workspace-write` exist for the rare restricted worker (no network, writes only to the workspace); there agent-mail
is auto-approved with `default_tools_approval_mode="approve"`, because `codex exec` otherwise denies every MCP call.
The recorded mode is in the worker record and `list`. A worker's server knows its own level (`AGENT_MAIL_SANDBOX`), so a
restricted worker cannot start a looser one (and defaults to its own level). Not enforced: a restricted worker can still
`send` to a worker that already exists. Sender names (`From <name>:`) are self-declared by whoever writes the mail
file: advisory only, since everything shares one user and one mail dir.

**The sandbox is not what keeps a worker off live systems; the brief is.** Copy the repo's own rules about live
services, shared data and hardware into every brief: say what the worker must not touch, and when it must stop and ask.

## Tests

```
cd agent-mail && python3.11 -B -m unittest -v test_agent_mail
```

A fake `codex`, a temp mail dir and temp HOME; nothing reaches the real codex, mail dir or MCP configs.

## Known limits

A turn is killed after 4 hours (`TURN_TIMEOUT`); shell commands a killed codex started can outlive it. `codex queue` is unreliable and unused. A Claude that declines the channel
dialog still gets pushes it drops: answer yes, or start it without the flag and use `inbox`.
