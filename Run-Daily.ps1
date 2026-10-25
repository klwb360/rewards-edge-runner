[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'rewards.py'
$runExitCode = 1

try {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw 'Python environment is missing. Run Setup.ps1 first.'
    }
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw 'rewards.py is missing.'
    }
    Push-Location -LiteralPath $PSScriptRoot
    try {
        & $pythonPath $scriptPath run
        $runExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    $runExitCode = 1
}
exit $runExitCode
