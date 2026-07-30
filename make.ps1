# Windows equivalent of the Makefile.
#
#   .\make.ps1 install
#   .\make.ps1 api        # terminal 1
#   .\make.ps1 agent      # terminal 2
#   .\make.ps1 ops        # terminal 3  -> http://127.0.0.1:8100
#
# The Makefile relies on make, sed and pkill, none of which ship with Windows.
# The Python itself is portable: nothing imports a posix-only module, and
# uvloop excludes itself on win32 via its own marker, so the dependency set
# installs cleanly.
#
# If anything here misbehaves, WSL2 runs the Makefile unchanged and is the
# path this project was developed and tested on.

param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'api', 'agent', 'console', 'ops', 'test', 'eval',
                 'smoke', 'measure', 'voices', 'reset', 'clean-logs', 'stop',
                 'stop-api', 'vendor', 'help')]
    [string]$Target = 'help',

    [int]$ApiPort = 0
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$Py  = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$Pip = Join-Path $PSScriptRoot '.venv\Scripts\pip.exe'

function Get-ApiPort {
    if ($ApiPort -gt 0) { return $ApiPort }
    # Same rule as the Makefile: take the port from RESERVATION_API_URL so the
    # API always starts where the agent is looking.
    if (Test-Path '.env') {
        $line = Select-String -Path '.env' -Pattern '^RESERVATION_API_URL=.*?:(\d+)' |
                Select-Object -First 1
        if ($line) { return [int]$line.Matches[0].Groups[1].Value }
    }
    return 8000
}

function Assert-Venv {
    if (-not (Test-Path $Py)) {
        throw "No virtualenv found. Run:  .\make.ps1 install"
    }
}

function Test-Api([int]$Port) {
    try {
        Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null
        return $true
    } catch { return $false }
}

# Matching on the command line is the only way to find these: they are all
# python.exe, so a name-based kill would take every Python process with them.
function Stop-Matching([string]$Pattern, [string]$Label) {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
             Where-Object { $_.CommandLine -and $_.CommandLine -match $Pattern }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Host "stopped $Label ($($procs.Count))"
}

function Invoke-Vendor {
    $dest = Join-Path $PSScriptRoot 'ops\vendor'
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $out = Join-Path $dest 'livekit-client.umd.min.js'
    Invoke-WebRequest -Uri 'https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js' `
                      -OutFile $out -UseBasicParsing
    Write-Host "vendored $((Get-Item $out).Length) bytes -> ops\vendor"
}

$port = Get-ApiPort

switch ($Target) {

    'install' {
        python -m venv .venv
        & $Pip install --upgrade pip
        & $Pip install -r requirements.txt
        # Silero VAD and the turn-detector weights, so the first call does not
        # pay for the download.
        & $Py scripts\download_models.py
        if (-not (Test-Path 'ops\vendor\livekit-client.umd.min.js')) { Invoke-Vendor }
        if (-not (Test-Path '.env')) {
            Copy-Item '.env.example' '.env'
            Write-Host "`nCreated .env from the example — fill in your keys before running." -ForegroundColor Yellow
        }
        Write-Host "`nDone. Next:  .\make.ps1 api" -ForegroundColor Green
    }

    'api' {
        Assert-Venv
        if (Test-Api $port) {
            Write-Host "A reservation API is already serving :$port - reusing it."
            Write-Host "To restart it:  .\make.ps1 stop-api  then  .\make.ps1 api"
        } else {
            & (Join-Path $PSScriptRoot '.venv\Scripts\uvicorn.exe') `
                app:app --host 127.0.0.1 --port $port --app-dir mock_api
        }
    }

    'agent'   { Assert-Venv; $env:PYTHONPATH = 'src'; & $Py -m luma.worker dev }
    'console' { Assert-Venv; $env:PYTHONPATH = 'src'; & $Py -m luma.worker console }
    'ops'     { Assert-Venv; & $Py ops\server.py }

    'test' {
        Assert-Venv
        if (-not (Test-Api $port)) {
            Write-Host "The mock API is not running on :$port. Start it first:  .\make.ps1 api" -ForegroundColor Red
            exit 1
        }
        & (Join-Path $PSScriptRoot '.venv\Scripts\pytest.exe') -q
    }

    'eval'    { Assert-Venv; & $Py eval\run_evals.py }
    'smoke'   { Assert-Venv; & $Py scripts\smoke_call.py }
    'measure' { Assert-Venv; & $Py scripts\measure_speech_latency.py --runs 6 }
    'voices'  { Assert-Venv; & $Py scripts\voice_samples.py }
    'vendor'  { Invoke-Vendor }

    'reset' {
        Invoke-RestMethod -Method Post "http://127.0.0.1:$port/admin/reset" | ConvertTo-Json -Compress
    }

    'clean-logs' {
        Invoke-RestMethod -Method Post "http://127.0.0.1:$port/admin/reset" | Out-Null
        Remove-Item 'logs\*.jsonl' -ErrorAction SilentlyContinue
        Write-Host "API reset and event feed cleared."
    }

    'stop' {
        Stop-Matching 'uvicorn|app:app'  'reservation api'
        Stop-Matching 'ops.\\?server\.py' 'ops console'
        Stop-Matching 'luma\.worker'      'agent worker'
    }

    'stop-api' { Stop-Matching 'uvicorn|app:app' 'reservation api' }

    default {
        Write-Host @"
Luma Bistro voice agent - Windows commands

  .\make.ps1 install      create .venv, install deps, fetch model weights
  .\make.ps1 api          mock reservation API        (terminal 1)
  .\make.ps1 agent        voice worker                (terminal 2)
  .\make.ps1 ops          call widget + console       (terminal 3)
                          -> http://127.0.0.1:8100

  .\make.ps1 test         81 tests (needs api running)
  .\make.ps1 eval         the 7 standard scenarios
  .\make.ps1 smoke        place a real call end to end
  .\make.ps1 voices       render Deepgram voice samples
  .\make.ps1 clean-logs   reset API state + clear the feed (before a demo take)
  .\make.ps1 stop         stop api, agent and ops

  .\make.ps1 console      terminal microphone, no LiveKit account needed

Add -ApiPort 9000 to override the port taken from .env.
"@
    }
}
