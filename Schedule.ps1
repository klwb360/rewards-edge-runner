[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateSet('Install', 'Remove', 'Show')]
    [string]$Action = 'Show',

    [ValidatePattern('^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$At = '20:00'
)

$ErrorActionPreference = 'Stop'

try {
    $projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
    $runnerPath = Join-Path $projectRoot 'Run-Daily.ps1'
    $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $runtimePath = Join-Path $projectRoot 'runtime_context.py'
    $configPath = Join-Path $projectRoot 'config.json'
    $powerShellPath = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $taskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $runnerPath
    $hashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $rootBytes = [System.Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())
        $rootHash = [BitConverter]::ToString($hashAlgorithm.ComputeHash($rootBytes)).Replace('-', '').Substring(0, 12)
    }
    finally {
        $hashAlgorithm.Dispose()
    }
    $taskName = 'MicrosoftRewards-' + $rootHash
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $userSid = $currentIdentity.User.Value

    Import-Module ScheduledTasks -ErrorAction Stop
    # List first so an absent task is distinct from access/service errors.
    $existingTask = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop |
        Where-Object { $_.TaskName -eq $taskName })
    if ($existingTask.Count -gt 1) { throw 'More than one matching scheduled task was returned.' }
    if ($existingTask.Count -eq 1) {
        $existingActions = @($existingTask[0].Actions)
        if ($existingActions.Count -ne 1 -or
            $existingActions[0].Execute -ine $powerShellPath -or
            $existingActions[0].Arguments -cne $taskArguments -or
            $existingActions[0].WorkingDirectory -ine $projectRoot) {
            throw ('Task {0} exists with different actions. It will not be changed.' -f $taskName)
        }
        $existingUserId = $existingTask[0].Principal.UserId
        if ($existingUserId -match '^S-1-') {
            $existingUserSid = $existingUserId
        }
        else {
            $existingAccount = New-Object System.Security.Principal.NTAccount($existingUserId)
            $existingUserSid = $existingAccount.Translate([System.Security.Principal.SecurityIdentifier]).Value
        }
        if ($existingUserSid -ine $userSid) {
            throw ('Task {0} belongs to a different Windows user. It will not be changed.' -f $taskName)
        }
    }

    switch ($Action) {
        'Show' {
            if ($existingTask.Count -eq 0) {
                Write-Output ('Task is not installed: {0}' -f $taskName)
            }
            else {
                $existingTask[0] | Select-Object TaskName, State, Description, Triggers
                Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\' |
                    Select-Object LastRunTime, LastTaskResult, NextRunTime
            }
        }
        'Install' {
            if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
                throw 'Python environment is missing. Run Setup.ps1 first.'
            }
            if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
                throw 'Run-Daily.ps1 is missing.'
            }
            if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'rewards.py') -PathType Leaf)) {
                throw 'rewards.py is missing.'
            }
            if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
                throw 'runtime_context.py is missing.'
            }
            # The helper validates config and computes the configured timezone offset.
            # Arguments remain separate values; no command shell interprets paths or config.
            $contextOutput = @(& $pythonPath $runtimePath --config $configPath --at $At)
            if ($LASTEXITCODE -ne 0) {
                throw ('Unable to read validated timezone configuration (exit code {0}). No task was changed.' -f $LASTEXITCODE)
            }
            $jsonParameters = @{ ErrorAction = 'Stop' }
            if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { $jsonParameters['DateKind'] = 'String' }
            try { $scheduleContext = ($contextOutput -join [Environment]::NewLine) | ConvertFrom-Json @jsonParameters }
            catch { throw 'Invalid timezone configuration returned by runtime_context.py.' }
            if ($scheduleContext -isnot [pscustomobject] -or
                $scheduleContext.PSObject.Properties.Name -notcontains 'timezone' -or
                $scheduleContext.PSObject.Properties.Name -notcontains 'start_boundary') {
                throw 'Invalid timezone configuration returned by runtime_context.py.'
            }
            $timeLabel = $At + ' local time'
            if ($null -eq $scheduleContext.timezone) {
                if ($null -ne $scheduleContext.start_boundary) { throw 'Invalid legacy timezone configuration.' }
            }
            else {
                $parsedBoundary = [DateTimeOffset]::MinValue
                if ($scheduleContext.timezone -isnot [string] -or [string]::IsNullOrWhiteSpace($scheduleContext.timezone) -or
                    $scheduleContext.start_boundary -isnot [string] -or
                    $scheduleContext.start_boundary -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:\.0+)?(?:Z|[+-]\d{2}:\d{2})$' -or
                    -not [DateTimeOffset]::TryParse($scheduleContext.start_boundary, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$parsedBoundary) -or
                    $parsedBoundary.ToString('HH:mm', [Globalization.CultureInfo]::InvariantCulture) -ne $At) {
                    throw 'Invalid configured timezone start boundary.'
                }
                $timeLabel = '{0} {1}' -f $At, $scheduleContext.timezone
            }
            if ($PSCmdlet.ShouldProcess($taskName, ('Install daily task at {0}' -f $timeLabel))) {
                $taskAction = New-ScheduledTaskAction -Execute $powerShellPath -Argument $taskArguments -WorkingDirectory $projectRoot
                $taskTrigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
                if ($null -ne $scheduleContext.timezone) {
                    # An explicit offset overrides the Windows host timezone.
                    # https://learn.microsoft.com/windows/win32/api/taskschd/nf-taskschd-itrigger-put_startboundary
                    $taskTrigger.StartBoundary = $scheduleContext.start_boundary
                }
                $taskPrincipal = New-ScheduledTaskPrincipal -UserId $userSid -LogonType Interactive -RunLevel Limited
                $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 120)
                $description = 'Runs this local Microsoft Rewards script daily while this Windows user is signed in. Workspace: ' + $projectRoot
                $registerParameters = @{
                    TaskName = $taskName
                    TaskPath = '\'
                    Action = $taskAction
                    Trigger = $taskTrigger
                    Principal = $taskPrincipal
                    Settings = $taskSettings
                    Description = $description
                }
                if ($existingTask.Count -eq 1) { $registerParameters['Force'] = $true }
                Register-ScheduledTask @registerParameters | Select-Object TaskName, State
                Write-Output ('Installed for {0}. Windows must be running and this user must be signed in.' -f $timeLabel)
            }
        }
        'Remove' {
            if ($existingTask.Count -eq 0) {
                Write-Output ('Task is not installed: {0}' -f $taskName)
            }
            elseif ($PSCmdlet.ShouldProcess($taskName, 'Remove this workspace scheduled task')) {
                Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
                Write-Output ('Removed: {0}' -f $taskName)
            }
        }
    }
}
catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
