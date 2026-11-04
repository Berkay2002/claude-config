# t3-bridge

Use T3 Code as a front end for a live Claude Code role session that runs in tmux. T3 thinks it runs `claude` over the
Agent SDK stream-json protocol; the bridge never starts a second claude on the session. It types into the session's
tmux pane and reads the session's transcript, so tmux, Remote Control and T3 all show and drive the same session.
Python 3.11 stdlib only. Install: [INSTALL.md](INSTALL.md).

## Files
| File | What it is |
|---|---|
| `rolelive.py` | The core, with no T3 code in it. Attaches to a live session by role name or session ID, types input, interrupts, answers dialogs, switches model and effort for the session only, and streams events. Also a CLI. |
| `t3-tmux-bridge` | A thin T3 adapter, set as a T3 Claude instance's binary path. Translates stream-json, SDK control requests and wake turns to and from the core. Any argv without `--session-id`/`--resume` (T3's probe, `--version`) goes to the real claude unchanged. |
| `t3-bridge-hook` | A recording hook in the live session (PreToolUse `AskUserQuestion`, PermissionRequest `*`). Appends each dialog to `$XDG_RUNTIME_DIR/t3-bridge/<session_id>.jsonl` (0600). Never decides anything: the TUI still shows the dialog. |
| `t3-bridge-guard` | Update guard for cron. When a new runtime appears under `~/.t3/runtime/versions/`, checks the protocol strings the bridge relies on, then runs one live Haiku turn through the bridge on a throwaway server. |
| `t3rpc.mjs` | Small WebSocket RPC client for a T3 server (Node 22+, no dependencies): the guard's live turn, and manual tests (`items`, `respond`, `send`). |
| `claude-role.patch` | Adds the hook to `claude-role`, and runs a role started by the bridge in tmux. |

## T3 settings per role
Settings → Providers → add a Claude instance, e.g. named `<role>`:
- Binary path: `<bin>/t3-tmux-bridge` (the link in your bin folder, next to `claude`).
- Launch args: `--bridge-role <role> --bridge-start "claude-role <role>"`. Add `--bridge-claude-dir <dir>` when T3
  runs with a different `HOME` than claude (the role's claude config dir; default `$CLAUDE_CONFIG_DIR`, else
  `~/.claude`), and `--bridge-tmux-session <name>` to only attach to sessions in that tmux session.
- Use the instance from a T3 project whose folder is a separate empty directory, never the role's working folder, so
  T3 checkpoints and restores can't touch the role's files.
- Approvals: run the thread in an approval mode where T3 asks you (approval-required, or full access, which still
  asks for the TUI's dialogs). The bridge presses a key only for a decision a person made in T3.

The bridge logs to `<claude dir>/t3-bridge/bridge-<role>.log` (0700 dir, 0600 file; frame bodies only with
`T3_BRIDGE_DEBUG=1`). The seen offset per role is in `<claude dir>/rolelive/t3-<role>.json`.

## How it maps
| Live session | T3 |
|---|---|
| A prompt typed in tmux/RC, by a peer, or a background task notification | A wake turn: `› (typed in tmux/RC) …`, `› (peer X) …`, `› (background task completed: …)` |
| Text, tool calls and results | Assistant messages and tool items, one message at a time (no token streaming) |
| Agent call | `task_started`, the child's prompt and frames with `parent_tool_use_id`, `task_notification`: a subagent item with its own child thread. Background agents also update `background_tasks_changed` |
| Permission dialog, AskUserQuestion (also several questions, multiSelect) | `can_use_tool` → T3's approval or question panel. Approve presses `1. Yes` (never a "don't ask again" row; "allow for session" is pressed as once, with a notice), Decline presses the last `No` row, answers are typed into the dialog. Answered in tmux or RC instead: T3's panel closes |
| `/model` picker, `s` (this session only) | T3's model and effort pickers, applied only when they change, while the session is idle |
| Turns that ran while no bridge was attached | Shown after the next T3 turn, as wake turns labelled `earlier` (from the saved offset; at most 2000 events) |

## Core interface (`rolelive.LiveSession`)
- `attach(role=None, session_id=None, start_cmd=None, claude_dir=None, tmux_session=None)`, then `start(since=None |
  "saved", state_key=None)` and `close()`. `save_offset(offset)` records what the consumer delivered.
- `send(text, files=())`: waits up to 5 s for an empty input line, never types over a draft or into a dialog.
- `interrupt()`, `answer(id, choice)` (`{question: label}`, `"a, b"` for multiSelect, free text, `"allow"`, `"deny"`),
  `set_model(model=None, effort=None)`, `status()`.
- Events (dicts with `kind`; names and fields are stable, the module docstring lists every field): `turn_start`,
  `text`, `thinking`, `tool_use`, `tool_result`, `turn_end`, `subagent_start`, `subagent_end`, `dialog_open`,
  `dialog_closed`, `send_failed` (`draft`, `dialog`, `timeout`, `tmux`), `session`, `notice`.
- CLI: `rolelive.py ROLE [--start CMD] [--tmux-session NAME]` prints events as JSON lines. Each stdin line is input,
  or `:interrupt`, `:answer ID …`, `:allow ID`, `:deny ID`, `:model M [E]`, `:effort E`, `:status`.

Model and effort: a typed `/model M` or `/effort E` also saves the choice as the default for every new session
(`settings.json`), so the core never types them. It drives the `/model` picker instead (arrows, ←/→ for effort, `s`
for this session only), checks every screen before a key, and restores `settings.json` with a notice if it changed.

## Deploys
A running bridge checks the mtime of `t3-tmux-bridge` and `rolelive.py` (through their links) every 2 s. When either
changed, it finishes the open turn, waits until no dialog or send is pending, and exits. T3 starts the new version
on the thread's next message (`--resume`), and the turns in between arrive as wake turns.

## Tests
`python3.11 -B -m unittest -v test_rolelive test_t3_bridge` (fake screens, transcripts and a fake session: no tmux,
claude or T3) and `node --test t3rpc.test.mjs`.

## Limits
- T3's system prompt and its `t3-code` MCP tools don't reach the live session. T3's permission mode doesn't either:
  the session keeps its own.
- No token streaming, and thinking text is empty (the transcript keeps only a signature).
- A turn that starts while T3 has released the provider (30 min idle, hard-coded in T3) shows only after the next T3
  turn. T3 does not respawn the bridge by itself after a deploy exit either: the next message does.
- A permission dialog is answered only when its command, file name or URL is in the dialog box on screen and no other
  open dialog shares it. Commands the TUI shortens past 200 characters, and tools whose input has no string, fail
  closed: answer those in tmux.
- Several T3 threads on one role all mirror it and can all send; a dialog answered from one closes in the others.
- Agents that were already running when the bridge attached are not shown. A lost send is noticed only once the
  session has been idle for 45 s.
- Hook spool files in `$XDG_RUNTIME_DIR/t3-bridge/` are not cleaned up (they go with the runtime dir at logout).
