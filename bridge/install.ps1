<#
.SYNOPSIS
  Install the mule bridge for every repo: copy mule_tool.py to ~/tools and add the delegation
  rule to the global ~/.claude/CLAUDE.md.

.DESCRIPTION
  Idempotent. The rule lives between BEGIN/END markers and is replaced in place on re-runs;
  everything else in CLAUDE.md is left alone. A legacy un-marked
  "# Global Mule-Protocol Delegation Rule" section is migrated into the managed block.
  The previous CLAUDE.md is saved as CLAUDE.md.bak the first time it changes.
#>
[CmdletBinding()]
param(
    [string]$ToolsDir  = (Join-Path $HOME "tools"),
    [string]$ClaudeDir = (Join-Path $HOME ".claude")
)
$ErrorActionPreference = "Stop"

# 1. Tool.
$src = Join-Path $PSScriptRoot "mule_tool.py"
if (-not (Test-Path $src)) { throw "missing $src" }
New-Item -ItemType Directory -Force $ToolsDir | Out-Null
$dst = Join-Path $ToolsDir "mule_tool.py"
Copy-Item $src $dst -Force
Write-Host "Installed $dst"

# 2. Global delegation rule (managed block).
$begin = "<!-- mule-bridge:begin -->"
$end   = "<!-- mule-bridge:end -->"
$rule  = @"
$begin
# Global Mule-Protocol Delegation Rule
- NEVER read full source files into your context window or edit code manually when a local GPU worker is active.
- Locate target line numbers using lightweight line-number search (``rg -n``), then delegate bounded edits (``<= 80`` lines per call) to the GBNF-constrained local worker:
  ``python ~/tools/mule_tool.py <file_path> <start_line> <end_line> "<exact slot specification>"``
- The worker runs on the RTX 5070 at http://127.0.0.1:8080. If ``mule_tool.py`` reports a server error, start it with ``bridge/start_vram.ps1`` from the mule-protocol repo.
- Only run tests/builds (``npm test``, ``pytest``, etc.) and verify results after ``mule_tool.py`` reports ``OK``. ``REJECTED BY MULE`` means the file was left untouched.
$end
"@

New-Item -ItemType Directory -Force $ClaudeDir | Out-Null
$claudeMd = Join-Path $ClaudeDir "CLAUDE.md"
$old = if (Test-Path $claudeMd) { [System.IO.File]::ReadAllText($claudeMd) } else { "" }

$markers = [regex]::new("(?s)" + [regex]::Escape($begin) + ".*?" + [regex]::Escape($end))
$legacy  = [regex]::new("(?ms)^# Global Mule-Protocol Delegation Rule\r?\n.*?(?=^# |\z)")
if ($markers.IsMatch($old))     { $new = $markers.Replace($old, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $rule }, 1) }
elseif ($legacy.IsMatch($old))  { $new = $legacy.Replace($old, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $rule + "`r`n" }, 1) }
elseif ($old.Trim().Length -gt 0) { $new = $old.TrimEnd() + "`r`n`r`n" + $rule + "`r`n" }
else                            { $new = $rule + "`r`n" }

if ($new -ne $old) {
    if ($old -and -not (Test-Path "$claudeMd.bak")) { Copy-Item $claudeMd "$claudeMd.bak" }
    [System.IO.File]::WriteAllText($claudeMd, $new, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Updated $claudeMd"
} else {
    Write-Host "$claudeMd already up to date"
}
Write-Host "OK: bridge installed. Next: bridge\start_vram.ps1, then see bridge\README.md for the smoke test."
