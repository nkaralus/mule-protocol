<#
.SYNOPSIS
  Boot the local llama.cpp nodes (Vulkan build, RTX 5070) in one of four modes and wait until healthy.

.DESCRIPTION
  -Mode worker-only    Qwen 14B code worker on :8080 (10.3.1.1).                              [default]
  -Mode sequential     Day shift. Only ONE model is ever in VRAM. -Phase eyes boots Gemma E4B + mmproj
                       on :8081; -Phase worker stops it and boots Qwen on :8080. mule_tool.py calls
                       the right phase before each slot, so neither model overflows the 12 GiB card.
  -Mode concurrent     Qwen :8080 + Gemma E4B :8081 together. Refuses unless the VRAM budget fits.
  -Mode overnight-26b  Night shift. ONE Gemma 4 26B A4B (MoE) server with its mmproj serves BOTH roles
                       on :8080 (mule_tool.py is pointed at it for eyes too). Hot experts stay in VRAM,
                       cold experts stream from RAM via --n-cpu-moe (or your own -OverrideTensor).
                       Needs a GGUF; see the message printed if it is missing. 3-strike NAK failures are
                       logged to bridge/dlq.jsonl by the bus.

  Every mode stops stale llama-server processes first (unless -Reuse), launches with -ngl 99, polls
  /health until 200 and checks the log for "offloaded N/N layers to GPU". Preflight (files, VRAM/RAM
  budget) runs BEFORE anything is stopped. -DryRun does the preflight and prints the plan, nothing else.
  On success the mode is saved to ~\.mule\mode.json so mule_tool.py knows the URLs and whether to swap.

.PARAMETER Phase
  sequential only: which node to bring up. Idempotent; a node that is already the only one running is
  left alone.
.PARAMETER Reuse
  Keep already-healthy nodes and start only missing ones. Never kills.
.PARAMETER Force
  Skip the VRAM/RAM budget refusal.
#>
[CmdletBinding()]
param(
    [ValidateSet("worker-only", "sequential", "concurrent", "overnight-26b")][string]$Mode = "worker-only",
    [ValidateSet("eyes", "worker")][string]$Phase = "worker",
    [string]$RuntimeDir    = "C:\Users\Nkara\bedrock-settlement-runtime\tools\llama_vk",
    [string]$Model         = "..\models\qwen2.5-coder-14b.gguf",
    [int]$Port             = 8080,
    [int]$GpuLayers        = 99,
    [int]$Context          = 8192,
    [string]$GemmaModel    = "..\models\gemma-4-E4B-it-Q4_0.gguf",
    [string]$GemmaMmproj   = "..\models\mmproj-gemma-4-E4B-it-BF16.gguf",
    [int]$GemmaPort        = 8081,
    [int]$GemmaGpuLayers   = 99,
    [int]$GemmaContext     = 4096,
    [string]$Big26bModel   = "C:\mule_models\gemma26b-gguf\gemma-4-26b-a4b-it-Q4_K_M.gguf",
    [string]$Big26bMmproj  = "C:\mule_models\gemma26b-gguf\mmproj-gemma-4-26b-a4b-it-BF16.gguf",
    [int]$Big26bPort       = 8080,
    [int]$Big26bContext    = 8192,
    [int]$Big26bLayers     = 30,    # gemma26b/config.json: num_hidden_layers
    [int]$HotExpertLayers  = 8,     # layers whose experts stay in VRAM; the other layers' experts go to RAM
    [string]$OverrideTensor = "",   # raw -ot value, replaces the --n-cpu-moe split
    [int]$PerServerOverheadMiB = 1500,
    [int]$HostReserveMiB   = 4096,  # RAM left for Windows and everything else
    [int]$TimeoutSec       = 600,
    [switch]$Reuse,
    [switch]$Force,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$exe    = Join-Path $RuntimeDir "llama-server.exe"
$logDir = Join-Path $PSScriptRoot "logs"
$modeFile = if ($env:MULE_MODE_FILE) { $env:MULE_MODE_FILE } else { Join-Path $HOME ".mule\mode.json" }

function Resolve-Rel([string]$p) {
    if ([System.IO.Path]::IsPathRooted($p)) { return [System.IO.Path]::GetFullPath($p) }
    [System.IO.Path]::GetFullPath((Join-Path $RuntimeDir $p))
}
function Test-Health([int]$p) {
    try { return (Invoke-WebRequest -Uri "http://127.0.0.1:$p/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}
function MiB([string]$path) { [math]::Ceiling((Get-Item $path).Length / 1MB) }
function Get-VramTotalMiB {
    try { return [int](& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1) } catch { return $null }
}
function Get-VramUsedMiB {
    try { return [int](& nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | Select-Object -First 1) } catch { return $null }
}
function Get-RamTotalMiB { [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB) }
function Assert-Files([string[]]$Paths, [string]$Hint) {
    foreach ($f in $Paths) { if (-not (Test-Path $f)) { throw "file not found: $f`n$Hint" } }
}
function Assert-Budget([string]$What, [int]$NeedMiB, $HaveMiB, [string]$Advice) {
    if ($null -eq $HaveMiB) { Write-Warning "${What}: budget check skipped (no figure available)"; return }
    Write-Host ("Budget ({0}): need ~{1} MiB, have {2} MiB" -f $What, $NeedMiB, $HaveMiB)
    if ($NeedMiB -gt $HaveMiB -and -not $Force) {
        throw "$What does not fit: ~$NeedMiB MiB needed, $HaveMiB MiB available. Nothing was stopped.`n$Advice -Force skips this check."
    }
}
function Stop-LlamaServers {
    $stale = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue
    if ($stale) {
        Write-Host "Stopping llama-server (PID $($stale.Id -join ', '))"
        $stale | Stop-Process -Force
        $stale | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2    # let the driver hand the VRAM back
    }
}
function Save-Mode([string]$WorkerUrl, [string]$EyesUrl) {
    New-Item -ItemType Directory -Force (Split-Path $modeFile) | Out-Null
    $obj = [ordered]@{ mode = $Mode; worker_url = $WorkerUrl; eyes_url = $EyesUrl
                       script = $PSCommandPath; updated = (Get-Date).ToString("s") }
    [System.IO.File]::WriteAllText($modeFile, ($obj | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
}

function Start-Node([string]$Name, [string]$ModelPath, [int]$NodePort, [int]$Ctx, [int]$Ngl, [string[]]$Extra) {
    $argList = @("-m", $ModelPath) + $Extra + @("--port", $NodePort, "-ngl", $Ngl, "-c", $Ctx)
    if ($DryRun) { Write-Host "DRYRUN launch ${Name}: llama-server $($argList -join ' ')"; return }
    if ($Reuse -and (Test-Health $NodePort)) {
        Write-Host "OK: $Name already healthy on :$NodePort (reused, not restarted)"
        return
    }
    New-Item -ItemType Directory -Force $logDir | Out-Null
    $outLog = Join-Path $logDir "$Name.out.log"
    $errLog = Join-Path $logDir "$Name.err.log"     # llama.cpp writes its load log to stderr
    Write-Host "Launching ${Name}: llama-server $($argList -join ' ')"
    $vramBefore = Get-VramUsedMiB
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
        # Newer builds log quietly. Fall back to the proof that matters: VRAM actually grew by ~the model size.
        $vramAfter = Get-VramUsedMiB
        $modelMiB = MiB $ModelPath
        if ($null -ne $vramBefore -and $null -ne $vramAfter -and ($vramAfter - $vramBefore) -ge (0.6 * $modelMiB)) {
            Write-Host ("${Name}: VRAM grew by {0} MiB for a {1} MiB model (no offload line in this build's log)" -f ($vramAfter - $vramBefore), $modelMiB)
        } else {
            Write-Warning "$Name is healthy but there is no offload line in the log and VRAM did not grow by the model size. Check $errLog; weights may be in CPU RAM."
        }
    } else {
        Write-Host "${Name}: $($hit.Line.Trim())"
        if ($hit.Line -match "offloaded (\d+)/(\d+)" -and $Matches[1] -ne $Matches[2] -and $Ngl -ge 99) {
            throw "$Name has only $($Matches[1])/$($Matches[2]) layers on the GPU; the rest are in CPU RAM"
        }
    }
    Write-Host "OK: $Name PID $($proc.Id) healthy on :$NodePort"
}

# ---- preflight (nothing is stopped until this passes) ---------------------------------------
if (-not (Test-Path $exe)) { throw "llama-server.exe not found: $exe" }
$qwen = Resolve-Rel $Model
$vram = Get-VramTotalMiB
$gemma = Resolve-Rel $GemmaModel
$mmproj = Resolve-Rel $GemmaMmproj
$gemmaHint = "Put a multimodal Gemma GGUF and its mmproj in tools\models, or pass -GemmaModel / -GemmaMmproj."

switch ($Mode) {
    "worker-only" { Assert-Files @($qwen) "Pass -Model." }
    "concurrent" {
        Assert-Files @($qwen, $gemma, $mmproj) $gemmaHint
        Assert-Budget "concurrent (Qwen + Gemma)" ((MiB $qwen) + (MiB $gemma) + (MiB $mmproj) + 2 * $PerServerOverheadMiB) $vram `
            "Use -Mode sequential (one model at a time) or -Mode worker-only."
    }
    "sequential" {
        if ($Phase -eq "eyes") {
            Assert-Files @($gemma, $mmproj) $gemmaHint
            Assert-Budget "eyes alone (Gemma)" ((MiB $gemma) + (MiB $mmproj) + $PerServerOverheadMiB) $vram "Use a smaller Gemma quant."
        } else {
            Assert-Files @($qwen) "Pass -Model."
            Assert-Budget "worker alone (Qwen)" ((MiB $qwen) + $PerServerOverheadMiB) $vram "Use a smaller Qwen quant or -Context."
        }
    }
    "overnight-26b" {
        $big = Resolve-Rel $Big26bModel
        $bigMm = Resolve-Rel $Big26bMmproj
        $convertHelp = @"
llama-server needs a GGUF. C:\mule_models\gemma26b is safetensors (bf16, ~48 GiB), gemma26b-split is the same
weights in 29 shards, and gemma26b-int8experts is a custom layer_*.bin format. None of them load in llama.cpp.
Convert once (needs the llama.cpp repo's convert_hf_to_gguf.py, torch and transformers; none is on this machine):
  python convert_hf_to_gguf.py C:\mule_models\gemma26b --outtype bf16 --outfile C:\mule_models\gemma26b-gguf\gemma-4-26b-a4b-it-BF16.gguf
  python convert_hf_to_gguf.py C:\mule_models\gemma26b --mmproj --outfile $Big26bMmproj
  $RuntimeDir\llama-quantize.exe <bf16.gguf> $Big26bModel Q4_K_M
then pass -Big26bModel / -Big26bMmproj if you used other names.
"@
        Assert-Files @($big, $bigMm) $convertHelp
        $hot = [math]::Max(0, [math]::Min($HotExpertLayers, $Big26bLayers))
        $cold = $Big26bLayers - $hot
        $sizeMiB = MiB $big
        $coldMiB = [int]($sizeMiB * $cold / $Big26bLayers)
        Assert-Budget "26B hot experts + attention in VRAM" (($sizeMiB - $coldMiB) + (MiB $bigMm) + $PerServerOverheadMiB) $vram `
            "Lower -HotExpertLayers or use a smaller quant."
        Assert-Budget "26B cold experts in RAM" ($coldMiB + $HostReserveMiB) (Get-RamTotalMiB) `
            "Raise -HotExpertLayers, use a smaller quant, or close other programs. 16 GiB of RAM is the limit here."
    }
}

# ---- run ------------------------------------------------------------------------------------
$workerUrl = "http://127.0.0.1:$Port"
$eyesUrl   = "http://127.0.0.1:$GemmaPort"

if ($Mode -eq "sequential" -and -not $Reuse -and -not $DryRun) {
    # Idempotent phases: the wanted node is already the only one up -> nothing to do.
    if ($Phase -eq "eyes") { $wantPort = $GemmaPort; $otherPort = $Port } else { $wantPort = $Port; $otherPort = $GemmaPort }
    if ((Test-Health $wantPort) -and -not (Test-Health $otherPort)) {
        Write-Host "OK: sequential phase '$Phase' already active on :$wantPort"
        Save-Mode $workerUrl $eyesUrl
        exit 0
    }
}
if ($DryRun) { Write-Host "DRYRUN: would stop all llama-server processes first" }
elseif (-not $Reuse) { Stop-LlamaServers }

switch ($Mode) {
    "worker-only" { Start-Node "worker" $qwen $Port $Context $GpuLayers @() }
    "concurrent"  {
        Start-Node "worker" $qwen $Port $Context $GpuLayers @()
        Start-Node "eyes" $gemma $GemmaPort $GemmaContext $GemmaGpuLayers @("--mmproj", $mmproj)
    }
    "sequential"  {
        if ($Phase -eq "eyes") { Start-Node "eyes" $gemma $GemmaPort $GemmaContext $GemmaGpuLayers @("--mmproj", $mmproj) }
        else                   { Start-Node "worker" $qwen $Port $Context $GpuLayers @() }
    }
    "overnight-26b" {
        if ($OverrideTensor) { $placement = @("-ot", $OverrideTensor) } else { $placement = @("--n-cpu-moe", $cold) }
        Start-Node "big26b" $big $Big26bPort $Big26bContext 99 (@("--mmproj", $bigMm) + $placement)
        $workerUrl = "http://127.0.0.1:$Big26bPort"
        $eyesUrl   = $workerUrl      # one process serves both roles
    }
}
if ($DryRun) { Write-Host "DRYRUN: mode '$Mode' preflight passed. Nothing was stopped, started or saved."; exit 0 }

Save-Mode $workerUrl $eyesUrl
$msg = "OK: mode '$Mode' ready. worker $workerUrl, eyes $eyesUrl"
if ($Mode -eq "sequential") { $msg += " (phase $Phase; the other node boots on demand)" }
Write-Host $msg
