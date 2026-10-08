<#
.SYNOPSIS
    FlowOps developer entry points for Windows / PowerShell.

.DESCRIPTION
    Mirrors the Makefile so the project is equally usable without GNU make.
    Every command works on a stock Python 3.11+ and Node 20+; there is nothing
    to install.

.EXAMPLE
    .\dev.ps1 verify          # lint + both test suites
    .\dev.ps1 serve -Port 9000
    .\dev.ps1 demo
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'demo', 'serve', 'serve-memory', 'seed', 'export', 'engine', 'test', 'test-py', 'test-js', 'lint', 'smoke', 'verify', 'ci', 'dist')]
    [string]$Command = 'help',

    [int]$Port = 8787,
    [string]$Base = ''
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'

# Resolve a usable Python: an explicit env var, then PATH, then the bundled DSH runtime.
function Resolve-Python {
    foreach ($candidate in @($env:FLOWOPS_PYTHON, 'python', "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe")) {
        if (-not $candidate) { continue }
        try {
            $resolved = (Get-Command $candidate -ErrorAction Stop).Source
            return $resolved
        } catch { }
        if (Test-Path $candidate) { return $candidate }
    }
    throw 'Python 3.11+ not found. Set FLOWOPS_PYTHON to its full path.'
}

function Resolve-Node {
    foreach ($candidate in @($env:FLOWOPS_NODE, 'node', "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe")) {
        if (-not $candidate) { continue }
        try {
            $resolved = (Get-Command $candidate -ErrorAction Stop).Source
            return $resolved
        } catch { }
        if (Test-Path $candidate) { return $candidate }
    }
    throw 'Node.js 20+ not found. Set FLOWOPS_NODE to its full path.'
}

$Root = $PSScriptRoot
$Py = Resolve-Python
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'

function Invoke-Step {
    param([string]$Workdir, [string]$File, [string[]]$Arguments)
    Write-Host "» $File $($Arguments -join ' ')" -ForegroundColor DarkCyan
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $Workdir -NoNewWindow -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "step failed with exit code $($process.ExitCode)"
    }
}

if (-not $Base) { $Base = "http://127.0.0.1:$Port" }

switch ($Command) {
    'help' {
        @'
  demo           run the built-in policy walkthrough in the terminal
  serve          start the API + console with demo data
  serve-memory   start without touching disk (in-memory store)
  seed           insert the demo dataset into SQLite
  export         regenerate the offline snapshot and the Pyodide engine bundle
  engine         rebuild only the in-browser Python engine bundle
  test           run both test suites
  test-py        run the Python suite
  test-js        run the console unit tests
  lint           policy lint + compile check
  smoke          end-to-end smoke test against a running server
  verify         lint + both test suites
  ci             everything CI runs, ending with a live smoke test
  dist           build the self-contained demo bundle under dist/

  Options: -Port <int>  -Base <url>
'@ | Write-Host
    }
    'demo' { Invoke-Step $Backend $Py @('-m', 'app.cli', 'demo', '--store', 'memory') }
    'serve' { Invoke-Step $Backend $Py @('-m', 'app.cli', 'serve', '--port', "$Port", '--seed') }
    'serve-memory' { Invoke-Step $Backend $Py @('-m', 'app.cli', 'serve', '--store', 'memory', '--port', "$Port") }
    'seed' { Invoke-Step $Backend $Py @('-m', 'app.cli', 'seed', '--reset') }
    'engine' { Invoke-Step $Backend $Py @('scripts/build_frontend_engine.py') }
    'export' {
        $snapshot = Join-Path $Frontend 'data/snapshot.json'
        Invoke-Step $Backend $Py @('-m', 'app.cli', 'export', '--store', 'memory', '--out', $snapshot)
        Invoke-Step $Backend $Py @('scripts/build_frontend_engine.py')
    }
    'test-py' { Invoke-Step $Backend $Py @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.', '-v') }
    'test-js' { Invoke-Step $Frontend (Resolve-Node) @('--test', 'tests/lib.test.js') }
    'test' {
        Invoke-Step $Backend $Py @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.')
        Invoke-Step $Frontend (Resolve-Node) @('--test', 'tests/lib.test.js')
    }
    'lint' {
        Invoke-Step $Backend $Py @('-m', 'app.cli', 'lint')
        Invoke-Step $Backend $Py @('-m', 'app.cli', 'check')
    }
    'smoke' { Invoke-Step $Backend $Py @('scripts/smoke.py', '--base', $Base) }
    'verify' {
        Invoke-Step $Backend $Py @('-m', 'app.cli', 'lint')
        Invoke-Step $Backend $Py @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.')
        Invoke-Step $Frontend (Resolve-Node) @('--test', 'tests/lib.test.js')
    }
    'ci' {
        Invoke-Step $Backend $Py @('-m', 'app.cli', 'lint')
        Invoke-Step $Backend $Py @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.')
        Invoke-Step $Frontend (Resolve-Node) @('--test', 'tests/lib.test.js')
        Invoke-Step $Backend $Py @('scripts/ci_smoke.py', '--port', '8799')
    }
    'dist' { Invoke-Step $Backend $Py @('scripts/build_dist.py') }
}

Write-Host "done: $Command" -ForegroundColor Green
