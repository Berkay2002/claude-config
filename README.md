<p align="center"><img src="assets/logo.svg" width="112" alt=""></p>
<h1 align="center">claude-config</h1>

A cost-first Claude Code setup: one Opus session plans and reviews, and hands the work to cheaper workers, each at an
explicit effort level: Haiku to read, Opus at medium effort to build (Sonnet as the cheaper rung), and a reviewer one
model up only where mistakes are expensive: critical reviews (the final review before merge, security, data-loss and
concurrency paths, debugging after one failed fix) go to Fable 5.2 at high effort. Install it once per device; every
device then gets the same rules, agents, skills and settings.

A real session, sped up between the routing lines (sound on):

https://github.com/user-attachments/assets/bc9a5004-35cc-4b0a-93a1-8ff33cff70fb

## What a plain install gives you
| Path | What |
|---|---|
| `CLAUDE.md` | The rules: which model and effort for which job, how to split work, when to escalate, the weekly budget pace. Imported from your `~/.claude/CLAUDE.md` |
| `agents/` | `scout` (Haiku@medium: read, search, summarise), `scout-lead` (Sonnet@medium: splits a big read across scouts), `builder` (Opus@medium: code with a test), `reviewer` (Opus@medium: judgement; `model: fable` at high for the critical reviews) |
| `skills/orchestrate` | Run a feature as parallel background sessions in git worktrees, coordinated from one session |
| `skills/tech-debt` | Gather debt (ponytail-debt, ponytail-audit, greps, open issues), score it with one `reviewer`, keep a ledger per repo |
| `skills/tidy` | Clean up worktrees, finished background sessions and offline Remote Control rows without losing work (`--auto` does the safe part, e.g. from a daily cron job) |
| `settings.shared.json` | Settings merged into `~/.claude/settings.json` (most of your other keys stay, see Install): auto permission mode, Concise output style, a default effort and a 372K auto-compact window per model, and a few plugins (from `claude-plugins-official`, plus `ponytail`) |
| `install.sh`, `install.ps1` | Installers for macOS/Linux and Windows |

Everything else in the repo (`agent-mail/`, `hooks/`, `codex/`, `t3-bridge/`, `skills/needs`, `skills/standup`,
`settings.mods.json`, `settings.agent-mail.json`) is optional, see [Optional extras](#optional-extras). A plain install
does not use it.

## Install
Needs Claude Code, git and, on macOS/Linux, `jq`.
```sh
git clone https://github.com/Berkay2002/claude-config ~/.claude/shared
~/.claude/shared/install.sh                 # macOS / Linux
~/.claude/shared/install.ps1                # Windows (PowerShell)
```
The installer:
- adds `@~/.claude/shared/CLAUDE.md` to the top of your `~/.claude/CLAUDE.md` and leaves the rest of that file alone;
- links `agents/` and the skills `orchestrate`, `tech-debt` and `tidy` into `~/.claude` (junctions on Windows),
  backing up anything it replaces into `~/.claude/backups`;
- registers the plugin marketplaces and installs the plugins listed in `settings.shared.json`;
- merges `settings.shared.json` into `~/.claude/settings.json` (shared keys win; it also removes a global
  `autoCompactWindow` / `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`, which would override the per-model windows) and, only if every plugin installed,
  writes `~/.claude/claude-config.installed`, the hash of the `settings.shared.json` it applied.

Re-running it is safe; a lock (`~/.claude/.install.lock`) keeps two runs from overlapping.

## Make it yours
- Notes about one machine (drives, toolchains, servers, where repos live, your plans) go in `local/` of this clone,
  which is gitignored, so a pull never touches them and they are never pushed. `local/CLAUDE.md` is imported at the
  end of the shared rules when it exists. Your own `~/.claude/CLAUDE.md` below the import line works too and is not
  synced either.
- Rules for one project go in that project's own `CLAUDE.md`.
- To change the shared rules, fork this repo, edit, push, and install from your fork. Shared settings you don't want
  (theme, output style, plugins) can be removed from `settings.shared.json` in your fork.

## Sync
Pull and re-run the installer on each device:
```sh
git -C ~/.claude/shared pull --ff-only && ~/.claude/shared/install.sh
```
The `config-sync` mod (one of the [optional mods](#optional-extras)) does the pull for you at session start and keeps
reporting changes until `~/.claude/claude-config.installed` matches `settings.shared.json`; `/config-sync apply` runs
the installer. Once that mod is installed and has pulled at least once, the installer drops an old SessionStart
`git pull` hook from `settings.json` if you still have one.

Not synced: `~/.claude.json` (MCP servers, login), credentials, and anything in your own `~/.claude/CLAUDE.md`.

## Optional extras
These are the author's own multi-session setup. Nobody needs them; the rules in `CLAUDE.md` that depend on them say
"when installed" and are skipped otherwise. Two of them are installer flags:
```sh
~/.claude/shared/install.sh --with-mods --with-agent-mail     # Windows: install.ps1 -WithMods -WithAgentMail
```
The choice is saved in `local/install-extras` (one word per line), so a plain re-run keeps it. Delete a line there
and re-run to turn that extra off: the installer then stops merging its settings and removes the `needs`/`standup`
links it made; keys already merged into `settings.json` and the Codex rules in `$CODEX_HOME` stay until you remove
them.

- **Mods** (`--with-mods`, `settings.mods.json`): the [berkays-mods](https://github.com/Berkay2002/berkays-mods)
  plugins and `babysit-pr`. Among them `effort-gate` (asks you before Opus runs at xhigh/max in a worker, and blocks a
  background session with no `--model`), `route-ledger` (records every worker's model, effort and outcome;
  `/routing-review` shows what to tune), `limit-resume` (shows the 5-hour usage each turn and resumes long unattended
  runs after the reset) and `config-sync` (see [Sync](#sync)).
- **agent-mail** (`--with-agent-mail`, `settings.agent-mail.json`), which brings:
  - the agent-mail hooks, which record AskUserQuestion dialogs as decisions and, in quiet hours, queue the questions
    and drop push notifications;
  - the routing gate (`hooks/routing_gate.py`): a SessionStart note with the routing table and weekly pace, and a
    PreToolUse gate on `Agent` and `claude --bg` launches that denies a launch breaking a lock or override in
    `local/routing.json` (built-in defaults when that file is missing), and new workers at the weekly stop;
  - Codex workers: `codex/AGENTS.md` (the same rules for Codex CLI) linked to `$CODEX_HOME/AGENTS.md` (copied on
    Windows) when that folder exists, and `local/AGENTS.md` to `$CODEX_HOME/AGENTS.local.md` when you have one;
  - the `needs` skill (what a session is waiting on you for) and the `standup` skill (a morning status from
    long-running role sessions), which both work through agent-mail.

  The hooks call `python3.11` by name and do nothing when it is missing. The MCP server itself you register by hand:
  [agent-mail/README.md](agent-mail/README.md), [architecture](agent-mail/ARCHITECTURE.md).
- **T3 bridge** (`t3-bridge/`, no installer flag): use T3 Code as a front end for a live role session running in tmux.
  It patches a `claude-role` launcher that is not part of this repo. See [t3-bridge/README.md](t3-bridge/README.md)
  and [t3-bridge/INSTALL.md](t3-bridge/INSTALL.md).

## Benchmarks behind the routing
The routing in `CLAUDE.md` is derived from these. Directional only: real outcomes (`/routing-review`) win.

Artificial Analysis, 9 Oct 2026 (score @ $ per task; use the ratios, not the dollars):
| | low | medium | high | xhigh | max |
|---|---|---|---|---|---|
| Reasoning index: Haiku 5.5  | 30 @ .03 | 35 @ .05 | 38 @ .08 | 41 @ .13 | 43 @ .21 |
| Reasoning index: Sonnet 5.5 | 36 @ .35 | 41 @ .48 | 47 @ .88 | 52 @ 2.0 | – |
| Reasoning index: Opus 5.5   | 42 @ .55 | 51 @ 1.4 | 54 @ 1.8 | ? | ? |
| Coding agent: Haiku 5.5     | 28 @ .15 | 34 @ .23 | 35 @ .36 | 42 @ .62 | 37 @ 2.6 |
| Coding agent: Sonnet 5.5    | 42 @ .48 | 46 @ .63 | 55 @ 1.25 | 63 @ 3.3 | – |
| Coding agent: Opus 5.5      | ? | ? | ? | ? | 66 @ 13 |

The five candidates worth using (AA detail page, same date):
| | Haiku@xhigh | Sonnet@high | Opus@medium | Opus@high | Sonnet@xhigh |
|---|---|---|---|---|---|
| $ per task / time per task | 0.12 / 295s | 0.88 / 230s | 1.34 / **206s** | 1.82 / 285s | 2.01 / 443s |
| Terminal-Bench (agentic shell) | 29% | 44% | 53% | 57% | 57% |
| AutomationBench / GDPval (long tool workflows) | 36% / 1511 | 59% / 1550 | 61% / 1586 | 63% / 1705 | **66% / 1730** |
| Long-context reading (AA-LCR) | 78% | 78% | **84%** | 83% | 80% |
| Knowledge without hallucinating (Omniscience) | **6** | 21 | 40 | 41 | 24 |

Prices per MTok (input / output / cache read): Haiku 0.10 / 0.50 / 0.01 below 100K prompt, 5× that above ·
Sonnet 2 / 10 / 0.10 · Opus 4 / 20 / 0.20 · Fable 5.2 8 / 40 (input / output). The AA numbers already use Sonnet's
$0.10 cache reads.

Fable 5.2 is not in the Artificial Analysis tables above; there are no benchmark numbers for it here. `CLAUDE.md` relies
only on this: it costs 2× Opus per token but is reported level with Opus 5.5 at about half the cost per task. The
advisor sits one rung above the worker: Haiku or Sonnet → Opus, Opus → Fable 5.2, Fable → none. Opus at xhigh/max for
workers, and Fable outside the critical reviews and the advisor, need the user's OK.

Anthropic, "Optimizing for cost and intelligence" and the advisor docs (Oct 2026):
- Opus 5.5 on long-horizon coding, vs `high`: `medium` ≈ −2.5 points at ~70% of the cost; `xhigh` ≈ +1.4 at 2.5×.
- Run low first, re-run only failures higher: beats a flat `high` when failure is detectable.
- Orchestrating pays for bulk/parallel work; for one dependent chain that fits one context it is pure overhead.
- Advisor: executors under-call it (Haiku/Sonnet → Opus advisor: 0 calls on 198 Q&A questions) unless prompted;
  best timing is one early call after the first reads and one before declaring done. Sonnet@medium + Opus advisor ≈
  Sonnet at default effort for less (coding). Subscription: counts toward plan limits. Needs feature-flag fetching
  (`DISABLE_TELEMETRY` / `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` turn it off silently).
- Prompt cache (code.claude.com/docs/en/prompt-caching): switching model invalidates it, and so does each plan-mode
  toggle under `opusplan`. Changing effort keeps it on Opus/Sonnet/Haiku 5.5 and Fable with a subscription or API
  key (not on Bedrock, Agent Platform, apps gateway, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, HIPAA). Toggling the
  advisor keeps it. Subagents get a 5-minute cache TTL even on a subscription (`subagentPromptCacheTtl` changes it).

## License
MIT
