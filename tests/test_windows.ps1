# Run: powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_windows.ps1
# This harness never imports ScheduledTasks or runs the original scheduling script.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$script:MockGuard = $true
$script:TaskWrites = 0
$script:ExistingTask = @()
$script:Registration = $null
$script:ContextOutput = '{"timezone":null,"start_boundary":null}'
$script:ContextExit = 0
$script:ContextCalls = @()
$script:TriggerHostIsUtc = $false

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

foreach ($name in @('Setup.ps1', 'Schedule.ps1', 'Run-Daily.ps1')) {
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $projectRoot $name), [ref]$tokens, [ref]$errors)
    Assert-True ($errors.Count -eq 0) ('Syntax errors in ' + $name)
}
Write-Output 'PASS: three PowerShell scripts parse'

$scheduleSource = [IO.File]::ReadAllText((Join-Path $projectRoot 'Schedule.ps1'))
$runnerSource = [IO.File]::ReadAllText((Join-Path $projectRoot 'Run-Daily.ps1'))
$importLine = 'Import-Module ScheduledTasks -ErrorAction Stop'
Assert-True ($scheduleSource.Contains($importLine)) 'Scheduling import changed; review test isolation before continuing.'
$scheduleSource = $scheduleSource.Replace($importLine, '# ScheduledTasks import blocked by isolated test harness')
Assert-True ($scheduleSource -notmatch '(?im)^\s*Import-Module\b|(?i:ScheduledTasks\\)') 'Unexpected module import or qualified task command.'
$contextCall = '$contextOutput = @(& $pythonPath $runtimePath --config $configPath --at $At)'
Assert-True ($scheduleSource.Contains($contextCall)) 'Scheduler must read validated timezone configuration through the isolated Python boundary.'
$scheduleSource = $scheduleSource.Replace($contextCall, '$contextOutput = @(Invoke-MockedSchedulePython $pythonPath @($runtimePath, ''--config'', $configPath, ''--at'', $At)); $LASTEXITCODE = $script:ContextExit')
Assert-True ($scheduleSource -notmatch '&\s*\$pythonPath') 'Actual schedule Python invocation escaped isolation.'
$quotedRoot = "'" + $projectRoot.Replace("'", "''") + "'"
$scheduleSource = $scheduleSource.Replace('$PSScriptRoot', $quotedRoot).Replace('exit 1', 'throw')
$isolatedSchedule = [ScriptBlock]::Create($scheduleSource)

function Assert-MockGuard {
    if (-not $script:MockGuard) { throw 'Mock guard is disabled.' }
}
function Import-Module { throw 'Module imports are forbidden in this test harness.' }
function Test-Path { param($LiteralPath, $PathType) Assert-MockGuard; return $true }
function Get-ScheduledTask { param($TaskPath) Assert-MockGuard; return $script:ExistingTask }
function Get-ScheduledTaskInfo { throw 'Unexpected task-info access in isolated tests.' }
function Invoke-MockedSchedulePython {
    param($FilePath, $Arguments)
    Assert-MockGuard
    $script:ContextCalls += [pscustomobject]@{ File = $FilePath; Args = $Arguments }
    return $script:ContextOutput
}
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    Assert-MockGuard
    return [pscustomobject]@{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
}
function New-ScheduledTaskTrigger {
    param([switch]$Daily, [datetime]$At)
    Assert-MockGuard
    Assert-True $Daily.IsPresent 'Expected a daily trigger.'
    if ($script:TriggerHostIsUtc) { $boundary = [datetime]::SpecifyKind($At, [DateTimeKind]::Utc).ToString('o') }
    else { $boundary = $At.ToUniversalTime().ToString('o') }
    return [pscustomobject]@{ DaysInterval = 1; StartBoundary = $boundary }
}
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    Assert-MockGuard
    Assert-True ($UserId -eq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) 'Unexpected task user.'
    # Windows can normalize a supplied SID to a local username.
    return [pscustomobject]@{ UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name.Split('\')[-1]; LogonType = $LogonType; RunLevel = $RunLevel }
}
function New-ScheduledTaskSettingsSet {
    param([switch]$StartWhenAvailable, $MultipleInstances, [timespan]$ExecutionTimeLimit)
    Assert-MockGuard
    return [pscustomobject]@{ StartWhenAvailable = $StartWhenAvailable.IsPresent; MultipleInstances = $MultipleInstances; ExecutionTimeLimit = $ExecutionTimeLimit }
}
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $Action, $Trigger, $Principal, $Settings, $Description, [switch]$Force)
    Assert-MockGuard
    $script:TaskWrites += 1
    # Plain objects cannot be passed as CIM actions to the real registration cmdlet.
    $script:Registration = [pscustomobject]@{
        TaskName = $TaskName; TaskPath = $TaskPath; Actions = @($Action); Triggers = @($Trigger)
        Principal = $Principal; Settings = $Settings; Description = $Description; WasForced = $Force.IsPresent
    }
    return $script:Registration
}
function Unregister-ScheduledTask {
    param($TaskName, $TaskPath, [switch]$Confirm)
    Assert-MockGuard
    $script:TaskWrites += 1
}
function Assert-MockBindings {
    foreach ($name in @('Get-ScheduledTask', 'New-ScheduledTaskAction', 'New-ScheduledTaskTrigger', 'New-ScheduledTaskPrincipal', 'New-ScheduledTaskSettingsSet', 'Register-ScheduledTask', 'Unregister-ScheduledTask')) {
        $command = Get-Command $name -ErrorAction Stop
        Assert-True ($command.CommandType -eq 'Function' -and -not $command.ModuleName -and $command.Definition.Contains('Assert-MockGuard')) ('Real task command escaped isolation: ' + $name)
    }
}

Assert-MockBindings
& $isolatedSchedule -Action Install -At '09:30' -WhatIf
Assert-True ($script:TaskWrites -eq 0) 'Install WhatIf attempted a write.'
Write-Output 'PASS: install WhatIf'

Assert-MockBindings
& $isolatedSchedule -Action Install -At '09:30' | Out-Null
Assert-True ($script:TaskWrites -eq 1) 'Expected one mocked registration.'
$task = $script:Registration
Assert-True ($task.Principal.LogonType -eq 'Interactive' -and $task.Principal.RunLevel -eq 'Limited') 'Unexpected task principal.'
Assert-True ($task.Settings.MultipleInstances -eq 'IgnoreNew' -and $task.Settings.StartWhenAvailable -and $task.Settings.ExecutionTimeLimit.TotalMinutes -eq 120) 'Unexpected task runtime settings; Edge runs require a 120-minute task budget.'
Assert-True ($task.Actions[0].WorkingDirectory -eq $projectRoot -and -not $task.WasForced) 'Unexpected working directory or overwrite.'
$expectedArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $projectRoot 'Run-Daily.ps1')
Assert-True ($task.Actions[0].Arguments -ceq $expectedArguments) 'Runner path is not quoted as expected.'
$triggerTime = ([datetime]$task.Triggers[0].StartBoundary).ToLocalTime()
Assert-True ($task.Triggers[0].DaysInterval -eq 1 -and $triggerTime.Hour -eq 9 -and $triggerTime.Minute -eq 30) 'Unexpected daily trigger time.'
Write-Output 'PASS: mocked installation settings'

$script:ExistingTask = @($task)
Assert-MockBindings
& $isolatedSchedule -Action Install -At '10:40' | Out-Null
Assert-True ($script:Registration.WasForced -and $script:TaskWrites -eq 2) 'Owned update did not use Force.'
Write-Output 'PASS: owned update with normalized Windows username'

Assert-MockBindings
& $isolatedSchedule -Action Remove -WhatIf
Assert-True ($script:TaskWrites -eq 2) 'Remove WhatIf attempted a write.'
& $isolatedSchedule -Action Remove | Out-Null
Assert-True ($script:TaskWrites -eq 3) 'Expected one mocked removal.'
Write-Output 'PASS: removal and removal WhatIf'

$script:ExistingTask = @([pscustomobject]@{ TaskName = $task.TaskName; Actions = @([pscustomobject]@{ Execute = 'other.exe'; Arguments = ''; WorkingDirectory = 'C:\' }); Principal = $task.Principal })
Assert-MockBindings
$refused = $false
try { & $isolatedSchedule -Action Install 2>$null | Out-Null }
catch { $refused = $_.Exception.Message -match 'different actions' }
Assert-True ($refused -and $script:TaskWrites -eq 3) 'Unrelated task action was not refused.'
Write-Output 'PASS: unrelated action refusal'

$script:ExistingTask = @([pscustomobject]@{ TaskName = $task.TaskName; Actions = $task.Actions; Principal = [pscustomobject]@{ UserId = 'S-1-5-18' } })
Assert-MockBindings
$refused = $false
try { & $isolatedSchedule -Action Remove 2>$null | Out-Null }
catch { $refused = $_.Exception.Message -match 'different Windows user' }
Assert-True ($refused -and $script:TaskWrites -eq 3) 'Another user task was not refused.'
Write-Output 'PASS: unrelated Windows user refusal'

$refused = $false
try { & $isolatedSchedule -Action Install -At '24:00' 2>$null | Out-Null }
catch { $refused = $true }
Assert-True ($refused -and $script:TaskWrites -eq 3) 'Invalid schedule time was not refused.'
Write-Output 'PASS: invalid time refusal'

$script:ExistingTask = @()
$script:TriggerHostIsUtc = $true
$script:ContextOutput = '{"timezone":"Asia/Shanghai","start_boundary":"2026-10-25T20:00:00+08:00"}'
Assert-MockBindings
& $isolatedSchedule -Action Install | Out-Null
Assert-True ($script:TaskWrites -eq 4 -and $script:Registration.Triggers[0].StartBoundary -ceq '2026-10-25T20:00:00+08:00') 'Shanghai 20:00 followed the mocked UTC host instead of config timezone.'
$instant = [DateTimeOffset]::Parse($script:Registration.Triggers[0].StartBoundary)
Assert-True ($instant.UtcDateTime.Hour -eq 12 -and $instant.Offset.TotalHours -eq 8) 'Configured 20:00 did not represent noon UTC.'
$contextCall = $script:ContextCalls[-1]
Assert-True ($contextCall.File -eq (Join-Path $projectRoot '.venv\Scripts\python.exe') -and $contextCall.Args.Count -eq 5 -and
    $contextCall.Args[0] -eq (Join-Path $projectRoot 'runtime_context.py') -and $contextCall.Args[1] -eq '--config' -and
    $contextCall.Args[2] -eq (Join-Path $projectRoot 'config.json') -and $contextCall.Args[3] -eq '--at' -and $contextCall.Args[4] -eq '20:00') 'Timezone helper arguments were not separate structured values.'
Write-Output 'PASS: configured Shanghai 20:00 with a UTC host and structured Python arguments'

$script:ContextOutput = '{"timezone":null,"start_boundary":null}'
& $isolatedSchedule -Action Install -At '09:30' | Out-Null
Assert-True ($script:TaskWrites -eq 5 -and $script:Registration.Triggers[0].StartBoundary -match 'T09:30:00(?:\.0+)?Z$') 'Legacy missing-timezone configuration stopped using the host-local trigger.'
Write-Output 'PASS: legacy local-time compatibility'

foreach ($invalidContext in @(
    '{broken', '{}', '[]',
    '{"timezone":"Asia/Shanghai","start_boundary":null}',
    '{"timezone":null,"start_boundary":"2026-10-25T20:00:00+08:00"}',
    '{"timezone":"Asia/Shanghai","start_boundary":"2026-10-25T20:00:00"}',
    '{"timezone":"Asia/Shanghai","start_boundary":"2026-10-25T09:30:00+08:00"}'
)) {
    $script:ContextOutput = $invalidContext
    $refused = $false
    try { & $isolatedSchedule -Action Install 2>$null | Out-Null }
    catch { $refused = $true }
    Assert-True ($refused -and $script:TaskWrites -eq 5) 'Malformed timezone context was accepted or changed a task.'
}
$script:ContextOutput = ''
$script:ContextExit = 1
$refused = $false
try { & $isolatedSchedule -Action Install 2>$null | Out-Null }
catch { $refused = $_.Exception.Message -match 'timezone configuration' }
Assert-True ($refused -and $script:TaskWrites -eq 5) 'Timezone/config read failure did not stop task registration.'
$beforeContextCalls = $script:ContextCalls.Count
& $isolatedSchedule -Action Show | Out-Null
& $isolatedSchedule -Action Remove | Out-Null
Assert-True ($script:ContextCalls.Count -eq $beforeContextCalls -and $script:TaskWrites -eq 5) 'Showing/removing an absent task required a readable runtime config.'
$script:ContextExit = 0
Write-Output 'PASS: malformed timezone context and failed config reads refuse all scheduler writes'

$runnerCall = '& $pythonPath $scriptPath run'
Assert-True ($runnerSource.Contains($runnerCall)) 'Runner call changed; review process isolation.'
$runnerSource = $runnerSource.Replace('$PSScriptRoot', $quotedRoot).Replace($runnerCall, '$script:RunnerArgs = @($pythonPath, $scriptPath, ''run''); $LASTEXITCODE = 3').Replace('exit $runExitCode', 'return $runExitCode')
Assert-True ($runnerSource -notmatch '&\s*\$pythonPath') 'Actual Python invocation escaped isolation.'
$startLocation = $PWD.Path
$runnerResult = & ([ScriptBlock]::Create($runnerSource))
Assert-True ($runnerResult -eq 3 -and $PWD.Path -eq $startLocation -and $script:RunnerArgs[2] -eq 'run') 'Runner exit code or location restore failed.'
Write-Output 'PASS: runner exit propagation and location restore'
Write-Output 'All Windows packaging checks passed. Every scheduler write and Python invocation was mocked.'
