<p align="center"><img src="assets/logo.svg" width="112" alt=""></p>
<h1 align="center">claude-config</h1>

A cost-first Claude Code setup: one Opus session plans and reviews, and hands the work to cheaper workers
(Haiku to read, Sonnet to build, Opus only where mistakes are expensive), each at an explicit effort level.
Install it once per device; every device then pulls the same rules, agents, skills and settings.

A real session, sped up between the routing lines (sound on):

https://github.com/user-attachments/assets/bc9a5004-35cc-4b0a-93a1-8ff33cff70fb

| Path | What |
|---|---|
| `CLAUDE.md` | The rules: which model and effort for which job, how to split work, when to escalate, budget |
| `agents/` | `scout` (Haiku: read, search, summarise), `builder` (Sonnet: code with a test), `reviewer` (Opus: judgement) |
| `skills/orchestrate` | Run a feature as parallel background sessions in git worktrees, coordinated from one session |
| `settings.shared.json` | Settings merged into `~/.claude/settings.json` on every device (your other keys stay) |
| `install.sh`, `install.ps1` | Installers for macOS/Linux and Windows |

It works best with the [berkays-mods](https://github.com/Berkay2002/berkays-mods) plugins, which the shared settings
install: `effort-gate` (asks you before Opus runs at xhigh/max in a worker, and blocks a background session with no
`--model`), `route-ledger` (records every worker's model, effort and outcome; `/routing-review` shows what to tune) and
`config-sync` (keeps every device on the latest config).

## Install
Needs Claude Code, git and, on macOS/Linux, `jq`.
```sh
git clone https://github.com/Berkay2002/claude-config ~/.claude/shared
~/.claude/shared/install.sh                 # macOS / Linux
~/.claude/shared/install.ps1                # Windows (PowerShell)
```
The installer:
- adds `@~/.claude/shared/CLAUDE.md` to the top of your `~/.claude/CLAUDE.md` and leaves the rest of that file alone;
- links `agents/` and the skills into `~/.claude`, backing up anything it replaces into `~/.claude/backups`;
- merges `settings.shared.json` into `~/.claude/settings.json` (shared keys win) and installs the listed plugins.

Re-running it is safe.

## Make it yours
- Notes about one machine (drives, toolchains, servers, where repos live) go in that machine's `~/.claude/CLAUDE.md`,
  below the import line. They are not synced.
- Rules for one project go in that project's own `CLAUDE.md`.
- To change the shared rules, fork this repo, edit, push, and install from your fork.
  Shared settings you don't want (theme, output style, plugins) can be removed from `settings.shared.json` in your fork.

## Sync
The `config-sync` mod (`config-sync@berkays-mods`) pulls this repo (`--ff-only`) at session start, at most once per 10 minutes per device. It tells you when the pull fails, when this clone has unpushed or uncommitted changes, and when `settings.shared.json` no longer matches `~/.claude/claude-config.installed` ("Shared settings changed: run /config-sync apply"). A state is announced when it appears or changes, and again at most once a day while it persists.
- `/config-sync` shows the last pull, the settings hash (installed vs current) and local changes.
- `/config-sync apply` runs the installer. The mod never runs it on its own.

The installer takes a lock (`~/.claude/.install.lock`), registers and updates the marketplaces, installs the missing user-scope plugins from `settings.shared.json`, merges the settings, and writes the marker last, only if every plugin installed. It only makes sure `~/.claude/CLAUDE.md` contains the line `@~/.claude/shared/CLAUDE.md` (prepending it if missing); the rest of that file is yours. It drops the old SessionStart `git pull` hook only once `config-sync@berkays-mods` is installed and has pulled at least once.

Without the mod, run `git -C ~/.claude/shared pull` and the installer yourself.

Not synced: `~/.claude.json` (MCP servers, login), credentials, and anything in your own `~/.claude/CLAUDE.md`.

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
Sonnet 2 / 10 / 0.10 · Opus 4 / 20 / 0.20. The AA numbers already use Sonnet's $0.10 cache reads.

Anthropic, "Optimizing for cost and intelligence" and the advisor docs (Oct 2026):
- Opus 5.5 on long-horizon coding, vs `high`: `medium` ≈ −2.5 points at ~70% of the cost; `xhigh` ≈ +1.4 at 2.5×.
- Run low first, re-run only failures higher: beats a flat `high` when failure is detectable.
- Orchestrating pays for bulk/parallel work; for one dependent chain that fits one context it is pure overhead.
- Advisor: executors under-call it (Haiku/Sonnet → Opus advisor: 0 calls on 198 Q&A questions) unless prompted;
  best timing is one early call after the first reads and one before declaring done. Sonnet@medium + Opus advisor ≈
  Sonnet at default effort for less (coding). Subscription: counts toward plan limits. Needs feature-flag fetching
  (`DISABLE_TELEMETRY` / `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` turn it off silently).
- Prompt cache (code.claude.com/docs/en/prompt-caching): switching model invalidates it, and so does each plan-mode
  toggle under `opusplan`. Changing effort keeps it on Opus/Sonnet/Haiku 5.5 and Fable 5.1 with a subscription or API
  key (not on Bedrock, Agent Platform, apps gateway, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, HIPAA). Toggling the
  advisor keeps it. Subagents get a 5-minute cache TTL even on a subscription (`subagentPromptCacheTtl` changes it).

## License
MIT
