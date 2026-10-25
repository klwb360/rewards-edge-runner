[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Action = 'menu',
    [string]$At = '20:00'
)

$script:RewardsRoot = $PSScriptRoot

function ConvertTo-RewardsArgument([string]$Value) {
    # Quote a Windows argv element without passing it through a command shell.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-RewardsProcess([string]$FilePath, [string[]]$Arguments) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = ($Arguments | ForEach-Object { ConvertTo-RewardsArgument $_ }) -join ' '
    $startInfo.WorkingDirectory = $script:RewardsRoot
    $startInfo.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $process.WaitForExit()
        return $process.ExitCode
    }
    finally { $process.Dispose() }
}

function Invoke-RewardsScript([string]$Name, [string[]]$Arguments = @()) {
    $path = Join-Path $script:RewardsRoot $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "缺少文件：$path" }
    $powerShellPath = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    return (Invoke-RewardsProcess $powerShellPath (@('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $path) + $Arguments))
}

function Get-RewardsRunReport {
    $logsPath = Join-Path $script:RewardsRoot '.logs'
    if (-not (Test-Path -LiteralPath $logsPath -PathType Container)) { return $null }
    foreach ($file in (Get-ChildItem -LiteralPath $logsPath -Filter '*.json' -File | Sort-Object Name -Descending)) {
        if ($file.Name -notmatch '^\d{8}-\d{6}(?:-\d{6})?\.json$') { continue }
        $fileTime = [datetime]::MinValue
        if (-not [datetime]::TryParseExact($file.BaseName, [string[]]@('yyyyMMdd-HHmmss-ffffff', 'yyyyMMdd-HHmmss'), [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$fileTime)) { continue }
        try {
            $data = [IO.File]::ReadAllText($file.FullName) | ConvertFrom-Json -ErrorAction Stop
            $startedAt = [DateTimeOffset]::MinValue
            if ($data.command -ne 'run' -or -not ($data.tasks -is [array]) -or
                [string]::IsNullOrWhiteSpace($data.status) -or
                -not [DateTimeOffset]::TryParse($data.started_at, [ref]$startedAt)) { continue }
            return [pscustomobject]@{ Path = $file.FullName; Data = $data; StartedAt = $startedAt }
        }
        catch { continue }
    }
    return $null
}

function Format-RewardsValue($Value) {
    if ($null -eq $Value) { return '未记录' }
    return [string]$Value
}

function Format-RewardsCompletion($Value) {
    if ($Value -isnot [bool]) { return '未核验' }
    if ($Value) { return '已完成' }
    return '未完成'
}

function Write-RewardsRunReport($Report) {
    $data = $Report.Data
    Write-Host ('运行时间：{0}' -f $Report.StartedAt.ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss zzz'))
    Write-Host ('报告文件：{0}' -f $Report.Path)
    if ($Report.StartedAt.LocalDateTime.Date -ne (Get-Date).Date) {
        Write-Host '注意：这是历史运行报告，不能代表今天的任务状态。' -ForegroundColor Yellow
    }
    $labels = @{
        completed = '本次全部启用阶段正常结束'; partial = '部分任务未确认完成'
        needs_human = '需要手动登录或验证'; interface_changed = '页面或日期数据不兼容'
        search_paused = '搜索计分暂停，已停止'; error = '运行出错'; started = '尚无结束结果'
        interrupted = '用户已中断运行'
    }
    $statusLabel = $labels[[string]$data.status]
    if (-not $statusLabel) { $statusLabel = '报告中的状态' }
    Write-Host ('运行状态：{0}（{1}）；退出码：{2}' -f $statusLabel, $data.status, (Format-RewardsValue $data.exit_code))
    $completed = @($data.tasks | Where-Object { $_.status -eq 'completed' }).Count
    $already = @($data.tasks | Where-Object { $_.status -eq 'already_completed' }).Count
    $remaining = $data.tasks.Count - $completed - $already
    Write-Host ('网页任务：本轮确认 {0}，此前完成 {1}，其余待处理/未确认 {2}，合计 {3}。' -f $completed, $already, $remaining, $data.tasks.Count)
    foreach ($item in $data.tasks) {
        if ($item.status -notin @('completed', 'already_completed')) {
            Write-Host ('  {0}：{1}' -f $item.status, $item.title)
        }
    }
    $edge = $data.edge_browsing
    if ($null -ne $edge) {
        $edgeLabels = @{
            already_completed = '执行前页面已确认完成'; completed = '页面已确认完成'
            pending = '今日尚未确认完成'; checking = '正在读取页面进度'; waiting = '正在等待页面确认'
            timeout = '等待达到上限，页面仍未确认完成'; unavailable = '当前页面未提供此任务'
            interface_changed = '页面或日期数据不兼容，未确认完成'
            interrupted = '用户已中断，未确认完成'; error = '阶段运行出错，未确认完成'
        }
        $edgeLabel = $edgeLabels[[string]$edge.status]
        if (-not $edgeLabel) { $edgeLabel = '报告中的阶段状态' }
        Write-Host ('Edge 浏览阶段：{0}（{1}）。' -f $edgeLabel, (Format-RewardsValue $edge.status))
        Write-Host ('Edge 页面分钟进度：{0}/{2} → {1}/{2}。' -f (Format-RewardsValue $edge.minutes_before), (Format-RewardsValue $edge.minutes_after), (Format-RewardsValue $edge.target_minutes))
        Write-Host ('Edge 页面完成标记：{0} → {1}；本轮等待 {2} 秒。' -f (Format-RewardsCompletion $edge.completed_before), (Format-RewardsCompletion $edge.completed_after), (Format-RewardsValue $edge.waited_seconds))
        Write-Host ('Edge 停止原因：{0}' -f (Format-RewardsValue $edge.stopped_reason))
    }
    $search = $data.searches
    if ($null -eq $search) {
        Write-Host 'Bing 搜索：本份报告未记录搜索结果。'
    }
    else {
        Write-Host ('Bing 搜索：已提交 {0}，本轮配置上限 {1}，当天去重跳过 {2}。' -f (Format-RewardsValue $search.opened), (Format-RewardsValue $search.requested), (Format-RewardsValue $search.skipped_today))
        if ($null -ne $search.quota_after -and $null -ne $search.quota_after.current -and $null -ne $search.quota_after.maximum) {
            $before = '未记录'
            if ($null -ne $search.quota_before) { $before = '{0}/{1}' -f $search.quota_before.current, $search.quota_before.maximum }
            Write-Host ('页面显示的搜索积分：{0} → {1}/{2}；本轮观察到增加 {3} 分。' -f $before, $search.quota_after.current, $search.quota_after.maximum, (Format-RewardsValue $search.observed_points_gained))
        }
        else { Write-Host '搜索积分：未核验，提交次数不代表积分到账。' }
        Write-Host ('计分记录：{0}' -f (Format-RewardsValue $search.credit))
        if ($search.stopped_reason) {
            $reasons = @{ limit_reached = '已达到页面显示的搜索积分上限'; no_credit = '连续搜索未观察到计分' }
            $reasonLabel = $reasons[[string]$search.stopped_reason]
            Write-Host ('搜索停止原因：{0} {1}' -f $search.stopped_reason, $reasonLabel)
        }
    }
    if ($data.reason) { Write-Host ('运行说明：{0}' -f $data.reason) }
    Write-Host '手机 App 签到/阅读任务：未核验，请在手机端查看。'
}

function Open-RewardsPath([string]$Path, [switch]$TextFile) {
    Write-Host ('打开：{0}' -f $Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "文件或目录不存在：$Path" }
    try { Invoke-Item -LiteralPath $Path -ErrorAction Stop }
    catch {
        if (-not $TextFile) { throw "无法打开目录，请手动打开：$Path" }
        Write-Host '默认程序无法打开，尝试使用记事本。' -ForegroundColor Yellow
        $notepad = Join-Path ([Environment]::GetFolderPath('System')) 'notepad.exe'
        Start-Process -FilePath $notepad -ArgumentList (ConvertTo-RewardsArgument $Path) -ErrorAction Stop | Out-Null
    }
}

function Show-RewardsHelp {
    Write-Host 'Microsoft Rewards 本地启动器'
    Write-Host '双击 Rewards.bat 打开中文菜单；命令行用法：Rewards.bat <操作>'
    Write-Host '  setup    安装/更新本地依赖       login    手动登录并保存会话'
    Write-Host '  inspect  检查网页任务           run      执行一次并显示本次报告'
    Write-Host '  report   查看最近有效运行报告   logs     打开日志目录'
    Write-Host '  config   编辑配置               guide    打开操作说明'
    Write-Host '  schedule-install -At 20:00      安排每日自动运行（本机时间）'
    Write-Host '  schedule-show                  查看定时任务'
    Write-Host '  schedule-remove                移除本目录的定时任务'
    Write-Host '命令行操作不会暂停；只有中文菜单等待输入。定时运行需要 Windows 开机且当前用户已登录。'
}

function Invoke-RewardsAction([string]$Action, [string]$At) {
    switch ($Action) {
        'help' { Show-RewardsHelp; return 0 }
        'setup' { return (Invoke-RewardsScript 'Setup.ps1') }
        'report' {
            $report = Get-RewardsRunReport
            if ($null -eq $report) { Write-Host '尚无有效的运行报告，请先执行 Rewards.bat run。'; return 1 }
            Write-RewardsRunReport $report
            return 0
        }
        'config' { Open-RewardsPath (Join-Path $script:RewardsRoot 'config.json') -TextFile; return 0 }
        'guide' { Open-RewardsPath (Join-Path $script:RewardsRoot 'docs\OPERATION_GUIDE.md') -TextFile; return 0 }
        'logs' {
            $logsPath = Join-Path $script:RewardsRoot '.logs'
            if (-not (Test-Path -LiteralPath $logsPath)) { New-Item -ItemType Directory -Path $logsPath | Out-Null }
            Open-RewardsPath $logsPath
            return 0
        }
        { $_ -in @('schedule-install', 'schedule-show', 'schedule-remove') } {
            $scheduleAction = @{ 'schedule-install' = 'Install'; 'schedule-show' = 'Show'; 'schedule-remove' = 'Remove' }[$Action]
            if ($At -notmatch '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$') { throw '时间格式应为 HH:mm，例如 20:00。' }
            return (Invoke-RewardsScript 'Schedule.ps1' @('-Action', $scheduleAction, '-At', $At))
        }
        { $_ -in @('login', 'inspect', 'run') } {
            $pythonPath = Join-Path $script:RewardsRoot '.venv\Scripts\python.exe'
            $mainPath = Join-Path $script:RewardsRoot 'rewards.py'
            if (-not (Test-Path -LiteralPath $mainPath -PathType Leaf)) { throw "主程序不存在：$mainPath" }
            if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
                Write-Host '运行环境缺失，先执行初始化。'
                $setupCode = Invoke-RewardsScript 'Setup.ps1'
                if ($setupCode -ne 0) { return $setupCode }
                if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw '初始化后仍找不到 Python，请执行 Rewards.bat setup 检查。' }
            }
            $before = $null
            if ($Action -eq 'run') { $before = Get-RewardsRunReport }
            $code = Invoke-RewardsProcess $pythonPath @($mainPath, $Action)
            if ($Action -eq 'run') {
                try {
                    $after = Get-RewardsRunReport
                    if ($null -ne $after -and ($null -eq $before -or $after.Path -ne $before.Path)) { Write-RewardsRunReport $after }
                    else { Write-Host '本次没有生成新的有效运行报告；可用 Rewards.bat report 查看历史记录。' -ForegroundColor Yellow }
                }
                catch { Write-Host ('无法读取本次报告：{0}' -f $_.Exception.Message) -ForegroundColor Yellow }
            }
            return $code
        }
        default { throw "未知操作：$Action。请执行 Rewards.bat help 查看用法。" }
    }
}

function Show-RewardsMenu([string]$At) {
    while ($true) {
        Write-Host ''
        Write-Host '========== Microsoft Rewards =========='
        Write-Host ' 1 立即执行任务        2 检查网页任务'
        Write-Host ' 3 登录/更新会话       4 初始化/更新依赖'
        Write-Host ' 5 查看最近报告        6 安排每日自动运行'
        Write-Host ' 7 查看定时任务        8 移除定时任务'
        Write-Host ' 9 编辑配置            D 打开操作说明'
        Write-Host ' L 打开日志目录        0 退出'
        $choice = Read-Host '请选择（直接回车也退出）'
        if ([string]::IsNullOrWhiteSpace($choice) -or $choice -eq '0') { return 0 }
        $actions = @{ '1' = 'run'; '2' = 'inspect'; '3' = 'login'; '4' = 'setup'; '5' = 'report'; '6' = 'schedule-install'; '7' = 'schedule-show'; '8' = 'schedule-remove'; '9' = 'config'; 'D' = 'guide'; 'L' = 'logs' }
        $selected = $actions[$choice.Trim()]
        if (-not $selected) { Write-Host '没有这个选项，请重新选择。'; continue }
        $selectedAt = $At
        if ($selected -eq 'schedule-install') {
            $enteredAt = Read-Host ('每天几点运行？HH:mm，直接回车使用 {0}' -f $At)
            if (-not [string]::IsNullOrWhiteSpace($enteredAt)) { $selectedAt = $enteredAt.Trim() }
        }
        try { $code = Invoke-RewardsAction $selected $selectedAt }
        catch { Write-Host ('执行失败：{0}' -f $_.Exception.Message) -ForegroundColor Red; $code = 1 }
        Write-Host ('操作退出码：{0}' -f $code)
        $null = Read-Host '按回车返回菜单'
    }
}

function Invoke-RewardsLauncher([string]$Action = 'menu', [string]$At = '20:00') {
    $previousUtf8 = $env:PYTHONUTF8
    $previousIoEncoding = $env:PYTHONIOENCODING
    $previousConsoleEncoding = $null
    $pushed = $false
    try {
        $ErrorActionPreference = 'Stop'
        $env:PYTHONUTF8 = '1'
        $env:PYTHONIOENCODING = 'utf-8'
        try { $previousConsoleEncoding = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
        Push-Location -LiteralPath $script:RewardsRoot
        $pushed = $true
        if ($Action -eq 'menu') { return (Show-RewardsMenu $At) }
        $code = Invoke-RewardsAction $Action $At
        if ($code -eq 2) { Write-Host '需要手动登录或验证，请执行 Rewards.bat login；程序不会自动重试验证码。' -ForegroundColor Yellow }
        elseif ($code -eq 3) { Write-Host '页面或日期数据发生变化，请查看报告和操作说明。' -ForegroundColor Yellow }
        elseif ($code -eq 130) { Write-Host '用户已中断本次运行（退出码 130）。请用 Rewards.bat report 查看已保存的进度；程序不会自动重试。' -ForegroundColor Yellow }
        elseif ($code -ne 0) { Write-Host '操作未完全成功。环境问题请用 setup，登录问题请用 login，运行结果请用 report 查看。' -ForegroundColor Yellow }
        return [int]$code
    }
    catch { Write-Host ('执行失败：{0}' -f $_.Exception.Message) -ForegroundColor Red; return 1 }
    finally {
        if ($pushed) { Pop-Location }
        $env:PYTHONUTF8 = $previousUtf8
        $env:PYTHONIOENCODING = $previousIoEncoding
        if ($null -ne $previousConsoleEncoding) { try { [Console]::OutputEncoding = $previousConsoleEncoding } catch { } }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-RewardsLauncher -Action $Action -At $At)
}
