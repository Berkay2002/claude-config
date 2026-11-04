# Codex workers (orchestrate, via agent-mail)

Read this instead of the `claude --bg` steps in SKILL.md when a piece of work should go to a Codex session. It needs the
`agent-mail` MCP server (see `agent-mail/README.md` for registration) and an orchestrator started with
`AGENT_MAIL_NAME=<you> claude --dangerously-load-development-channels server:agent-mail`. Without that flag (a `--bg`
orchestrator) there is no push and the mail waits: call `inbox` after each step.

## 1. Brief
Same rules as step 3 of SKILL.md (goal, files, test that proves it, report format), plus:
- A Codex worker has no `SendMessage`. It reports by finishing its turn: the final message is mailed to you as
  `turn_end`. To talk mid-turn it can call agent-mail's `send` tool (its name is its worker name). Mail between
  workers runs only while you (their owner) are up: your agent-mail instance starts every runner.
- Workers run **unsandboxed by default** (`--dangerously-bypass-approvals-and-sandbox`), like our Claude workers, because
  they need network for uv/Artifactory. So the brief, not the sandbox, keeps them off lab equipment: copy the
  AGENTS.md live-equipment rules into it (hosts/nodes/CLIs not to touch; stop and ask before anything that could).
- Images: tell the worker to use its image generation tool and to copy the results into a folder named in the brief
  (Codex saves them under `$CODEX_HOME/generated_images/<thread id>/`). Reference screenshots: give their paths.
- Worktree: create it yourself (`git worktree add` under `<repo>/.claude/worktrees/<name>`, never a sibling folder) and
  pass it as `cwd`; Codex has no `EnterWorktree`.

## 2. Launch
`codex_start(name="<repo>-<piece>", cwd="<worktree>", prompt="<brief or path to it>", model="gpt-6.1-sol", effort="medium")`.
Model, effort and when to use Codex at all: "Codex workers" in the shared CLAUDE.md. Always pass both, and check
`list` → `codex_usage` (the Codex quota) before launching. `sandbox` stays unset unless the user wants a restricted worker (`read-only`,
`workspace-write`). It returns at once. You then get mail from the worker: `message` (thread id), then `turn_end` or
`error` after each turn. A `claude/channel` push shows as `← agent-mail: …`.

## 3. While it works
`send(to="<name>", text="…")` queues a message; messages that arrive during a turn are joined (each prefixed
`From <sender>:`) into the next turn. `list` shows state (`running`, `idle`, `failed`) and thread ids. Don't poll.
`error` mail means the turn failed (exit code, timeout or dead runner, and the log path in the text): read the log,
then `send` a corrected instruction (the thread resumes) or relaunch with a new name. After a dead-runner `error` the
interrupted messages run again on their own; the `turn_end` follows.

## 4. Report and merge
Same as step 6 of SKILL.md: `git log main..`, `git diff main...`, merge `--no-ff`, run checks. The worker's last message
is its report; ask for the report format in the brief.

## 5. Retire
When merged or abandoned: `retire(name)` removes the worker's agent-mail state (owner only). It refuses while a turn runs ("retry after its
`turn_end`") or mail is undelivered (`force=true` discards it), and it keeps the Codex thread unless you pass `delete_thread=true`
(keep it while the history may still matter for review). The worktree is removed with the `tidy` skill.

## Traps (all checked on the VDI, codex 0.166)
- `codex exec resume` takes its flags before the thread id and rejects `-C`, `-s` and `--add-dir`; agent-mail handles
  this (cwd via the process, sandbox via `-c sandbox_mode=…`). `codex queue` is unreliable: never use it.
- `codex exec` denies MCP tool calls ("approval policy is never") under a restricted sandbox unless
  `mcp_servers.<n>.default_tools_approval_mode="approve"` is set; under the bypass flag they run.
- Codex gives MCP servers a filtered environment: `CODEX_HOME` and `AGENT_MAIL_*` must be injected (agent-mail does).
- This VDI's `python3` is 3.6: use `/usr/bin/python3.11`. Claude runs with `HOME=/repo/<user>`, the shell and Codex with
  `HOME=/home/<user>`: never default a path to `~`. The real `CODEX_HOME` is the folder `/repo/<user>/codex`; `.codex`
  there is a stray home without auth. Keep `AGENT_MAIL_DIR` on local disk (`/repo/<user>/…`), never under `/home` (NFS, size-limited).
- Never edit Codex's `config.toml` for a worker: the same `CODEX_HOME` runs the user's daily digest cron.
- Meta keys in channel notifications must be plain identifiers; Claude's variadic flags swallow a trailing prompt.
