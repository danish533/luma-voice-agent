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
                 'stop-api', 'vendor', 'up', 'down', 'logs', 'ps',
                 'docker-test', 'help')]
    [string]$Target = 'help',

    [int]$ApiPort = 0
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Windows venvs put executables in Scripts\, POSIX ones in bin/. Detecting it
# rather than hardcoding lets this script be exercised under pwsh on Linux and
# macOS too -- which is the only way it gets tested before you run it.
$VenvBin = if ($IsWindows -ne $false) { '.venv/Scripts' } else { '.venv/bin' }
$Exe     = if ($IsWindows -ne $false) { '.exe' } else { '' }

$Py      = Join-Path $PSScriptRoot "$VenvBin/python$Exe"
$Pip     = Join-Path $PSScriptRoot "$VenvBin/pip$Exe"
$Uvicorn = Join-Path $PSScriptRoot "$VenvBin/uvicorn$Exe"
$Pytest  = Join-Path $PSScriptRoot "$VenvBin/pytest$Exe"

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

# Matching on the command line is the only way to find these: all three are
# python, so a name-based kill would take every unrelated Python process with
# them. Get-CimInstance is Windows-only, hence the pgrep path for pwsh on
# Linux and macOS.
function Stop-Matching([string]$Pattern, [string]$Label) {
    $count = 0
    if (Get-Command Get-CimInstance -ErrorAction SilentlyContinue) {
        $procs = Get-CimInstance Win32_Process |
                 Where-Object { $_.CommandLine -and $_.CommandLine -match $Pattern }
        foreach ($p in $procs) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            $count++
        }
    } else {
        # Bracket the first character so pgrep does not match its own argv.
        $safe = $Pattern -replace '^(.)', '[$1]'
        $ids = (& pgrep -f $safe 2>$null)
        foreach ($id in $ids) {
            Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
            $count++
        }
    }
    Write-Host "stopped $Label ($count)"
}

# Delegates to the Python fetcher, which is also what the Docker build runs.
# One copy of the URL, and one place where a bad download gets caught.
function Invoke-Vendor {
    Assert-Venv
    & $Py scripts/fetch_vendor.py
}

$port = Get-ApiPort

switch ($Target) {

    'install' {
        python -m venv .venv
        & $Pip install --upgrade pip
        & $Pip install -r requirements.txt
        # Silero VAD and the turn-detector weights, so the first call does not
        # pay for the download.
        & $Py scripts/download_models.py
        if (-not (Test-Path 'ops/vendor/livekit-client.umd.min.js')) { Invoke-Vendor }
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
            Write-Host ''
            Write-Host '  Reservation API - data only, there is NO web page here.'
            Write-Host "  Opening http://127.0.0.1:$port/ in a browser returns 404, which is correct."
            Write-Host "    check it:  curl http://127.0.0.1:$port/health"
            Write-Host '    the UI is: http://127.0.0.1:8100   <- run ".\make.ps1 ops" in another terminal'
            Write-Host ''
            & $Uvicorn app:app --host 127.0.0.1 --port $port --app-dir mock_api
        }
    }

    'agent'   { Assert-Venv; $env:PYTHONPATH = 'src'; & $Py -m luma.worker dev }
    'console' { Assert-Venv; $env:PYTHONPATH = 'src'; & $Py -m luma.worker console }
    'ops'     { Assert-Venv; & $Py ops/server.py }

    'test' {
        Assert-Venv
        if (-not (Test-Api $port)) {
            Write-Host "The mock API is not running on :$port. Start it first:  .\make.ps1 api" -ForegroundColor Red
            exit 1
        }
        & $Pytest -q
    }

    'eval'    { Assert-Venv; & $Py eval/run_evals.py }
    'smoke'   { Assert-Venv; & $Py scripts/smoke_call.py }
    'measure' { Assert-Venv; & $Py scripts/measure_speech_latency.py --runs 6 }
    'voices'  { Assert-Venv; & $Py scripts/voice_samples.py }
    'vendor'  { Invoke-Vendor }

    'reset' {
        Invoke-RestMethod -Method Post "http://127.0.0.1:$port/admin/reset" | ConvertTo-Json -Compress
    }

    'clean-logs' {
        Invoke-RestMethod -Method Post "http://127.0.0.1:$port/admin/reset" | Out-Null
        Remove-Item 'logs/*.jsonl' -ErrorAction SilentlyContinue
        Write-Host "API reset and event feed cleared."
    }

    'stop' {
        Stop-Matching 'uvicorn|app:app'  'reservation api'
        Stop-Matching 'ops.\\?server\.py' 'ops console'
        Stop-Matching 'luma\.worker'      'agent worker'
    }

    'stop-api' { Stop-Matching 'uvicorn|app:app' 'reservation api' }

    # --- Docker ------------------------------------------------------------
    # Needs nothing from the venv: the image carries its own Python, its own
    # dependencies and the model weights. This is the shortest route to a
    # working demo on Windows.
    'up' {
        docker compose up --build -d
        Write-Host ''
        docker compose ps
        Write-Host ''
        Write-Host '  console   http://localhost:8100'
        Write-Host '  metrics   http://localhost:9091/metrics'
        Write-Host ''
        Write-Host '  Sign-in password: set OPS_PASSWORD in .env, or read the'
        Write-Host '  generated one from  .\make.ps1 logs  (printed at startup).'
        Write-Host ''
    }

    'down'        { docker compose down }
    'logs'        { docker compose logs -f agent ops }
    'ps'          { docker compose ps }
    'docker-test' { docker compose run --rm tests }

    default {
        # A plain array, not a here-string. PowerShell will not parse a
        # here-string whose lines end in LF only, which is exactly what a git
        # checkout produces unless .gitattributes forces CRLF -- and the parse
        # error it reports points at whatever text happens to follow, not at
        # the delimiter, so it reads as a problem with the help text itself.
        @(
            'Luma Bistro voice agent - Windows commands',
            '',
            'Docker (recommended - no Python, no venv, no model download):',
            '',
            '  .\make.ps1 up           build and start the whole stack',
            '                          http://localhost:8100',
            '  .\make.ps1 logs         follow the worker and console',
            '  .\make.ps1 ps           service health',
            '  .\make.ps1 docker-test  run the suite inside the image',
            '  .\make.ps1 down         stop everything',
            '',
            'Native (needs Python 3.12+):',
            '',
            '  .\make.ps1 install      create .venv, install deps, fetch model weights',
            '  .\make.ps1 api          mock reservation API      (terminal 1)',
            '  .\make.ps1 agent        voice worker              (terminal 2)',
            '  .\make.ps1 ops          call widget + console     (terminal 3)',
            '                          http://127.0.0.1:8100',
            '',
            '  .\make.ps1 test         106 tests (needs api running)',
            '  .\make.ps1 eval         the 7 standard scenarios',
            '  .\make.ps1 smoke        place a real call end to end',
            '  .\make.ps1 voices       render Deepgram voice samples',
            '  .\make.ps1 clean-logs   reset API state and clear the feed',
            '  .\make.ps1 stop         stop api, agent and ops',
            '',
            '  .\make.ps1 console      terminal microphone, no LiveKit account',
            '',
            '  -ApiPort 9000           override the port taken from .env'
        ) | ForEach-Object { Write-Host $_ }
    }
}
