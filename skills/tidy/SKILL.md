---
name: tidy
description: Clean up git worktrees, finished background sessions and offline Remote Control rows without losing work. Use whenever the user says "clean up worktrees", "tidy", "/tidy", "too many agents", "too many sessions", "disk full", "/repo is full", "ListAgents shows old sessions", "offline Remote Control rows", or asks what is safe to delete; and after every orchestrator round (orchestrate step 6.5 / Housekeeping), even when nobody asked.
---

# Tidy worktrees and sessions

One script does the checking: `/usr/bin/python3.11 ~/.claude/skills/tidy/tidy.py` (stdlib only; the system
`python3` is too old). It never deletes a local branch itself (`claude rm` may drop a session's own worktree branch,
so it only runs when that branch has nothing unpushed), never touches a repo's main working tree, and treats any
doubt as Keep or Ask. A daily cron runs `--auto`, which only does the Safe part; this skill is for the rest.

## 1. Report
`tidy.py report` (add `--fetch` to refresh remote-tracking refs first). Every worktree under the roots in
`~/.claude/tidy.json` and every background session gets a class and a reason:
- **keep**: locked, on the keep list, a process or live session inside it, touched within `min_age_hours`, or a git
  error. Never removed. Mention a Keep only when it looks stale (e.g. locked by a dead pid) and ask before unlocking.
- **ask**: tracked changes, untracked files, unpushed commits (also ones only in the worktree's HEAD reflog), nested
  git repos, or the directory is missing.
- **safe**: clean, its HEAD on a remote or the default branch, old enough, not in use.
- Sessions: `rm` (finished, worktree safe or gone, branch has nothing unpushed), `ask`, `keep` (finished
  recently), `live` (report only).

Show the user the summary line and the ask items, not the whole table.

## 2. Apply Safe
`tidy.py --apply` removes the Safe worktrees (rechecking each right before) and runs `claude rm <id>` for the `rm`
sessions. It aborts and removes nothing if `claude agents` or `/proc` can't be read. Tell the user what it freed.

## 3. Walk through Ask
One `AskUserQuestion` per batch of up to four Ask worktrees, each with: **Back up and remove** / **Keep**. Put the
reason (e.g. "2 untracked files, 1 unpushed commit on detached HEAD") in the question.
- Back up and remove: `tidy.py --backup <path>`. It tags detached unpushed commits as `backup/wt-<name>` (reflog-only
  ones as `backup/wt-<name>-reflog-<sha>`), saves
  tracked and untracked changes to `~/worktree-backups/<repo>-<name>.patch`, then removes the worktree. Commits on a
  branch stay on the branch. It refuses Keep items and worktrees with nested repos; report those to the user.
- `ask` sessions: name the reason. If the user wants one gone, `claude rm <id>` (it refuses unpushed or dirty
  worktrees itself). Never pass `--discard-unpushed` or `--force-remove-worktree` unless the user says so for that id.

## 4. Offline Remote Control rows
`tidy.py offline` lists them (last active, id, title, claude.ai/code link); rows that belong to a local session are
marked. Ask the user which to remove (multiSelect, oldest first), then `tidy.py offline --delete <id> ...` (gone for
good) or `--archive` if they'd rather keep them in claude.ai/code. It refuses ids that aren't offline rows right now or
that belong to a live local session, and checks the list again afterwards. Never delete or archive rows the user
didn't pick; `--auto` never touches them.

## Notes
- Config: `~/.claude/tidy.json` `{"roots": [...], "keep": [...], "min_age_hours": 24}`; created with defaults on first run.
- Log of every `--apply`/`--auto` run: `~/.claude/tidy.log`.
- Tests: `cd ~/.claude/skills/tidy && /usr/bin/python3.11 -m unittest -v test_tidy`.
