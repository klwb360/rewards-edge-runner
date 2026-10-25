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
            if ($PSCmdlet.ShouldProcess($taskName, ('Install daily task at {0} local time' -f $At))) {
                $taskAction = New-ScheduledTaskAction -Execute $powerShellPath -Argument $taskArguments -WorkingDirectory $projectRoot
                $taskTrigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
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
                Write-Output ('Installed for {0} local time. Windows must be running and this user must be signed in.' -f $At)
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
