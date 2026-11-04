# Installing t3-bridge

`<bin>` is the folder with your `claude` wrapper (on `PATH`); `<repo>` is this repository's checkout.

1. Link the pieces into `<bin>` (the bridge finds the real `claude` next to its link, so keep them together):
   ```sh
   for f in t3-tmux-bridge t3-bridge-hook t3-bridge-guard; do ln -sfn <repo>/t3-bridge/$f <bin>/$f; done
   ```
   `t3rpc.mjs` and `rolelive.py` stay in the repo; the links resolve to them. Set `T3_BRIDGE_CLAUDE` only if the
   real `claude` is somewhere else.
2. Patch `claude-role` so every role runs with the recording hook, and a role the bridge starts runs in tmux:
   ```sh
   patch <bin>/claude-role <repo>/t3-bridge/claude-role.patch
   ```
   A role picks the hook up at its next start (`claude-role <role>`, or `claude-roles` for the missing ones).
3. Add one Claude instance per role in T3 (Settings → Providers), as in the README: binary path
   `<bin>/t3-tmux-bridge`, launch args `--bridge-role <role> --bridge-start "claude-role <role>"`, and a T3 project
   folder that is a separate empty directory.
4. The update guard, hourly (not installed for you; `crontab -e`). cron has no `PATH` or session bus, so pass them;
   `T3_GUARD_CLAUDE_DIR` is the claude config dir the roles use:
   ```cron
   17 * * * * PATH=<bin>:/usr/local/bin:/usr/bin:/bin DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus T3_GUARD_CLAUDE_DIR=<claude dir> <bin>/t3-bridge-guard >/dev/null 2>&1
   ```
   It does nothing until a new runtime version appears. Then it writes `~/.local/state/t3-bridge/guard-<version>.txt`
   (PASS or FAIL with the reason), sends a desktop notification, and exits 1 on FAIL. It uses port 3799, a throwaway
   base dir, a toy Haiku role in tmux session `t3-bridge-guard` (window `guard`, removed afterwards) and the folder
   `~/.local/state/t3-bridge/guard-toy`, whose folder-trust question it answers on the first run. Run
   `<bin>/t3-bridge-guard --force` once by hand to see it pass.
