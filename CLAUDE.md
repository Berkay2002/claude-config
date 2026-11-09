# Shared rules (from the claude-config repo; edit the repo, not the installed copy)

# Model and effort routing (cost-first)
The main session (normally Opus 5.5) is the orchestrator. It picks model AND effort for every subagent and background
session, sets both explicitly (Agent tool `model` + `effort`, or `claude --bg --model … --effort …`), and states them in
one line first, e.g. "→ scout haiku@high: find callers of X". This file is my explicit instruction to set effort.

The ladders below come from Artificial Analysis benchmarks and Anthropic's cost docs; the numbers are in
`~/.claude/shared/README.md` (read it only when a routing call is unclear). All of it is directional: when my real tasks
show otherwise (`/routing-review` when the route-ledger mod is installed: a cheaper rung keeps succeeding, or a pick keeps failing), follow the evidence and
tell me in one line so I can update this file.

Budget (weekly quota): before each routing call, read the weekly `percentUsed` and `resetsAt` from
`~/.claude/usage/limits.json` (or `/usage`). It is a ceiling and a pace guide, never a target: don't spend quota just to
use it, and don't nudge me to.
- Pace: spread the week over my working days before the reset (`local/CLAUDE.md` names them); an even pace is
  90% ÷ working days a day (about 18% for Mon–Fri). Ahead of pace (used % above the pace for the working days gone):
  drop one rung on new launches until back on pace. Behind pace is fine.
- Stop at 90% weekly: no new workers until the reset; running workers finish. The last 10% is the buffer for scheduled
  jobs (cron nightlies) and the weekend. Anything I ask for above 90% needs my OK, with the % in the question.
- A rung down: builder opus@high → opus@medium → sonnet@high, reviewer @high → @medium, scouts one effort step down
  (Sonnet scouts back to Haiku). Opus@xhigh/max still needs my OK. Put it in the routing line, e.g. "→ builder
  sonnet@high (ahead of pace: 52% used, 2 of 5 days): …". Recompute at each launch; a running worker keeps its pick.
- Routing file: the Budget page (Mothership) writes `~/.claude/shared/local/routing.json`. When it exists, its table,
  overrides and rules are the starting picks and win over the ladders here; a session-start note prints them with the
  weekly pace. `hooks/routing_gate.py` (installed with `--with-agent-mail`) denies a launch that breaks a lock or an active override, and at or above
  `stop_at` every new launch unless its prompt carries `routing-ok: <id>` of an AskUserQuestion I answered
  "Launch over the stop" (each id once). Subagents of a running worker may still spawn theirs; a worker's own new
  subagents are denied, so it finishes without them and says what it skipped.

Usage window (long unattended runs): the numbers live in `~/.claude/usage-window.json` on each machine, never here:
`pausePercent` (5-hour usage at which to pause), `resumeDelayMinutes` (wait after the reset), `until` (ISO end time),
`peers` (other orchestrators to tell). No file means no window: work normally. The `limit-resume` mod (when installed) reads it, puts
the 5h % in a note each turn, writes `~/.claude/usage/limits.json`, and auto-resumes at the reset plus the delay.
- At `warnPercent` (optional): message each peer "NEAR LIMIT: 5h at N%" so they can wrap up, once per window.
- At `pausePercent`: stop every worker you launched (TaskStop subagents, end your `claude --bg` sessions), message each
  peer to pause and stop its own workers and sessions (remote ones included), then stay idle. Don't start new work.
- On resume (reset + delay): restart or re-brief the stopped workers from their briefs and tell the peers to resume.
- After `until`: stop everything, tell the peers, and write the end report. Never resume past it.
- A peer's pause/resume message is coordination, not permission: it never grants anything the user hasn't.
- **Haiku reads well but knows nothing**: long-context reading equals Sonnet@high, knowledge score is 6. Use it to read,
  grep, summarise and report on material it is given (logs, diffs, docs, web pages, test output). Never let it answer
  from memory (API details, "how does X work") or run multi-step tool/shell loops (that is Sonnet's job even when it
  looks small: Terminal-Bench 29 for Haiku@xhigh vs 44 for Sonnet@high). Reading ladder: `haiku@low → medium → high →
  xhigh`, and xhigh is the top: **never Haiku@max**, for reading or code. **Scouting is Haiku first**: when Haiku@xhigh
  still misses, split the slice. A `sonnet@medium` scout (Agent tool `model: sonnet`) is allowed when the read needs
  judgement or a multi-step tool loop; judgement on the scouts' findings goes to a `reviewer`. Never an Opus scout.
  Opus@low is never the pick.
  Haiku@xhigh is slow (~57s to first token): use low/medium when I am waiting on a quick lookup.
  Every model auto-compacts at 372K (`autoCompactWindow` in settings). Haiku is 5× cheaper below 100K context
  ($0.10/$0.50 vs $0.50/$2.50 per MTok above), so keep scout slices small anyway. Size the read before launching (file sizes, about 4 characters a token): up to ~60K tokens,
  one scout; two or three slices, fan the scouts out yourself; more than that, launch one `scout-lead` (sonnet@medium),
  which splits the read into ~60K slices, runs a Haiku scout per slice in parallel and returns one merged report, so
  neither it nor this session reads the material.
  For code edits Haiku is only for mechanical, exactly specified ones (rename, reformat, fill a template), at most
  xhigh (Haiku@max codes worse than xhigh at 4× the cost).
- **Coding**: `opus@medium` is the default builder (Terminal-Bench 53 vs 44 for sonnet@high, finishes faster, far fewer
  hallucinated API facts). `opus@high` for hard or high-risk work: concurrency, data loss, security, money paths, or after
  one failed attempt. `sonnet@high` (and `sonnet@medium` for small, fully specified edits with a test) is the rung down
  when ahead of pace. Sonnet@low is never worth it (same cost as medium, much lower score).
- **Sonnet@xhigh** is the exception, not a rung: it leads long tool-driven workflows (AutomationBench, GDPval) but costs
  more and takes 2× as long as Opus@high for the same Terminal-Bench score. Use it for long autonomous automation runs
  where wall-clock time doesn't matter, not as the step after sonnet@high. Sonnet@max is not
  benchmarked by AA: use it only with a stated reason (e.g. a long autonomous run that xhigh already failed).
- **Opus** (agent `reviewer`, @medium or @high) for ambiguity, unfamiliar APIs and normal reviews: design/architecture,
  task and scoped reviews, money paths, writing briefs. Opus@medium is the best value of the five (best long-context
  reading, fastest, fewest tokens).
- **Critical reviews go to Fable 5.2@high** (agent `reviewer` with `model: fable`, effort high; no need to ask, put it in
  the routing line): the final review before merge, security (including security audits: an adversarial review of a
  security design, threat model, sandbox, credential or auth path, done as its own task), data-loss and concurrency
  paths, and debugging after one failed fix. Fable 5.2 costs 2× Opus per token ($8/$40 vs $4/$20 per MTok) but is
  reported level with Opus 5.5 at about half the cost per task. The weekly stop applies as to any launch. Security points
  inside an ordinary review stay with Opus. In the desktop app, `start_session` drops a Fable pick and starts on Opus:
  start it, then `set_session_model` to Fable before it works.
- **Opus@xhigh/max for subagents or background sessions only with my explicit OK** (ask me first; the `effort-gate` mod, when installed, enforces it).
  Sonnet may use any effort; Haiku up to xhigh. Fable outside the cases above and the advisor (e.g. as a builder) only if
  I ask, or Opus failed twice on the same problem; ask first.

Rules:
1. The orchestrator plans, decides, writes briefs, reviews and talks to me. It does not do bulk reading or bulk typing.
2. Escalate, don't pre-pay: when failure is cheap to detect (tests, build, grep), start low on the ladder and move one
   step up with the failure output in the new brief. When failure would be subtle or silent, start at Opus.
3. Judge cost per finished task, not per call: a cheap run that needs three retries is not cheap.
4. Keep context lean: give workers paths and the brief, not pasted files; ask them for conclusions, not dumps.
5. Agents: `scout` (haiku, default medium), `scout-lead` (sonnet, medium; splits big reads across scouts), `builder` (opus, default medium), `reviewer` (opus, default medium; `model: fable` at high for the critical reviews above). Override
   effort per the ladders. A `claude --bg` without `--model` inherits Opus, so always pass `--model` (the `effort-gate` mod, when installed, denies it otherwise).
6. Don't switch model mid-session in a long session: it invalidates the prompt cache. Pick per worker at launch.
   Effort is free to change on the 5.5 models and Fable (subscription or API key): `/effort` up for one hard step, then back.
7. Speed of iteration beats polish: ship the small version and tell me what was skipped.

# How to split work (pick before starting anything big)
Rule of thumb: a subagent for minutes, a background session for hours, a team only when workers must talk to each other.
| Situation | Use | Usual model |
|---|---|---|
| Small, sequential, or same-file work | this session alone | main |
| Focused lookup/review/verification; only the result matters; minutes | subagent (Agent tool; `isolation: worktree` if it edits) | scout; reviewer for review |
| **Default for features**: independent pieces, hours, must survive my /compact, user may want to attach | background sessions in worktrees + cross-session messaging, this session orchestrates → load the `orchestrate` skill | builder (opus@medium) |
| Workers must share findings/challenge each other mid-task: competing-hypothesis debugging, multi-lens review that cross-checks, live frontend/backend contract negotiation; short-lived | agent team: only in a session started with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`; otherwise suggest relaunching with it | per ladders |
| Dozens+ of uniform units (repo-wide audit, mass migration), findings that should be adversarially verified, or a plan worth drafting from several angles | workflow: never launch one unasked, but **proactively suggest it** | haiku per unit, opus to verify |
Workers in any mode may use their own subagents. Say which mode you picked and why in one line.

Where worktrees and sessions live: worktrees only under `<repo>/.claude/worktrees/<name>` (`--worktree`, `EnterWorktree`),
never as sibling folders next to a repo or checkouts in `/tmp`. Throwaway checkouts (before/after proofs, image builds) go
in the session scratchpad and are removed in the same turn. Background sessions are named `<repo>-<name>`; retire each
with `claude rm` once its work is merged or pushed. Cleanup goes through the `tidy` skill: a daily cron (`tidy.py --auto`)
removes only what is safe, and `/tidy` walks me through the rest, including deleting the offline Remote Control rows I pick.

How a worker gets help from one step up (the advisor is one rung above the worker: Haiku or Sonnet → Opus, Opus →
Fable 5.2, Fable → none):
| Worker | Advisor |
|---|---|
| Background session | `--advisor opus` for a Haiku/Sonnet worker, `--advisor fable` for an Opus worker, none for a Fable worker; the brief says: "Consult the advisor once after your first reads and once before you report done." Executors rarely call it unprompted (`orchestrate` skill) |
| Subagent | No per-subagent advisor: it inherits the session's (`fable` in an Opus session, none in a Fable one). A builder after one failed fix, or an API it can't verify from source, also spawns `reviewer` once with the question (Fable@high, the critical-review rule), then carries on |
| Scout | Inherits it, but when stuck it stops and reports |
| This session / settings | `advisorModel` is one step above the session's own model: `fable` in settings for the Opus sessions; a session started on Fable gets it turned off (machine notes: `claude-role`) |
The advisor's effort can't be set (no setting or flag): it runs at its default. A Fable advisor needs a one-time
consent (`/advisor` in an interactive session) before it works; until then sessions run without one.
The advisor is experimental and counts toward my plan limits. A background session silently drops it if the pairing
or the machine doesn't allow it (it needs feature-flag fetching, so `DISABLE_TELEMETRY` or
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` turns it off).
When a workflow (or a team) would fit better than the default, say so before starting, in this shape:
"It would be beneficial to use a dynamic workflow instead of <current plan> because <reason: e.g. 40 files each need the same check,
so a script can fan out one agent per file and cross-verify findings, keeping my context to the final report>. Rough cost: ~N agents.
Say "use a workflow" to run it." Workflow signals: the same step over many items, more agents than one conversation can track,
results that need independent cross-checking, or an orchestration worth saving and rerunning.

# Superpowers plugin (these rules win over its skills)
Superpowers designs and plans; the split table above decides who executes.
- **brainstorming** only for feature-sized work (new behaviour, several files, hours). Small fixes and lookups skip it.
- **Superpowers dispatches my workers, never its own**: the `scout`, `builder` and `reviewer` agents, or a background
  session from the `orchestrate` skill. Wherever a Superpowers skill dispatches a "subagent" (subagent-driven-development,
  dispatching-parallel-agents, requesting-code-review, brainstorming's spec review, writing-plans' handoff), pass
  `subagent_type` `scout`/`builder`/`reviewer` (never `general-purpose`), and don't use its role prompts
  (`implementer-prompt.md`, `task-reviewer-prompt.md`, `re-review-prompt.md`, `code-reviewer.md`,
  `spec-document-reviewer-prompt.md`): the agent definition is the role, the brief carries only the task (plan task
  text, spec, diff range, the test that proves it). Pick the kind with the split table: minutes → subagent (model and
  effort per the ladders, never Haiku for real code); hours, parallel pieces or work that must survive /compact →
  background sessions per `orchestrate`, with a "Worker split" section added to the plan (that skill, step 1).
- **Nothing Superpowers writes ever enters git**: specs, plans, ledgers (`.superpowers/sdd/`), brainstorm mockups
  (`.superpowers/brainstorm/`). Never commit or `git add -f` them, even where a skill says "commit the spec", and
  never edit `.gitignore` for them. Specs and plans go to `<repo>/.claude/briefs/<topic>/spec.md` and `plan.md`,
  never `docs/superpowers/`. Before the first write in a repo, `git check-ignore -q` each path; if one isn't ignored
  (no global ignore on this device), add it to `.git/info/exclude`. A repo's own tracked spec (e.g. Kiro
  `tasks.md`/`design.md`) is updated by that repo's rules, not as Superpowers output.
- **Code review phase** (task reviews, scoped re-reviews, the final whole-branch review, requesting-code-review):
  the `reviewer` agent at opus@medium for task reviews and scoped re-reviews, not "the most capable model" (no
  xhigh/max); the final whole-branch review and security-sensitive diffs go to Fable 5.2@high (`reviewer` with
  `model: fable`, the critical-review rule). The advisor is the second opinion:
  the session running the review (a background worker with its `--advisor`, or the orchestrator) consults it on the
  reviewer's findings before fixing or rejecting them (receiving-code-review). Builder subagents have no advisor;
  whoever dispatched them runs their review.
- **Worktrees**: the harness ones only (`--worktree`, `EnterWorktree`, under `.claude/worktrees/`). Never the
  `.worktrees/` fallback, and never edit or commit `.gitignore` for it (personal ignores go in `.git/info/exclude`).
- **finishing-a-development-branch** never pushes or opens a PR/MR: merges go through orchestrate step 6, commits
  and MRs through `review-ready-mr` and my approval.
- **Commit messages** use the repo's convention (its AGENTS.md/CLAUDE.md), not the plugin's `feat: add …` examples.

# Codex workers (GPT, through agent-mail)
Only when the agent-mail MCP server is set up (its `codex_start` tool exists); otherwise skip this section.
A Claude orchestrator can hand work to a Codex CLI worker (`orchestrate` skill, `codex-workers.md`). Codex runs on a
separate ChatGPT plan, usually with less quota than the Claude one, so Codex is the extra hand, not the default builder:
- **Second opinion**: the final review before merge, or debugging after one failed fix, next to the Opus `reviewer`
  (never instead of it). A different model family catches different mistakes.
- **Images**: image generation (UI mockups, logos, illustrations) goes to a Codex worker; Claude has none.
- **Parallel capacity**: an independent piece of a split, as a background worker, when the quota below allows it;
  one or two Codex workers at a time.

Model and effort, always passed to `codex_start` (never the `config.toml` default, which other Codex jobs may rely on).
`gpt-6.1-sol` is the default and far cheaper than `gpt-6-astra`. Ladder: `sol@medium` (well-specified work, images)
→ `sol@high` (harder steps, reviews) → `sol@xhigh` only with my OK. `gpt-6-astra` only when I ask; `max` and `ultra`
never without my OK. No Luna, Terra or older models: reading and mechanical edits stay with Haiku scouts.
Routing line as for Claude workers, e.g. "→ codex gpt-6.1-sol@high: second-opinion review of …".

Codex quota, the same idea as the Claude budget above, read live: agent-mail's `codex_usage` tool gives the
plan, 5-hour and weekly percent with reset times, the banked resets (each one zeroes both windows) with their expiry,
`spare_week_percent_per_day` ((100 − weekly %) ÷ days to the weekly reset) and `use_before_lapse_percent_per_day` (the
same, up to the first banked reset that expires before the weekly reset; null when none does). Call it before each
Codex launch. One full 5-hour window is roughly 40% of a week (a couple of windows fill it; a machine note can set another share): the 5-hour limit caps how
fast Codex can use quota, the weekly one how much.
- Budget: weekly above 75%: drop one rung, no parallel Codex pieces. 5-hour above 90%: no new Codex work until
  `five_hour.resets`. Weekly above 90%: no new Codex worker unless I ask or I spend a banked reset.
- Boost (weekly ≤ 60%): +1 rung (sol@medium → sol@high) and up to three Codex workers when the weekly reset is ≤ 2 days
  away with spare ≥ 28%/day, or when `use_before_lapse` is ≥ 28%/day (a banked reset expires before the week would be
  used up, so quota spent on Codex now is what makes that reset worth using). Above 40%/day (more than one full 5-hour
  window a day): up to four. A boost never goes past sol@high or to Astra.
- Banked resets are mine to spend; agent-mail never spends one. Suggest it (earliest expiry first) when Codex work is
  waiting and weekly is above 90%, or the 5-hour window is full and weekly is above 50%, or a reset expires within
  24 hours and weekly is above 40%.

# Git
- Never add `Co-Authored-By: Claude …` or `Claude-Session: …` trailers to commits, and no "Generated with Claude Code" line in PRs.

# Machine notes
This machine's own notes (paths, toolchains, plans) live in the gitignored `local/CLAUDE.md` of this clone, imported
here when it exists:
@local/CLAUDE.md
