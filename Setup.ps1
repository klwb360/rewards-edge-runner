[CmdletBinding()]
param(
    [string]$PythonCommand = 'python'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$requirementsPath = Join-Path $projectRoot 'requirements.txt'

try {
    if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
        throw 'requirements.txt was not found next to Setup.ps1.'
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        $bootstrapPython = (Get-Command $PythonCommand -CommandType Application -ErrorAction Stop).Source
        & $bootstrapPython -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Creating the Python virtual environment failed.' }
    }
    & $pythonPath -m pip install --requirement $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw 'Installing Python dependencies failed.' }

    Write-Output 'Setup finished. Next, sign in manually:'
    Write-Output ('& "{0}" "{1}" login' -f $pythonPath, (Join-Path $projectRoot 'rewards.py'))
    Write-Output 'After checking one manual run, install the daily task with Schedule.ps1 -Action Install.'
}
catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
