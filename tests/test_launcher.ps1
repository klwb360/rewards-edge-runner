[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$launcherPath = Join-Path $workspaceRoot 'Rewards-Launcher.ps1'
$tempBase = [IO.Path]::GetTempPath()
$testRoot = Join-Path $tempBase ('Rewards 启动器 & ! ' + [guid]::NewGuid().ToString('N'))
$markerName = '.rewards-launcher-test'
$originalUtf8 = $env:PYTHONUTF8
$originalIo = $env:PYTHONIOENCODING
$initialLocation = $PWD.Path

function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Write-ReportFixture([string]$Name, $Data) {
    [IO.File]::WriteAllText((Join-Path $testRoot ('.logs\' + $Name)), ($Data | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
}

try {
    $null = New-Item -ItemType Directory -Path $testRoot
    [IO.File]::WriteAllText((Join-Path $testRoot $markerName), 'owned launcher test')
    foreach ($directory in @('.venv\Scripts', '.logs', 'docs')) { $null = New-Item -ItemType Directory -Path (Join-Path $testRoot $directory) }
    Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $testRoot 'Rewards-Launcher.ps1')
    Copy-Item -LiteralPath (Join-Path $workspaceRoot 'Rewards.bat') -Destination (Join-Path $testRoot 'Rewards.bat')
    foreach ($name in @('Setup.ps1', 'Schedule.ps1', 'rewards.py', '.venv\Scripts\python.exe', 'config.json', 'docs\OPERATION_GUIDE.md')) {
        [IO.File]::WriteAllText((Join-Path $testRoot $name), 'throw "Real test backend execution is forbidden"')
    }
    $bytes = [IO.File]::ReadAllBytes($launcherPath)
    Assert-True ($bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191) 'Chinese launcher must have a UTF-8 BOM.'
    . (Join-Path $testRoot 'Rewards-Launcher.ps1')
    $realProcessHelper = (Get-Command Invoke-RewardsProcess).ScriptBlock
    $script:Calls = @()
    $script:Opened = @()
    $script:Notepads = @()
    $script:PythonExit = 0
    $script:SetupExit = 0
    $script:ScheduleExit = 0
    $script:NewReport = $null
    $script:OpenFails = $false
    $script:MenuInputs = New-Object 'Collections.Generic.Queue[string]'
    $script:ReadCount = 0

    # Every side-effect boundary is replaced. Stub files also throw if accidentally executed.
    function Invoke-RewardsProcess([string]$FilePath, [string[]]$Arguments) {
        $script:Calls += [pscustomobject]@{ File = $FilePath; Args = $Arguments; Directory = $PWD.Path; Utf8 = $env:PYTHONUTF8; Io = $env:PYTHONIOENCODING }
        if ($FilePath -eq (Join-Path $testRoot '.venv\Scripts\python.exe')) {
            if ($Arguments[1] -eq 'run' -and $null -ne $script:NewReport) {
                Write-ReportFixture ((Get-Date).ToString('yyyyMMdd-HHmmss-ffffff') + '.json') $script:NewReport
            }
            Write-Host 'MOCK Python output'
            return $script:PythonExit
        }
        if ($Arguments -contains (Join-Path $testRoot 'Setup.ps1')) {
            if ($script:SetupExit -eq 0) { [IO.File]::WriteAllText((Join-Path $testRoot '.venv\Scripts\python.exe'), 'not executable') }
            return $script:SetupExit
        }
        if ($Arguments -contains (Join-Path $testRoot 'Schedule.ps1')) { return $script:ScheduleExit }
        throw 'Unexpected process reached the test boundary.'
    }
    function Invoke-Item { param($LiteralPath) if ($script:OpenFails) { throw 'No file association' }; $script:Opened += $LiteralPath }
    function Start-Process { param($FilePath, $ArgumentList) $script:Notepads += [pscustomobject]@{ File = $FilePath; Args = $ArgumentList } }
    function Read-Host { param($Prompt) $script:ReadCount += 1; if ($script:MenuInputs.Count) { return $script:MenuInputs.Dequeue() }; return $null }
    function Invoke-Case([string]$TestAction, [string]$TestAt = '20:00') {
        $script:CaseCode = $null
        $records = @(& { $script:CaseCode = Invoke-RewardsLauncher -Action $TestAction -At $TestAt } 6>&1)
        $script:CaseText = ($records | Out-String -Width 4096)
        Assert-True ($PWD.Path -eq $initialLocation) 'Launcher failed to restore the current directory.'
        return $script:CaseCode
    }

    $env:PYTHONUTF8 = 'before-launcher'
    $env:PYTHONIOENCODING = 'before-launcher'
    Assert-True ((Invoke-Case 'help') -eq 0 -and $script:Calls.Count -eq 0) 'Help started a backend.'
    foreach ($exitCode in @(0, 1, 2, 3, 130)) {
        $script:PythonExit = $exitCode
        Assert-True ((Invoke-Case 'inspect') -eq $exitCode) 'Python exit code was not propagated.'
        $call = $script:Calls[-1]
        Assert-True ($call.Args[1] -eq 'inspect' -and $call.Directory -eq $testRoot -and $call.Utf8 -eq '1' -and $call.Io -eq 'utf-8') 'Python invocation had incorrect action/cwd/environment.'
    }
    Assert-True ($env:PYTHONUTF8 -eq 'before-launcher' -and $env:PYTHONIOENCODING -eq 'before-launcher') 'Process environment leaked.'
    Assert-True ($script:ReadCount -eq 0) 'A CLI action prompted for input.'
    Write-Output 'PASS: help, numeric exit codes, cwd and environment restore, no CLI pause'

    foreach ($testAction in @('login', 'setup', 'schedule-install', 'schedule-show', 'schedule-remove')) {
        $script:PythonExit = 0
        $beforeCount = $script:Calls.Count
        Assert-True ((Invoke-Case $testAction '09:15') -eq 0 -and $script:Calls.Count -eq ($beforeCount + 1)) ('Routing failed: ' + $testAction)
    }
    Assert-True ($script:Calls[-3].Args[-1] -eq '09:15' -and $script:Calls[-3].Args -contains 'Install') 'Schedule arguments were lost.'
    Assert-True ((Invoke-Case 'schedule-install' '24:00') -eq 1) 'Invalid schedule time was accepted.'
    $script:ScheduleExit = 3
    Assert-True ((Invoke-Case 'schedule-show') -eq 3) 'Schedule exit code was not propagated.'
    $script:ScheduleExit = 0
    [IO.File]::Delete((Join-Path $testRoot '.venv\Scripts\python.exe'))
    $beforeCount = $script:Calls.Count
    Assert-True ((Invoke-Case 'inspect') -eq 0 -and $script:Calls.Count -eq ($beforeCount + 2)) 'Missing environment did not bootstrap exactly once.'
    Write-Output 'PASS: runtime/setup/schedule routing and missing-environment bootstrap'

    $baseReport = @{ command = 'run'; started_at = '2020-01-01T12:00:00+08:00'; finished_at = '2020-01-01T12:01:00+08:00'; status = 'completed'; exit_code = 0; tasks = @() }
    Write-ReportFixture '20200101-120000-111111.json' $baseReport
    $partial = $baseReport.Clone()
    $partial.status = 'partial'; $partial.exit_code = 1
    $partial.tasks = @(@{ status = 'completed'; title = 'confirmed' }, @{ status = 'manual'; title = 'manual fixture' })
    $partial.searches = @{ requested = 20; opened = 1; skipped_today = 0; credit = 'observed'; quota_before = @{ current = 57; maximum = 60 }; quota_after = @{ current = 60; maximum = 60 }; observed_points_gained = 3; stopped_reason = 'limit_reached' }
    Write-ReportFixture '20200101-120000-222222.json' $partial
    [IO.File]::WriteAllText((Join-Path $testRoot '.logs\20990101-000000-000001.json'), '{broken')
    Write-ReportFixture '20990101-000000-000002.json' @{ command = 'inspect'; status = 'completed'; started_at = '2099-01-01'; tasks = @() }
    Write-ReportFixture '29991399-000000-000000.json' $baseReport
    Write-ReportFixture 'diagnostic.json' $baseReport
    Assert-True ((Get-RewardsRunReport).Path.EndsWith('20200101-120000-222222.json')) 'Latest valid actual run report was not selected.'
    Assert-True ((Invoke-Case 'report') -eq 0) 'Report command failed.'
    foreach ($expected in @('20200101-120000-222222.json', '2020-01-01', 'partial', '60/60', '3 分', '历史运行报告', '手机 App', '未核验', 'limit_reached')) {
        Assert-True ($script:CaseText.Contains($expected)) ('Report omitted: ' + $expected + '. Captured: ' + $script:CaseText)
    }
    Assert-True (-not $script:CaseText.Contains('Edge 浏览阶段')) 'Legacy report invented an Edge result.'
    $script:PythonExit = 3
    Assert-True ((Invoke-Case 'run') -eq 3 -and $script:CaseText.Contains('本次没有生成') -and -not $script:CaseText.Contains('运行状态：')) 'Failed run reused historical success as its current result.'
    $script:NewReport = $partial.Clone(); $script:NewReport.started_at = [DateTimeOffset]::Now.ToString('o'); $script:NewReport.status = 'search_paused'; $script:NewReport.searches = @{ opened = 2; requested = 20; skipped_today = 0; credit = 'unverified'; quota_before = @{ current = 57; maximum = 60 }; quota_after = @{ current = 57; maximum = 60 }; observed_points_gained = 0; stopped_reason = 'no_credit' }
    $script:PythonExit = 1
    Assert-True ((Invoke-Case 'run') -eq 1 -and $script:CaseText.Contains('search_paused') -and $script:CaseText.Contains('0 分') -and $script:CaseText.Contains('no_credit')) 'Current report or observed zero points were misreported.'
    Write-Output 'PASS: microsecond reports, corrupt/diagnostic filtering, historical warning and current-run summaries'

    $edgeCases = @(
        @{ Status = 'already_completed'; Before = 30; After = 30; CompletedBefore = $true; CompletedAfter = $true; Waited = 0; Code = 0; Top = 'completed'; Label = '执行前页面已确认完成' },
        @{ Status = 'completed'; Before = 5; After = 30; CompletedBefore = $false; CompletedAfter = $true; Waited = 1500; Code = 0; Top = 'completed'; Label = '页面已确认完成' },
        @{ Status = 'timeout'; Before = 5; After = 5; CompletedBefore = $false; CompletedAfter = $false; Waited = 3600; Code = 1; Top = 'partial'; Label = '等待达到上限，页面仍未确认完成' },
        @{ Status = 'unavailable'; Before = $null; After = $null; CompletedBefore = $null; CompletedAfter = $null; Waited = 0; Code = 1; Top = 'partial'; Label = '当前页面未提供此任务' },
        @{ Status = 'interrupted'; Before = 5; After = 10; CompletedBefore = $false; CompletedAfter = $false; Waited = 300; Code = 130; Top = 'interrupted'; Label = '用户已中断，未确认完成' },
        @{ Status = 'error'; Before = 5; After = 5; CompletedBefore = $false; CompletedAfter = $false; Waited = 10; Code = 1; Top = 'partial'; Label = '阶段运行出错，未确认完成' }
    )
    foreach ($edgeCase in $edgeCases) {
        $script:NewReport = $partial.Clone()
        $script:NewReport.started_at = [DateTimeOffset]::Now.ToString('o')
        $script:NewReport.status = $edgeCase.Top; $script:NewReport.exit_code = $edgeCase.Code
        $script:NewReport.edge_browsing = @{ status = $edgeCase.Status; minutes_before = $edgeCase.Before; minutes_after = $edgeCase.After; target_minutes = 30; completed_before = $edgeCase.CompletedBefore; completed_after = $edgeCase.CompletedAfter; waited_seconds = $edgeCase.Waited; stopped_reason = ('fixture_' + $edgeCase.Status) }
        $script:PythonExit = $edgeCase.Code
        Assert-True ((Invoke-Case 'run') -eq $edgeCase.Code) ('Edge run exit code changed: ' + $edgeCase.Status)
        foreach ($expected in @('Edge 浏览阶段', $edgeCase.Label, ('fixture_' + $edgeCase.Status), ('本轮等待 ' + $edgeCase.Waited + ' 秒'), '手机 App 签到/阅读任务：未核验')) {
            Assert-True ($script:CaseText.Contains($expected)) ('Edge report omitted ' + $expected)
        }
        if ($null -ne $edgeCase.Before) {
            $progressText = '{0}/30 → {1}/30' -f $edgeCase.Before, $edgeCase.After
            Assert-True ($script:CaseText.Contains($progressText)) 'Edge page progress was omitted or inferred from elapsed time.'
        }
        if ($edgeCase.Status -eq 'timeout') { Assert-True ($script:CaseText.Contains('未完成 → 未完成')) 'Timed-out task was reported as completed.' }
        if ($edgeCase.Code -eq 130) { Assert-True ($script:CaseText.Contains('用户已中断本次运行')) 'Interrupted CLI lacked an actionable message.' }
    }
    Write-Output 'PASS: Edge stage results, actual page progress, timeout and interrupted exit 130'

    foreach ($testAction in @('config', 'guide', 'logs')) { Assert-True ((Invoke-Case $testAction) -eq 0) ('Open action failed: ' + $testAction) }
    Assert-True ($script:Opened.Count -eq 3) 'Open actions were not routed.'
    $script:OpenFails = $true
    Assert-True ((Invoke-Case 'config') -eq 0 -and $script:Notepads.Count -eq 1) 'Missing file association did not use Notepad.'
    $beforeCount = $script:Calls.Count
    $script:MenuInputs.Enqueue('0')
    Assert-True ((Invoke-Case 'menu') -eq 0 -and $script:Calls.Count -eq $beforeCount) 'Quitting the menu started a backend.'
    Assert-True ((Invoke-Case 'menu') -eq 0) 'Menu EOF did not exit.'
    Write-Output 'PASS: explicit open actions, Notepad fallback, menu quit and EOF'

    # Exercise the real process helper with a harmless backend, never Python/Setup/Schedule.
    $probePath = Join-Path $testRoot 'argv probe.ps1'
    $probeSource = 'param([string]$Value); [IO.File]::WriteAllText((Join-Path $PSScriptRoot "process-args.json"), (@{ value=$Value; cwd=$PWD.Path } | ConvertTo-Json)); exit 7'
    [IO.File]::WriteAllText($probePath, $probeSource)
    $powerShellPath = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $probeExit = & $realProcessHelper $powerShellPath @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $probePath, '-Value', 'argument with spaces & !')
    $probe = [IO.File]::ReadAllText((Join-Path $testRoot 'process-args.json')) | ConvertFrom-Json
    Assert-True ($probeExit -eq 7 -and $probe.value -eq 'argument with spaces & !' -and $probe.cwd -eq $testRoot) 'Real process arguments/cwd/exit code were damaged.'

    # Only the BAT copy is exercised here. Its PS backend is replaced with a harmless recorder.
    $fakeBackend = 'param([string]$Action="menu",[string]$At="20:00"); [IO.File]::WriteAllText((Join-Path $PSScriptRoot "bat-args.json"), (@{ action=$Action; at=$At; root=$PSScriptRoot } | ConvertTo-Json), (New-Object Text.UTF8Encoding($false))); if ($Action -eq "run" -and $At -eq "13:00") { exit 130 }; if ($Action -eq "run") { exit 3 }; exit 0'
    [IO.File]::WriteAllText((Join-Path $testRoot 'Rewards-Launcher.ps1'), $fakeBackend, (New-Object Text.UTF8Encoding($true)))
    foreach ($case in @(@{ Tail = ' run -At 09:15'; Action = 'run'; At = '09:15'; Code = 3 }, @{ Tail = ' run -At 13:00'; Action = 'run'; At = '13:00'; Code = 130 }, @{ Tail = ' help'; Action = 'help'; At = '20:00'; Code = 0 }, @{ Tail = ''; Action = 'menu'; At = '20:00'; Code = 0 })) {
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $env:ComSpec
        $info.Arguments = '/d /v:off /s /c ""' + (Join-Path $testRoot 'Rewards.bat') + '"' + $case.Tail + '"'
        $info.WorkingDirectory = $env:SystemRoot
        $info.UseShellExecute = $false; $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
        $process = [Diagnostics.Process]::Start($info)
        try {
            if (-not $process.WaitForExit(15000)) { $process.Kill(); throw 'BAT test timed out.' }
            $stdout = $process.StandardOutput.ReadToEnd(); $stderr = $process.StandardError.ReadToEnd()
            Assert-True ($process.ExitCode -eq $case.Code) ('BAT exit mismatch: ' + $stdout + $stderr)
        }
        finally { $process.Dispose() }
        $record = [IO.File]::ReadAllText((Join-Path $testRoot 'bat-args.json')) | ConvertFrom-Json
        Assert-True ($record.action -eq $case.Action -and $record.at -eq $case.At -and $record.root -eq $testRoot) 'BAT argument or special-character path forwarding failed.'
    }
    Write-Output 'PASS: real process helper and real CMD wrapper in Chinese/space/&/! path'
    Write-Output 'All launcher checks passed; no account, network, browser profile or scheduled task was used.'
}
finally {
    $env:PYTHONUTF8 = $originalUtf8
    $env:PYTHONIOENCODING = $originalIo
    if ($PWD.Path -ne $initialLocation) { Set-Location -LiteralPath $initialLocation }
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $allowedPrefix = [IO.Path]::GetFullPath($tempBase).TrimEnd('\') + '\'
    $marker = Join-Path $resolvedTestRoot $markerName
    if ($resolvedTestRoot.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Directory]::Exists($resolvedTestRoot) -and [IO.File]::Exists($marker) -and
        [IO.File]::ReadAllText($marker) -eq 'owned launcher test') {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
