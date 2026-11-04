# agent-mail

One stdio MCP server (`agent_mail.py`, Python 3.11 stdlib, one file) that Claude Code and Codex CLI both load. A Claude
orchestrator uses it to start Codex workers, message them by name, and be woken by their messages and turn results,
the way `claude --bg` + SendMessage work between Claude sessions. No Claude mod, no hooks, no Monitor.

## Tools

| Tool | What it does |
|---|---|
| `send(to, text)` | `to` is a Codex worker: the text is queued and its runner started. Any other name: written to that inbox. |
| `codex_start(name, cwd, prompt, model?, effort?, sandbox?)` | Starts a Codex worker. Returns at once; the thread id and every turn result arrive as mail from the worker. |
| `list()` | Workers (state, thread id, cwd, last message's first line) and this inbox's unread count. |
| `inbox()` | Claims and returns this instance's unread mail (for clients without push, e.g. Codex). |

Mail kinds: `message`, `turn_end` (the worker's last message), `error` (nonzero exit, `turn.failed`, or no last
message; the text names the exit code and the log). Silence never means success.

## Environment (nothing defaults to `~`)

- `AGENT_MAIL_DIR`: absolute mail root, **on local disk** (on the VDI: a folder under `/repo/$USER/`, never `/home`, which
  is a size-limited NFS share). Missing: every tool returns a clear error and nothing is created.
- `AGENT_MAIL_NAME`: who this instance is. Without it `send` still works (from `unknown`) but there is no inbox,
  no push and no `claude/channel` capability.
- `AGENT_MAIL_CODEX`: the codex binary (default `codex` on PATH). `CODEX_HOME`: passed through to every worker turn
  (on the VDI the real one is the folder `/repo/$USER/codex`; a stray `.codex` has no auth).
- `AGENT_MAIL_POLL`: inbox poll seconds (default 1).

## Register (once, by hand)

Claude Code, user scope. The name is not in the registration: it comes from the launching shell, so each session can
have its own.

```
claude mcp add --scope user agent-mail -e AGENT_MAIL_DIR=/repo/$USER/.agent-mail -e CODEX_HOME=/repo/$USER/codex \
  -- /usr/bin/python3.11 <this checkout>/agent-mail/agent_mail.py serve
```

Orchestrator launch (push needs the development-channel flag and a startup confirmation; `--bg` sessions drop it, so
a background Claude reads mail with `inbox` instead):

```
alias claude-orch='AGENT_MAIL_NAME=project-manager claude --dangerously-load-development-channels server:agent-mail'
```

Claude's variadic flags swallow a trailing prompt: put the prompt before `--mcp-config`, `--channels` and
`--dangerously-load-development-channels`.

Codex (only for an interactive Codex session you want an inbox in; Codex workers get the server injected per launch
with `-c`, so `config.toml` is never edited). Codex filters the environment it hands to MCP servers, so the name goes
into the registration:

```
codex mcp add agent-mail --env AGENT_MAIL_DIR=/repo/$USER/.agent-mail --env AGENT_MAIL_NAME=<name> \
  -- /usr/bin/python3.11 <this checkout>/agent-mail/agent_mail.py serve
```

## How it works

- Mail: one JSON file per message in `inbox/<name>/new/`, written atomically; a reader claims it by renaming it to
  `cur/`, so it is delivered exactly once even with two readers, and mail sent while no reader ran is delivered when
  one starts. Push to Claude is a `notifications/claude/channel` notification, sent only when the client is
  `claude-code` (Codex is `codex-mcp-client` and only gets the `inbox` tool).
- Workers: `workers/<name>.json` (state `starting | running | idle | failed`), `workers/<name>/queue/`, logs per turn
  in `workers/<name>/logs/`. A detached runner (`agent_mail.py run <name>`, one at a time per worker via `flock`)
  runs `codex exec --json`, later turns `codex exec resume <thread_id>`, several queued messages joined into one
  turn, each prefixed `From <sender>:`. It exits when the queue is empty; `send` starts it again.
- Every turn re-injects the agent-mail server with `-c mcp_servers.agent-mail.*` (command, args, name, dir, plus
  `CODEX_HOME` and `AGENT_MAIL_CODEX`, because Codex hands MCP servers a filtered env).

## Permissions: workers run unsandboxed by default

`codex_start` defaults to `sandbox: full`, i.e. `--dangerously-bypass-approvals-and-sandbox` on every turn, the same
trust level as our Claude workers (`bypassPermissions`). Reason: workers need network (uv, Artifactory) and a sandbox
that blocks it makes them useless. Under the bypass flag MCP tool calls are allowed. `read-only` and
`workspace-write` exist for the rare restricted worker (no network, writes only to the workspace); there agent-mail
is auto-approved with `default_tools_approval_mode="approve"`, because `codex exec` otherwise denies every MCP call.
The recorded mode is in the worker record and `list`.

**The sandbox is not what keeps a worker off lab equipment; the brief is.** Follow the repo's AGENTS.md live-equipment
rules in every brief: say which hosts, nodes and CLIs the worker must not touch, and when it must stop and ask.

## Tests

```
cd agent-mail && /usr/bin/python3.11 -B -m unittest -v test_agent_mail
```

A fake `codex`, a temp mail dir and temp HOME; nothing reaches the real codex, mail dir or MCP configs.

## Known limits

No per-turn timeout (a hung codex keeps its runner); no `retire` tool (delete `workers/<name>*` by hand); `codex queue`
is unreliable and unused.
