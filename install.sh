#!/usr/bin/env bash
# Links this repo into ~/.claude and merges settings.shared.json into settings.json. Safe to re-run. Needs jq.
# Usage (repo must be cloned to ~/.claude/shared):  ./install.sh
# Exits 1 when a plugin could not be installed; the applied-settings marker is then not written.
set -euo pipefail
unset CLAUDE_CONFIG_DIR  # always the default config, even when started from a session with a private one
claude="$HOME/.claude"; shared="$claude/shared"
[ "$(cd "$(dirname "$0")" && pwd -P)" = "$(cd "$shared" && pwd -P)" ] || { echo "Clone this repo to $shared first." >&2; exit 1; }
command -v jq >/dev/null || { echo "Install jq first." >&2; exit 1; }
stamp="$(date +%Y%m%d-%H%M%S)"; backups="$claude/backups"
mkdir -p "$backups" "$claude/skills"

# Only one installer at a time (the config-sync mod, a terminal and a second device share ~/.claude); a lock older than 10 minutes is stale.
lock="$claude/.install.lock"; tmp=""; snap=""
if ! mkdir "$lock" 2>/dev/null; then
  if [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then rmdir "$lock" 2>/dev/null || true; fi
  mkdir "$lock" 2>/dev/null || { echo "Another install is running ($lock)." >&2; exit 1; }
fi
trap 'rmdir "$lock" 2>/dev/null || true; [ -z "$tmp" ] || rm -f "$tmp"; [ -z "$snap" ] || rm -f "$snap"' EXIT

# One copy of settings.shared.json for the whole run: the merge and the marker must describe the same bytes, even if a pull lands meanwhile.
snap="$(mktemp "$claude/settings.shared.XXXXXX")"
cp "$shared/settings.shared.json" "$snap"
want="$(git -C "$shared" hash-object --path=settings.shared.json "$snap")"

# 1. ~/.claude/CLAUDE.md is yours: only make sure it imports the shared rules; nothing else in it is touched.
line='@~/.claude/shared/CLAUDE.md'; g="$claude/CLAUDE.md"
# The import line counts if it is there at all: indented, trailing blanks, CRLF, or a BOM before it. A symlinked CLAUDE.md stays a symlink (written through).
if [ ! -s "$g" ]; then
  printf '%s\n' "$line" > "$g"
elif ! sed $'1s/^\xef\xbb\xbf//; s/\r$//; s/^[[:space:]]*//; s/[[:space:]]*$//' "$g" | grep -qxF "$line"; then
  cp "$g" "$backups/CLAUDE.md.$stamp"
  tmp="$(mktemp "$claude/CLAUDE.md.XXXXXX")"
  { printf '%s\n\n' "$line"; cat "$g"; } > "$tmp"
  if [ -L "$g" ]; then cat "$tmp" > "$g"; rm -f "$tmp"; else mv "$tmp" "$g"; fi; tmp=""
fi

# 2. Symlinks for agents and repo-owned skills.
link() { [ -L "$2" ] && return; [ -e "$2" ] && mv "$2" "$backups/$(basename "$2").$stamp"; ln -s "$1" "$2"; }
link "$shared/agents" "$claude/agents"
for d in "$shared"/skills/*/; do d="${d%/}"; link "$d" "$claude/skills/$(basename "$d")"; done

# 3. Plugins: register and update marketplaces, then install missing user-scope plugins named in settings.shared.json. Failures are printed to stderr and keep the marker back.
failed=0
if command -v claude >/dev/null; then
  have_m="$(claude plugin marketplace list --json </dev/null 2>/dev/null | jq -r '.[].name' || true)"
  while read -r name repo; do
    if ! grep -qxF "$name" <<<"$have_m"; then
      out="$(claude plugin marketplace add "$repo" </dev/null 2>&1)" && echo "Added marketplace $name" || { echo "Marketplace $name failed: $(head -n1 <<<"$out")" >&2; failed=1; }
    fi
    out="$(claude plugin marketplace update "$name" </dev/null 2>&1)" || echo "Marketplace $name update failed: $(head -n1 <<<"$out")" >&2
  done < <(jq -r '.extraKnownMarketplaces // {} | to_entries[] | "\(.key) \(.value.source.repo)"' "$snap")
  have_p="$(claude plugin list --json </dev/null 2>/dev/null | jq -r '.[] | select(.scope == "user") | .id' || true)"
  while read -r id; do
    grep -qxF "$id" <<<"$have_p" && continue
    out="$(claude plugin install "$id" </dev/null 2>&1)" && echo "Installed plugin $id" || { echo "Plugin $id failed: $(head -n1 <<<"$out")" >&2; failed=1; }
  done < <(jq -r '.enabledPlugins // {} | to_entries[] | select(.value) | .key' "$snap")
else
  echo "claude not on PATH: plugins not installed." >&2; failed=1
fi

# 4. Settings: shared keys win, local-only keys (hooks, env, voice, ...) stay.
# The old session-start git pull goes only once the config-sync mod is installed AND has run (its store holds a pull), so a device is never left without a pull.
drop=false
if command -v claude >/dev/null && claude plugin list --json </dev/null 2>/dev/null | jq -e '[.[] | select(.scope == "user" and .id == "config-sync@berkays-mods")] | length > 0' >/dev/null 2>&1; then
  for f in "$claude"/plugins/store/config-sync_*.json; do
    if [ -f "$f" ] && jq -e '."last-pull".at | numbers' "$f" >/dev/null 2>&1; then drop=true; fi
  done
fi
s="$claude/settings.json"
if [ -f "$s" ]; then cp "$s" "$backups/settings.json.$stamp"; else echo '{}' > "$s"; fi
pull="git -C \"$shared\" pull --ff-only -q"
tmp="$(mktemp "$claude/settings.json.XXXXXX")"
if ! jq --slurpfile sh "$snap" --arg pull "$pull" --argjson drop "$drop" '
  . * $sh[0]
  | del(.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, .autoCompactWindow)
  | if $drop and .hooks.SessionStart then
      .hooks.SessionStart |= [.[] | (if (.hooks | type) == "array" then .hooks |= map(select(.command != $pull)) else . end)
                                  | select((.hooks | type) != "array" or (.hooks | length) > 0)]
      | if (.hooks.SessionStart | length) == 0 then del(.hooks.SessionStart) else . end
    else . end
' "$s" > "$tmp"; then
  echo "Settings merge failed; $s is unchanged." >&2; exit 1  # the trap removes the temp file
fi
mv "$tmp" "$s"; tmp=""

# 5. Marker: which settings.shared.json is applied; the config-sync mod nags until it matches. Only after every plugin installed.
if [ "$failed" -ne 0 ]; then echo "Installed with plugin errors (see above); marker not written, so /config-sync will keep reporting changes." >&2; exit 1; fi
tmp="$(mktemp "$claude/claude-config.installed.XXXXXX")"
printf '%s\n' "$want" > "$tmp"
mv "$tmp" "$claude/claude-config.installed"; tmp=""
echo "Installed. Backups in $backups."
