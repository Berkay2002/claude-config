---
name: tech-debt
description: Identify, categorize, and prioritize technical debt. Trigger with "tech debt", "technical debt audit", "what should we refactor", "code health", or when the user asks about code quality, refactoring priorities, or maintenance backlog.
---

# Tech Debt Management

Systematically identify, categorize, and prioritize technical debt.

## Categories

| Type | Examples | Risk |
|------|----------|------|
| **Code debt** | Duplicated logic, poor abstractions, magic numbers | Bugs, slow development |
| **Architecture debt** | Monolith that should be split, wrong data store | Scaling limits |
| **Test debt** | Low coverage, flaky tests, missing integration tests | Regressions ship |
| **Dependency debt** | Outdated libraries, unmaintained dependencies | Security vulns |
| **Documentation debt** | Missing runbooks, outdated READMEs, tribal knowledge | Onboarding pain |
| **Infrastructure debt** | Manual deploys, no monitoring, no IaC | Incidents, slow recovery |

## Prioritization Framework

Score each item on:
- **Impact**: How much does it slow the team down? (1-5)
- **Risk**: What happens if we don't fix it? (1-5)
- **Effort**: How hard is the fix? (1-5, inverted — lower effort = higher priority)

Priority = (Impact + Risk) x (6 - Effort)

## Output

Produce a prioritized list with estimated effort, business justification for each item, and a phased remediation plan that can be done alongside feature work.

## Workflow

This skill scores and plans; other tools find the debt. The orchestrator does no bulk reading (CLAUDE.md rule 1).

1. **Gather** (run in parallel, each returns rows `<file>:<line> | category | one-line problem`, no dumps):
   - `ponytail-debt`: the deliberate `shortcut:` deferrals; keep its `no-trigger` tag, it raises Risk.
   - `ponytail-audit` checklist over the repo: one `scout` (haiku@medium) per directory slice that fits well under
     100K context. Big repo (dozens of slices): suggest a workflow instead (CLAUDE.md "How to split work").
   - Cheap greps: `TODO|FIXME|HACK|XXX`, skipped/xfail tests, pinned or outdated dependencies.
   - Open issues: `glab issue list -l maintenance` (or `gh` on GitHub repos), plus open merge requests that may
     already fix an item.
2. **Score**: one `reviewer` (opus@medium) gets the merged rows and the existing ledger, drops false positives and
   duplicates, merges rows that share one fix, and scores Impact/Risk/Effort with the formula above.
3. **Ledger**: write or update `<repo>/.claude/briefs/tech-debt/ledger.md` (never in git: `git check-ignore -q`,
   else add it to `.git/info/exclude`). Keep each item's first-seen date and re-score instead of starting over; mark
   items gone from the code as resolved.
4. **Report**: top ~10 by priority, each with effort and why it matters, plus the phased plan (fix-now quick wins,
   fold into related feature work, needs its own issue).
5. **Issues**: only on the user's OK, file via `review-ready-mr` under one standing "Maintenance backlog" epic, label
   `maintenance` (plus `Bug` for real defects). One issue per fix, small related items as checkboxes in one issue;
   fold-into-feature items get no issue. Put the issue number in the ledger's `Issue` column.

Run it on request, before sprint planning, or after a large merge.

Rubric adapted from the tech-debt skill in Anthropic's `engineering` plugin.
