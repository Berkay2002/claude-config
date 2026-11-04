# t3-tmux-bridge (spike)

Use T3 Code as a front end for a live Claude Code role session that runs in tmux. The bridge never starts a second
claude on the session: it types into the session's tmux pane and reads the session's transcript. Production home:
your bin folder.

## Files
| File | What it is |
|---|---|
| `rolelive.py` | Core library and CLI, stdlib only, with no T3 code in it. It attaches to a live session by role name or session ID, sends input, interrupts, answers dialogs, and streams events. |
| `t3-tmux-bridge` | A thin T3 adapter. T3 sets it as the Claude provider's `binaryPath`. It translates stream-json and SDK control messages to and from the core. |
| `t3-bridge-hook` | A recording hook that runs in the live session (PreToolUse `AskUserQuestion`, PermissionRequest `*`). It appends each open dialog to `$XDG_RUNTIME_DIR/t3-bridge/<session_id>.jsonl`. It never decides anything, so the TUI and Remote Control still show the dialog. |
| `t3-bridge-guard` | Update guard. It runs when a new runtime appears under `~/.t3/runtime/versions/`. It checks that the strings the bridge relies on are still there and that the new server boots on a throwaway base dir. |

## Setup
1. The role session must run with the hook. claude-role adds this to the claude command line:
   `--settings '{"hooks":{"PreToolUse":[{"matcher":"AskUserQuestion","hooks":[{"type":"command","command":"~/bin/t3-bridge-hook"}]}],"PermissionRequest":[{"matcher":"*","hooks":[{"type":"command","command":"~/bin/t3-bridge-hook"}]}]}}'`
2. In T3, go to Settings → Providers and add a Claude instance per role, for example `<role>`:
   - Binary path: `~/bin/t3-tmux-bridge`
   - Launch args: `--bridge-role <role> --bridge-start "claude-role <role>"`
   - Use it from a T3 project whose folder is a separate empty directory, never the role's working folder, so T3
     checkpoints and restores can't touch the role's files.
   - The equivalent `settings.json` entry is `providerInstances.<id> = {"driver":"claudeAgent","displayName":"<role>","config":{"binaryPath":…,"launchArgs":…}}`.
3. The bridge writes its log to `$T3_BRIDGE_LOG_DIR` (default `~/.cache/t3-bridge`), in `bridge-<role>.log`.
   The log holds message text and T3's MCP bearer token, so it stays private.

## Core interface (`rolelive.LiveSession`)
- `attach(role=None, session_id=None, start_cmd=None)`, then `start()` and `close()`
- `send(text, files=())`, `interrupt()`
- `answer(id, choice)`, where `choice` is `{question: label}`, free text, `"allow"` or `"deny"`
- `set_model(m, persist_ok=False)`, `set_effort(e, persist_ok=False)`
- `status()`

Events are dicts with a `kind` key (full list in the module docstring): `turn_start`, `text`, `thinking`, `tool_use`,
`tool_result`, `turn_end`, `dialog_open`, `dialog_closed`, `session`, `notice`.

`set_model` and `set_effort` refuse by default. A typed `/model` or `/effort` also saves the choice as the default for
every new session (`~/.claude/settings.json`).

The CLI is `rolelive.py ROLE`. It prints events as JSON lines. Each stdin line is user input, or a command:
`:interrupt`, `:answer ID …`, `:allow ID`, `:deny ID`, `:model ! M`, `:effort ! E`, `:status`.

## Mapping to the T3 web chat (packages/contracts `OrchestrationV2TurnItem`)
| Core event | T3 item | apps/web component |
|---|---|---|
| `turn_start` (`origin` human or peer) | `user_message` (peer: `senderThreadId` or `notification`) | `TimelineUserMessageRow` |
| `text` | `assistant_message` | `AssistantTimelineRow` |
| `thinking` | `reasoning` | `ThinkingTimelineRow` / `ReasoningTraceContent` |
| `tool_use` + `tool_result` (paired by id; `tool` gives the name) | `command_execution` (Bash), `file_change` (Edit/Write), `file_search` (Grep/Glob), `subagent` (Agent), `dynamic_tool` (other tools) | `SimpleWorkEntryRow`, `LiveWorkEntryTimelineRow`, `V2ItemInspector` |
| `dialog_open` question / permission | `user_input_request` / `approval_request` | `ComposerPendingUserInputPanel` / `TimelinePendingApprovalRow` |
| `turn_end` done / interrupted | run status `completed` / `interrupted` | `WorkingTimelineRow`, Stop button in `ComposerPrimaryActions` |
| `notice` | `system_notice` | timeline notice row |

## T3 behaviours the bridge relies on (0.1.8-nightly.20261104)
- T3 spawns `binaryPath` with `--session-id=<T3 id>`, or `--resume=<T3 id>` when it respawns, and appends the launch
  args. Any other argv (`--version`, the health probe) is passed on to the real claude.
- T3 accepts any `session_id` in `system/init`. It matches its own prompt through `user_message_uuids`, and drops a
  `result` with `num_turns` 0.
- An unsolicited `system/init` while T3 is idle opens a continuation turn ("Background activity updated"). This works
  only after T3 has run one turn on that process.
- Dialogs: a `can_use_tool` control request with `requires_user_interaction` gets a `control_response` whose
  `updatedInput.answers` maps each question to its label. A `control_cancel_request` closes the dialog.
- T3 releases an idle provider after 30 minutes. That timeout is hard-coded. T3 kills the provider's process group
  after an interrupt. The role runs in its own tmux session, so neither kill reaches it.

## Limits
- T3's system prompt and its `t3-code` MCP tools don't reach the live session.
- No token streaming: text arrives one message at a time. Thinking text is empty, because the transcript stores only
  a signature.
- Agent calls render as a generic tool. A `subagent` item would need `task_*` frames.
- Turns that start while T3 has released the provider show up only after the next T3 turn.
- A T3 paste can merge with text half-typed in the tmux pane.
- Permission-dialog mirroring and multi-select or multi-question answers are untested (the toy role ran in bypass
  mode).
