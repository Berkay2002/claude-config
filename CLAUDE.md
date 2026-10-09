# Shared rules (from the claude-config repo; edit the repo, not the installed copy)

# Model and effort routing (cost-first)
The main session (normally Opus 5.5) is the orchestrator. It picks model AND effort for every subagent and background
session, sets both explicitly (Agent tool `model` + `effort`, or `claude --bg --model … --effort …`), and states them in
one line first, e.g. "→ scout haiku@high: find callers of X". This file is my explicit instruction to set effort.

The ladders below come from Artificial Analysis benchmarks and Anthropic's cost docs; the numbers are in
`~/.claude/shared/README.md` (read it only when a routing call is unclear). All of it is directional: when my real tasks
show otherwise (`/routing-review`: a cheaper rung keeps succeeding, or a pick keeps failing), follow the evidence and
tell me in one line so I can update this file.

Budget: when my weekly usage (`/usage`) is above 75%, drop one rung on every ladder; above 90%, ask me before any Opus worker.
- **Haiku reads well but knows nothing**: long-context reading equals Sonnet@high, knowledge score is 6. Use it to read,
  grep, summarise and report on material it is given (logs, diffs, docs, web pages, test output). Never let it answer
  from memory (API details, "how does X work") or run multi-step tool/shell loops (that is Sonnet's job even when it
  looks small: Terminal-Bench 29 for Haiku@xhigh vs 44 for Sonnet@high). Reading ladder: `haiku@low → medium → high →
  xhigh → max`, then `opus@medium`. Haiku@max matches Opus@low at 40% of the cost, so Opus@low is never the pick.
  Haiku@xhigh is slow (~57s to first token): use low/medium when I am waiting on a quick lookup.
  Haiku is 5× cheaper below 100K context ($0.10/$0.50 vs $0.50/$2.50 per MTok above), so it auto-compacts at 100K
  (Opus/Sonnet at 300K): give each scout a slice that fits well under 100K and fan out several scouts for bigger reads.
  For code edits Haiku is only for mechanical, exactly specified ones (rename, reformat, fill a template), at most
  xhigh: **never Haiku@max for code** (lower coding score than xhigh at 4× the cost).
- **Coding**: `sonnet@high` is the default builder (cheapest real coder, fastest responses). Escalate to `opus@medium`:
  ~1.5× the cost, Terminal-Bench 53 vs 44, finishes faster, and far fewer hallucinated API facts. Then `opus@high`.
  `sonnet@medium` is the cheap rung below it (about half the cost, clearly lower score): small, fully specified edits
  with a test. A background `sonnet@medium` with `--advisor opus` is worth trying for well-specified multi-file work
  (Anthropic: about Sonnet@high quality for less); check `/routing-review` before making it a habit.
  Sonnet@low is never worth it (same cost as medium, much lower score).
- **Sonnet@xhigh** is the exception, not a rung: it leads long tool-driven workflows (AutomationBench, GDPval) but costs
  more and takes 2× as long as Opus@high for the same Terminal-Bench score. Use it for long autonomous automation runs
  where wall-clock time doesn't matter, not as the step after sonnet@high. Sonnet@max is not
  benchmarked by AA: use it only with a stated reason (e.g. a long autonomous run that xhigh already failed).
- **Opus** (agent `reviewer`, @medium or @high) for ambiguity, unfamiliar APIs and high cost of error: design/architecture,
  debugging after one failed fix, security, concurrency, data-loss or money paths, final review before merge, writing
  briefs. Opus@medium is the best value of the five (best long-context reading, fastest, fewest tokens).
- **Opus@xhigh/max for subagents or background sessions only with my explicit OK** (the `effort-gate` mod asks me).
  Sonnet and Haiku may use any effort. **Fable 5.1** only if I ask, or Opus failed twice on the same problem; ask first.

Rules:
1. The orchestrator plans, decides, writes briefs, reviews and talks to me. It does not do bulk reading or bulk typing.
2. Escalate, don't pre-pay: when failure is cheap to detect (tests, build, grep), start low on the ladder and move one
   step up with the failure output in the new brief. When failure would be subtle or silent, start at Opus.
3. Judge cost per finished task, not per call: a cheap run that needs three retries is not cheap.
4. Keep context lean: give workers paths and the brief, not pasted files; ask them for conclusions, not dumps.
5. Agents: `scout` (haiku, default medium), `builder` (sonnet, default high), `reviewer` (opus, default medium; high for security and final review). Override
   effort per the ladders. A `claude --bg` without `--model` inherits Opus, so always pass `--model` (`effort-gate` denies it otherwise).
6. Don't switch model mid-session in a long session: it invalidates the prompt cache. Pick per worker at launch.
   Effort is free to change on the 5.5 models and Fable (subscription or API key): `/effort` up for one hard step, then back.
7. Speed of iteration beats polish: ship the small version and tell me what was skipped.

# How to split work (pick before starting anything big)
Rule of thumb: a subagent for minutes, a background session for hours, a team only when workers must talk to each other.
| Situation | Use | Usual model |
|---|---|---|
| Small, sequential, or same-file work | this session alone | main |
| Focused lookup/review/verification; only the result matters; minutes | subagent (Agent tool; `isolation: worktree` if it edits) | scout; reviewer for review |
| **Default for features**: independent pieces, hours, must survive my /compact, user may want to attach | background sessions in worktrees + cross-session messaging, this session orchestrates → load the `orchestrate` skill | builder (sonnet@high) |
| Workers must share findings/challenge each other mid-task: competing-hypothesis debugging, multi-lens review that cross-checks, live frontend/backend contract negotiation; short-lived | agent team: only in a session started with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`; otherwise suggest relaunching with it | per ladders |
| Dozens+ of uniform units (repo-wide audit, mass migration), findings that should be adversarially verified, or a plan worth drafting from several angles | workflow: never launch one unasked, but **proactively suggest it** | haiku per unit, opus to verify |
Workers in any mode may use their own subagents. Say which mode you picked and why in one line.

How a cheaper worker gets Opus help (the advisor makes Sonnet/Haiku perform better on hard steps):
| Worker | Opus help |
|---|---|
| Background session, non-Opus model | `--advisor opus` on the launch line, and the brief says: "Consult the advisor once after your first reads and once before you report done." Executors rarely call it unprompted (`orchestrate` skill) |
| Builder subagent | No advisor key for subagents: after one failed fix, or an API it can't verify from source, it spawns `reviewer` once with the question, then carries on |
| Scout | None: when stuck it stops and reports |
| This session / settings | `advisorModel` stays unset: subagents inherit the session advisor, so it would add Opus calls to every Haiku scout |
The advisor is experimental and counts toward my plan limits. A background session silently drops it if the pairing
or the machine doesn't allow it (it needs feature-flag fetching, so `DISABLE_TELEMETRY` or
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` turns it off).
When a workflow (or a team) would fit better than the default, say so before starting, in this shape:
"It would be beneficial to use a dynamic workflow instead of <current plan> because <reason: e.g. 40 files each need the same check,
so a script can fan out one agent per file and cross-verify findings, keeping my context to the final report>. Rough cost: ~N agents.
Say "use a workflow" to run it." Workflow signals: the same step over many items, more agents than one conversation can track,
results that need independent cross-checking, or an orchestration worth saving and rerunning.

# Git
- Never add `Co-Authored-By: Claude …` or `Claude-Session: …` trailers to commits, and no "Generated with Claude Code" line in PRs.
