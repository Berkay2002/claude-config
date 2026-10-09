---
name: orchestrate
description: Run multi-part feature work as parallel background Claude Code sessions in git worktrees, coordinated from this session over cross-session messaging (brief, launch, monitor, review, merge, deploy, clean up). Use when the user asks to delegate/parallelize work to other sessions, or when a task splits into independent pieces that each take hours.
---

# Orchestrate background sessions

You are the orchestrator. You plan, brief, review, merge, deploy and talk to the user. You do not
implement the features yourself; that is the point (it keeps this session's context small).

## 1. Plan the split
- 2-4 workers per repo. Split by **file ownership** (each worker owns new files; shared files get
  only small additive edits). If two pieces need the same files or depend on each other in order,
  they belong to one worker.
- Define contracts up front (interfaces, config keys, route prefixes) so workers don't need each other.
  If a worker needs another's type before it exists, it declares a stub exactly as in the brief and you dedupe at merge.
- Decide the merge order (the one that defines contracts first).

## 2. Prepare the repo (once per repo)
- `.gitignore`: `.claude/worktrees/`, dev config, dev data dirs.
- `.worktreeinclude`: gitignored files every worktree needs (e.g. `config.dev.json`, `.env`).
- Make sure `main` is committed; workers branch from it (no remote → local HEAD).

## 3. Write the briefs
Put them outside the worktrees, e.g. `<repo>/.claude/briefs/` (gitignored) or next to the repo:
- `BRIEF.md` (shared): orchestrator name (`ListAgents` shows this session's name; `/rename` if it's generic),
  codebase map, rules, contracts, reporting format. Rules to always include:
  work only in your worktree/branch, commit there, never merge/rebase/push main; no deploys, no
  touching live services, prod config, DNS, tunnels or accounts; own dev ports + data dir;
  verify (`check`/`build`/tests, screenshots for UI, saved as PNGs to a folder named in the brief) before reporting; ask one concrete question
  when blocked and keep working meanwhile; **clean up before reporting**: close everything you started
  (dev servers, browsers/Playwright, apps, background shells, watchers, test sessions), check with `ps`
  that nothing of yours still holds memory/CPU, and list what you closed; report ≤40 lines: branch, commits, what works + how
  verified, what doesn't, config/secrets the deploy needs, user actions.
- `task-<name>.md` per worker: goal, scope, acceptance criteria, ports, files it owns.
- Never put secrets in briefs or messages. Credentials are created by you and written straight into
  the worker's gitignored dev config.

## 4. Launch
Per worker, from the repo root (trusted folder):
```bash
claude --bg -n <repo>-<name> --worktree <name> --model sonnet --effort high --advisor opus --permission-mode bypassPermissions \
  "You are <repo>-<name>. Read <path>/BRIEF.md and <path>/task-<name>.md and do the task. Consult the advisor once after your first reads and once before you report done."
```
Pick `--model`/`--effort` per the routing ladders in my CLAUDE.md (default sonnet@high; always pass `--model`, or the
worker inherits Opus). Opus at xhigh/max needs my explicit OK. Keep `--advisor opus` on every non-Opus worker; drop it
for Opus workers, and drop it if this machine rejects the flag (the advisor is experimental).
(Verified 2026-09-27: `--bg` + `--worktree` works; worktree at `.claude/worktrees/<name>`, branch `worktree-<name>`, locked while running.
If it misbehaves, fall back to `git worktree add ../<repo>-worktrees/<name> -b feat/<name>`,
trust the folder in `~/.claude.json` `projects.<path>.hasTrustDialogAccepted`, and launch there.)
Check with `claude agents --json`. Then subscribe once per worker:
`SendMessage({to: "<repo>-<name>", message: "", notify_when_idle: true})`. Never poll.

## 5. While they work
- Answer questions promptly and concretely. Re-subscribe after each idle notice if the worker isn't done.
- Idle notices can be stale (a message you sent may already have woken the worker); check
  `claude agents --json` before acting on one.
- Handle credentials/outward-facing steps yourself (browser-use for dashboards), then tell the worker.

## 6. Merge each report (in the planned order)
0. For UI work: open the worker's screenshots (saved as PNGs to the folder named in the brief, since messages are
   text-only) and send back anything that looks off before merging.
1. `git -C <worktree> log main..`, `git diff main...` (read shared-file edits closely), look for
   secrets, unauthenticated routes, destructive commands.
2. Merge `--no-ff` into main, dedupe stubs, wire subsystems together, run check/build/tests on main.
3. Fix small issues yourself; send bigger ones back to the worker.
4. Deploy, verify the live result (browser), update the handoff/CLAUDE.md notes.
5. Retire the worker once it's merged (or abandoned) and you won't message it again:
   `claude stop <id>`; `git worktree unlock` + `git worktree remove <path>`; `git branch -d <branch>`;
   then `claude rm <id>` to delete the session (it refuses while unpushed commits exist in the worktree,
   so remove the worktree first). Keep only workers still working or awaiting review.
   Check for leftovers (their dev ports, browsers, dev processes) and stop them.
6. Tell the remaining workers what landed on main if it affects them.

## Housekeeping (whenever a round settles, and before reporting)
`claude agents --json`: `claude rm` sessions *you* created that are done (never others' sessions; mention
those to the user instead). Idle sessions still hold hundreds of MB each.

## 7. Report to the user
Short: what merged and deployed, how it was verified, what the user must do, who is still working.
