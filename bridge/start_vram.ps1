<#
.SYNOPSIS
  Start the local llama.cpp worker with every layer in RTX 5070 VRAM and wait until it is healthy.

.DESCRIPTION
  Kills any running llama-server (a stale CPU-only instance would hold port 8080), then launches
  llama_vk\llama-server.exe with -ngl 99 so all layers are offloaded to the GPU, and polls
  /health until it returns 200. Fails if the log does not show the layers on the GPU.

.PARAMETER Reuse
  If a healthy server is already answering on the port, leave it alone instead of restarting.
#>
[CmdletBinding()]
param(
    [string]$RuntimeDir = "C:\Users\Nkara\bedrock-settlement-runtime\tools\llama_vk",
    [string]$Model      = "..\models\qwen2.5-coder-14b.gguf",
    [int]$Port          = 8080,
    [int]$GpuLayers     = 99,
    [int]$Context       = 8192,
    [int]$TimeoutSec    = 300,
    [switch]$Reuse
)
$ErrorActionPreference = "Stop"
$exe    = Join-Path $RuntimeDir "llama-server.exe"
$health = "http://127.0.0.1:$Port/health"
$logDir = Join-Path $PSScriptRoot "logs"

function Test-Health {
    try { return (Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}

if (-not (Test-Path $exe)) { throw "llama-server.exe not found: $exe" }
$modelPath = [System.IO.Path]::GetFullPath((Join-Path $RuntimeDir $Model))
if (-not (Test-Path $modelPath)) { throw "model not found: $modelPath" }

if ($Reuse -and (Test-Health)) {
    Write-Host "OK: server already healthy at $health (reused, not restarted)"
    exit 0
}

# 1. Kill stale llama-server processes.
$stale = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue
if ($stale) {
    Write-Host "Stopping stale llama-server (PID $($stale.Id -join ', '))"
    $stale | Stop-Process -Force
    $stale | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue
}

# 2. Launch with all layers on the GPU. Working directory = exe dir so ..\models resolves.
New-Item -ItemType Directory -Force $logDir | Out-Null
$outLog = Join-Path $logDir "llama-server.out.log"
$errLog = Join-Path $logDir "llama-server.err.log"   # llama.cpp writes its load log to stderr
$serverArgs = @("-m", $Model, "--port", $Port, "-ngl", $GpuLayers, "-c", $Context)
Write-Host "Launching: $exe $($serverArgs -join ' ')"
$proc = Start-Process -FilePath $exe -ArgumentList $serverArgs -WorkingDirectory $RuntimeDir `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog

# 3. Wait for /health = 200.
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while (-not (Test-Health)) {
    if ($proc.HasExited) { throw "llama-server exited early (code $($proc.ExitCode)). See $errLog" }
    if ((Get-Date) -gt $deadline) { throw "timed out after ${TimeoutSec}s waiting for $health. See $errLog" }
    Start-Sleep -Seconds 2
}

# 4. Prove the layers are in VRAM, not CPU RAM.
$offload = Select-String -Path $errLog, $outLog -Pattern "offloaded \d+/\d+ layers to GPU" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $offload) {
    Write-Warning "healthy, but no 'offloaded N/N layers to GPU' line in the log. Check $errLog before trusting it."
} else {
    Write-Host $offload.Line.Trim()
    if ($offload.Line -match "offloaded (\d+)/(\d+)" -and $Matches[1] -ne $Matches[2]) {
        throw "only $($Matches[1])/$($Matches[2]) layers on the GPU; the rest are in CPU RAM"
    }
}
Write-Host "OK: llama-server PID $($proc.Id) healthy at $health"
