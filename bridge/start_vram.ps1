<#
.SYNOPSIS
  Start the local llama.cpp nodes with every layer in RTX 5070 VRAM and wait until they are healthy.

.DESCRIPTION
  -Mode worker-only  Qwen 14B code worker on :8080 (10.3.1.1).                      [default]
  -Mode concurrent   Qwen 14B on :8080 plus Gemma (E4B + mmproj) eyes on :8081 (10.2.1.1).

  Kills stale llama-server processes first (unless -Reuse), launches with -ngl 99, polls /health
  until 200 and checks the log for "offloaded N/N layers to GPU".

  concurrent first estimates the VRAM it needs (weights + a per-server allowance for KV cache and
  compute buffers) and REFUSES if that exceeds the card, before anything is stopped. Qwen 14B at
  Q4 is ~8.6 GiB of weights on its own, so on a 12 GiB card this only passes with a much smaller
  Gemma / Qwen quant. -Force skips the check at your own risk.

.PARAMETER Reuse
  Leave an already-healthy node alone; only start the ones that are not answering. Never kills.
#>
[CmdletBinding()]
param(
    [ValidateSet("worker-only", "concurrent")][string]$Mode = "worker-only",
    [string]$RuntimeDir    = "C:\Users\Nkara\bedrock-settlement-runtime\tools\llama_vk",
    [string]$Model         = "..\models\qwen2.5-coder-14b.gguf",
    [int]$Port             = 8080,
    [int]$GpuLayers        = 99,
    [int]$Context          = 8192,
    [string]$GemmaModel    = "..\models\gemma-4-e4b-it-Q4_K_M.gguf",
    [string]$GemmaMmproj   = "..\models\mmproj-gemma-4-e4b-it-f16.gguf",
    [int]$GemmaPort        = 8081,
    [int]$GemmaGpuLayers   = 99,
    [int]$GemmaContext     = 4096,
    [int]$PerServerOverheadMiB = 1500,
    [int]$TimeoutSec       = 300,
    [switch]$Reuse,
    [switch]$Force
)
$ErrorActionPreference = "Stop"
$exe    = Join-Path $RuntimeDir "llama-server.exe"
$logDir = Join-Path $PSScriptRoot "logs"

function Resolve-Rel([string]$p) {
    if ([System.IO.Path]::IsPathRooted($p)) { return [System.IO.Path]::GetFullPath($p) }
    [System.IO.Path]::GetFullPath((Join-Path $RuntimeDir $p))
}
function Test-Health([int]$p) {
    try { return (Invoke-WebRequest -Uri "http://127.0.0.1:$p/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}
function MiB([string]$path) { [math]::Ceiling((Get-Item $path).Length / 1MB) }

function Start-Node([string]$Name, [string]$ModelPath, [int]$NodePort, [int]$Ctx, [int]$Ngl, [string[]]$Extra) {
    if ($Reuse -and (Test-Health $NodePort)) {
        Write-Host "OK: $Name already healthy on :$NodePort (reused, not restarted)"
        return
    }
    New-Item -ItemType Directory -Force $logDir | Out-Null
    $outLog = Join-Path $logDir "$Name.out.log"
    $errLog = Join-Path $logDir "$Name.err.log"     # llama.cpp writes its load log to stderr
    $argList = @("-m", $ModelPath) + $Extra + @("--port", $NodePort, "-ngl", $Ngl, "-c", $Ctx)
    Write-Host "Launching ${Name}: llama-server $($argList -join ' ')"
    $proc = Start-Process -FilePath $exe -ArgumentList $argList -WorkingDirectory $RuntimeDir `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not (Test-Health $NodePort)) {
        if ($proc.HasExited) { throw "$Name exited early (code $($proc.ExitCode)). See $errLog" }
        if ((Get-Date) -gt $deadline) { throw "$Name timed out after ${TimeoutSec}s on :$NodePort. See $errLog" }
        Start-Sleep -Seconds 2
    }
    $hit = Select-String -Path $errLog, $outLog -Pattern "offloaded (\d+)/(\d+) layers to GPU" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $hit) {
        Write-Warning "$Name is healthy but its log has no 'offloaded N/N layers to GPU' line. Check $errLog."
    } else {
        Write-Host "${Name}: $($hit.Line.Trim())"
        if ($hit.Line -match "offloaded (\d+)/(\d+)" -and $Matches[1] -ne $Matches[2] -and $Ngl -ge 99) {
            throw "$Name has only $($Matches[1])/$($Matches[2]) layers on the GPU; the rest are in CPU RAM"
        }
    }
    Write-Host "OK: $Name PID $($proc.Id) healthy on :$NodePort"
}

# ---- preflight (nothing is stopped until this passes) -------------------------------------
if (-not (Test-Path $exe)) { throw "llama-server.exe not found: $exe" }
$qwen = Resolve-Rel $Model
if (-not (Test-Path $qwen)) { throw "worker model not found: $qwen" }
$gemma = $null; $mmproj = $null
if ($Mode -eq "concurrent") {
    $gemma  = Resolve-Rel $GemmaModel
    $mmproj = Resolve-Rel $GemmaMmproj
    foreach ($f in @($gemma, $mmproj)) {
        if (-not (Test-Path $f)) {
            throw "sensor file not found: $f`nDownload a multimodal Gemma GGUF and its mmproj file, then pass -GemmaModel / -GemmaMmproj."
        }
    }
    $need = (MiB $qwen) + (MiB $gemma) + (MiB $mmproj) + 2 * $PerServerOverheadMiB
    $total = $null
    try { $total = [int](& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1) } catch {}
    if ($null -eq $total) {
        Write-Warning "nvidia-smi not available; skipping the VRAM budget check."
    } else {
        Write-Host ("VRAM budget: need ~{0} MiB (weights {1} + {2} + {3}, plus {4} per server), card has {5} MiB" -f `
            $need, (MiB $qwen), (MiB $gemma), (MiB $mmproj), $PerServerOverheadMiB, $total)
        if ($need -gt $total -and -not $Force) {
            throw ("concurrent mode does not fit: ~$need MiB needed, $total MiB available. Nothing was stopped.`n" +
                   "Use -Mode worker-only, a smaller quant of either model, or -Force to try anyway.")
        }
    }
}

# ---- stop stale servers, then start ---------------------------------------------------------
if (-not $Reuse) {
    $stale = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue
    if ($stale) {
        Write-Host "Stopping stale llama-server (PID $($stale.Id -join ', '))"
        $stale | Stop-Process -Force
        $stale | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue
    }
}
Start-Node "worker" $qwen $Port $Context $GpuLayers @()
if ($Mode -eq "concurrent") {
    Start-Node "eyes" $gemma $GemmaPort $GemmaContext $GemmaGpuLayers @("--mmproj", $mmproj)
}
$msg = "OK: mode '$Mode' ready. worker http://127.0.0.1:$Port"
if ($Mode -eq "concurrent") { $msg += ", eyes http://127.0.0.1:$GemmaPort" }
Write-Host $msg
