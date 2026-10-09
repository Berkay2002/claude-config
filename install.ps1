# Links this repo into ~/.claude and merges settings.shared.json into settings.json. Safe to re-run.
# Usage (repo must be cloned to ~/.claude/shared):  ./install.ps1
# Works in Windows PowerShell 5.1 and PowerShell 7. Exits 1 when a plugin could not be installed; the applied-settings marker is then not written.
$ErrorActionPreference = 'Stop'
Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue  # always the default config, even when started from a session with a private one
$claude = Join-Path $HOME '.claude'
$shared = Join-Path $claude 'shared'
if ((Resolve-Path $PSScriptRoot).Path -ne (Resolve-Path $shared).Path) { throw "Clone this repo to $shared first." }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backups = Join-Path $claude 'backups'
New-Item -ItemType Directory -Force $backups, (Join-Path $claude 'skills') | Out-Null
function Write-Utf8($path, $text) { [IO.File]::WriteAllText($path, $text) }  # no BOM
function Read-Utf8($path) { [IO.File]::ReadAllText($path) }  # UTF-8 with or without BOM; Get-Content -Raw reads BOM-less UTF-8 as ANSI on Windows PowerShell 5.1
# Only one installer at a time (the config-sync mod, a terminal and a second device share ~/.claude); a lock older than 10 minutes is stale.
# The try below stays open to the end of the file (not indented) so the finally always releases the lock.
$lock = Join-Path $claude '.install.lock'
if ((Test-Path $lock) -and ((Get-Item $lock).LastWriteTime -lt (Get-Date).AddMinutes(-10))) { Remove-Item $lock -Force -ErrorAction SilentlyContinue }
try { $lockFile = [IO.File]::Open($lock, 'CreateNew', 'Write', 'None') } catch { throw "Another install is running ($lock)." }
$exitCode = 0
$snap = Join-Path $claude "settings.shared.$PID.snap"
try {

# One copy of settings.shared.json for the whole run: the merge and the marker must describe the same bytes, even if a pull lands meanwhile.
Copy-Item (Join-Path $shared 'settings.shared.json') $snap -Force
$want = ("$(git -C $shared hash-object --path=settings.shared.json $snap)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $want) { throw 'git hash-object failed on settings.shared.json.' }
$shc = Read-Utf8 $snap | ConvertFrom-Json

# 1. ~/.claude/CLAUDE.md is yours: only make sure it imports the shared rules; nothing else in it is touched.
$line = '@~/.claude/shared/CLAUDE.md'
$global = Join-Path $claude 'CLAUDE.md'
# The import line counts if it is there at all: indented, trailing blanks, CRLF, or a BOM before it. A symlinked CLAUDE.md stays a symlink (written through).
$existing = if (Test-Path $global) { Read-Utf8 $global } else { $null }
if (-not $existing) { Write-Utf8 $global "$line`n" }
elseif (@($existing -split '\r?\n' | ForEach-Object { $_.TrimStart([char]0xFEFF).Trim() }) -notcontains $line) {
  Copy-Item $global (Join-Path $backups "CLAUDE.md.$stamp")
  if ((Get-Item $global).LinkType) { Write-Utf8 $global "$line`n`n$existing" }
  else { Write-Utf8 "$global.tmp" "$line`n`n$existing"; Move-Item -Force "$global.tmp" $global }
}

# 2. Junctions (no admin needed) for agents and repo-owned skills.
function Link($target, $link) {
  $item = Get-Item $link -ErrorAction SilentlyContinue
  if ($item -and $item.LinkType) { return }
  if ($item) { Move-Item $link (Join-Path $backups "$($item.Name).$stamp") }
  New-Item -ItemType Junction -Path $link -Target $target | Out-Null
}
Link (Join-Path $shared 'agents') (Join-Path $claude 'agents')
Get-ChildItem (Join-Path $shared 'skills') -Directory | ForEach-Object { Link $_.FullName (Join-Path $claude "skills\$($_.Name)") }

# 3. Plugins: register and update marketplaces, then install missing user-scope plugins named in settings.shared.json. Failures go to stderr and keep the marker back.
function Fail($text) { [Console]::Error.WriteLine($text); $script:exitCode = 1 }
function Claude-Cli {  # merged output, for messages; native stderr must not trip $ErrorActionPreference = 'Stop' on 5.1
  $ErrorActionPreference = 'Continue'
  $out = @(& claude @args 2>&1 | ForEach-Object { "$_" })
  @{ ok = ($LASTEXITCODE -eq 0); lines = $out }
}
function Claude-Json {  # stdout only, so stderr noise cannot break ConvertFrom-Json
  $ErrorActionPreference = 'Continue'
  $out = @(& claude @args 2>$null)
  if ($LASTEXITCODE -ne 0 -or -not $out) { return }
  # Windows PowerShell 5.1 emits a JSON array as ONE pipeline object; assign, then emit, so the caller sees the elements.
  $json = $out -join "`n" | ConvertFrom-Json
  $json
}
$haveClaude = [bool](Get-Command claude -ErrorAction SilentlyContinue)
if ($haveClaude) {
  $haveM = @(Claude-Json plugin marketplace list --json | ForEach-Object { $_.name })
  foreach ($m in $shc.extraKnownMarketplaces.PSObject.Properties) {
    if ($haveM -notcontains $m.Name) {
      $r = Claude-Cli plugin marketplace add $m.Value.source.repo
      if ($r.ok) { Write-Host "Added marketplace $($m.Name)" } else { Fail "Marketplace $($m.Name) failed: $($r.lines[0])" }
    }
    $r = Claude-Cli plugin marketplace update $m.Name
    if (-not $r.ok) { [Console]::Error.WriteLine("Marketplace $($m.Name) update failed: $($r.lines[0])") }
  }
  $haveP = @(Claude-Json plugin list --json | Where-Object { $_.scope -eq 'user' } | ForEach-Object { $_.id })
  foreach ($p in $shc.enabledPlugins.PSObject.Properties) {
    if (-not $p.Value -or $haveP -contains $p.Name) { continue }
    $r = Claude-Cli plugin install $p.Name
    if ($r.ok) { Write-Host "Installed plugin $($p.Name)" } else { Fail "Plugin $($p.Name) failed: $($r.lines[0])" }
  }
} else { Fail 'claude not on PATH: plugins not installed.' }

# 4. Settings: shared keys win, local-only keys (hooks, env, voice, ...) stay.
function Merge($a, $b) {
  foreach ($p in $b.PSObject.Properties) {
    if ($p.Value -is [pscustomobject] -and $a.($p.Name) -is [pscustomobject]) { Merge $a.($p.Name) $p.Value }
    else { $a | Add-Member -Force -NotePropertyName $p.Name -NotePropertyValue $p.Value }
  }
}
$settingsPath = Join-Path $claude 'settings.json'
$settings = if (Test-Path $settingsPath) { Copy-Item $settingsPath (Join-Path $backups "settings.json.$stamp"); Read-Utf8 $settingsPath | ConvertFrom-Json } else { [pscustomobject]@{} }
Merge $settings $shc
if ($settings.env) { $settings.env.PSObject.Properties.Remove('CLAUDE_CODE_AUTO_COMPACT_WINDOW') }  # would override modelSettings.autoCompactWindow
$settings.PSObject.Properties.Remove('autoCompactWindow')  # global window; the per-model ones replace it

# The old session-start git pull goes only once the config-sync mod is installed AND has run (its store holds a pull), so a device is never left without a pull; every other hook stays.
$hasSync = $false
if ($haveClaude -and (@(Claude-Json plugin list --json | Where-Object { $_.scope -eq 'user' -and $_.id -eq 'config-sync@berkays-mods' }).Count -gt 0)) {
  foreach ($f in @(Get-ChildItem (Join-Path $claude 'plugins\store') -Filter 'config-sync_*.json' -ErrorAction SilentlyContinue)) {
    try { if ((Read-Utf8 $f.FullName | ConvertFrom-Json).'last-pull'.at) { $hasSync = $true } } catch {}
  }
}
$pull = "git -C `"$($shared -replace '\\','/')`" pull --ff-only -q"
if ($hasSync -and $settings.hooks -and $settings.hooks.SessionStart) {
  $start = @(foreach ($g in @($settings.hooks.SessionStart)) {
    if ($g.hooks) {
      $keep = @($g.hooks | Where-Object { $_.command -ne $pull })
      if ($keep.Count -eq 0) { continue }
      if ($keep.Count -ne @($g.hooks).Count) { $g.hooks = $keep }
    }
    $g
  })
  if ($start.Count) { $settings.hooks | Add-Member -Force -NotePropertyName SessionStart -NotePropertyValue $start }
  else { $settings.hooks.PSObject.Properties.Remove('SessionStart') }
}
Write-Utf8 "$settingsPath.tmp" ($settings | ConvertTo-Json -Depth 32)
Move-Item -Force "$settingsPath.tmp" $settingsPath  # a crash never leaves a half-written settings.json

# 5. Marker: which settings.shared.json is applied; the config-sync mod keeps reporting changes until it matches. Only after every plugin installed.
if ($exitCode -eq 0) {
  $marker = Join-Path $claude 'claude-config.installed'
  Write-Utf8 "$marker.tmp" "$want`n"
  Move-Item -Force "$marker.tmp" $marker
  Write-Host "Installed. Backups in $backups."
} else { [Console]::Error.WriteLine('Installed with plugin errors (see above); marker not written, so /config-sync will keep reporting changes.') }
} finally { $lockFile.Dispose(); Remove-Item $lock, $snap -Force -ErrorAction SilentlyContinue }
exit $exitCode
