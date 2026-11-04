# Global rules for Codex (from the claude-config repo; edit the repo, not the installed link)

These apply in every repo. A repo's own AGENTS.md adds to them and wins where they disagree.

## Attribution
Never add AI attribution to anything that leaves this machine or lands in a repo: no "Generated with Codex" (or
similar) lines in merge request or PR descriptions, issues or comments, no `Co-Authored-By` trailers for Codex,
OpenAI or any model in commit messages, no "written by AI" notes in code, docs or commit messages.

## Git
- Commit or push only when asked. If you are on the default branch, branch first.
- Commit messages follow the repo's own convention (its AGENTS.md, or `git log`).
- Never force-push, rebase or merge a shared branch (`main`, `master`) unless asked.
- Worktrees only under `<repo>/.claude/worktrees/<name>`: never sibling folders next to a repo, never checkouts in
  `/tmp`. Never edit a tracked `.gitignore` for personal ignores; use `.git/info/exclude`.

## Machine notes
If `$CODEX_HOME/AGENTS.local.md` exists, read it before you start: it holds this machine's notes (paths,
toolchains, what not to touch), linked from the gitignored `local/AGENTS.md` of claude-config.

## Disk
Where home is a size-limited network share, keep work folders, caches and scratch data on local disk (the brief or the
repo's rules say where).

## As a worker
When another session started you (an orchestrator, through agent-mail):
- Work only in your worktree and branch. Never merge into, rebase or push the default branch: the orchestrator merges.
- No deploys, no live services, no shared data, no lab equipment unless your brief says so. You run without a
  sandbox, so your brief and the repo's AGENTS.md are what keep you off them.
- Blocked: send the orchestrator one concrete question with agent-mail `send` and keep working on what you can.
- Before reporting, close everything you started (servers, browsers, background shells, watchers) and check with `ps`.
- Report in at most 40 lines: branch, commits, what works and how you verified it, what doesn't, what the user must do.
