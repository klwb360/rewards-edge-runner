# Microsoft Rewards 每日任务脚本

Windows + 本机 Microsoft Edge + Python。支持单账户：尝试处理已识别的网页版每日活动、常见测验/投票，执行并核验 Bing 搜索和 Edge 浏览分钟任务；提供 Windows 每日定时任务。

本项目为非微软官方的本地工具。公开仓库仅包含程序、示例配置、测试和操作说明，不包含登录会话、私人搜索词及个人实测记录。

双击 **`Rewards.bat`** 打开中文菜单。完整使用步骤见[本机操作流程](docs/OPERATION_GUIDE.md)。

**使用前了解规则：**微软将机器人、宏及其他自动获取 Rewards 积分的方式列为违规行为，可能限制账户或使积分失效。本脚本无法保证计分、保持连续记录或避免账户限制。[官方账户政策](https://support.microsoft.com/en-us/accounts-billing/rewards/getting-the-most-out-of-microsoft-rewards)、[Microsoft 服务协议](https://www.microsoft.com/zh-cn/servicesagreement)。

## 快速使用

需要 Windows、已安装的 Edge、Python 3.10 或更新版本，以及已加入 Rewards 的个人账户。双击 `Rewards.bat` 后，可依次选择安装环境、登录、检查和运行；也可在终端中执行：

```powershell
# 1. 创建独立 Python 环境并安装固定版本依赖
.\Rewards.bat setup

# 2. 在打开的独立 Edge 窗口手动登录，返回终端按 Enter
.\Rewards.bat login

# 3. 只检查当天的任务，不点击活动、不执行搜索
.\Rewards.bat inspect

# 4. 运行一次
.\Rewards.bat run

# 查看最新的有效运行报告
.\Rewards.bat report

# 5. 每天本机时间 20:00 运行；可改为自己的时间
.\Rewards.bat schedule-install -At 20:00
```

首次登录时，手动处理密码、双重验证、验证码和 Rewards 加入操作。请进入显示个人任务的仪表板后再按 Enter。脚本使用 `.state/profile`，和你平常的 Edge 配置独立。会话过期后需要再次运行 `login`。

不带参数的 BAT 打开交互菜单；带命令参数时直接执行并返回退出码。`setup` 安装依赖，`login` 需要人工登录，`inspect` 只读取任务，`run` 执行网页活动、搜索和已启用的 Edge 浏览阶段。查看报告、日志、配置、流程和计划任务分别使用 `report`、`logs`、`config`、`guide` 和 `schedule-show`。`help` 显示全部命令。原来的 Python/PowerShell 入口继续可用。

定时任务使用当前 Windows 用户的普通权限，仅在电脑运行且用户已登录时执行。默认可能在电池供电时跳过，不会开机或唤醒电脑。移动本目录后，需要从旧目录移除任务，再从新目录安装。

## 配置搜索和运行方式

编辑 `config.json`：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `channel` | `msedge` | 调用已安装的 Edge |
| `headless` | `false` | 显示浏览器；设为 `true` 后在后台运行。`login` 始终显示窗口 |
| `max_tasks` | `12` | 每次最多尝试的网页活动数 |
| `visit_seconds` | `8` | 打开活动后的等待时间 |
| `step_seconds` | `2` | 测验操作和完成状态复查的等待时间 |
| `max_quiz_steps` | `20` | 每项测验/投票最多操作次数 |
| `timeout_ms` | `30000` | 单次浏览器操作超时 |
| `search_count` | `20` | 每次最多尝试多少条不同搜索词；`0` 关闭搜索；还受当天去重和实际积分上限约束 |
| `search_interval_seconds` | `15` | 搜索页面等待时间；不是微软计分保证 |
| `search_terms_file` | `searches.example.txt` | UTF-8 文件，每行一个搜索词 |
| `verify_search_points` | `true` | 从“今日积分”的明细读取必应搜索进度；满额或连续两次没有计分时停止 |
| `edge_daily_enabled` | `true` | 每日运行在搜索后检查 Edge 浏览分钟，已完成时直接跳过 |
| `edge_max_wait_minutes` | `60` | Edge 阶段最多等待 60 分钟；超时仍未确认则记录未完成 |
| `edge_poll_seconds` | `60` | 等待期间每 60 秒重新读取实际分钟及今日完成标记 |

旧配置省略 `verify_search_points` 时，沿用不核验积分的兼容模式；省略 `edge_daily_enabled` 时不启动 Edge 阶段。建议按需使用随附配置。

`searches.example.txt` 是示例主题。建议复制成 `searches.txt`，换成你关注的内容，并修改 `search_terms_file`。空行和 `#` 开头的注释会忽略，重复搜索词会合并。如果词条不足，不会重复填满次数。程序按日期从文件轮换选词，不生成随机搜索词。

配置文件中的搜索词会在提交前写入当天历史，再运行会跳过。网络失败或中断的搜索当天也不会自动重试；更换搜索词文件可能增加当天的新搜索。历史仅保存搜索词哈希，保留最近约 30 个运行日期，不保存完整词条或搜索 URL。

开启 `verify_search_points` 时，脚本通过 Bing 搜索框填写并提交搜索词，每次等待后，从 Rewards 页面打开“今日积分 → 积分明细”读取实际搜索进度。积分满额后立即停止；连续两次搜索没有观察到积分增加时，以 `search_paused` 停止。不会为了凑够次数继续提交。这里读取的是当前账户显示的上限，不假定所有账户都有 60 分或每次都计 3 分。

## Edge 浏览分钟

每日顺序为网页活动、Bing 搜索、Edge 浏览核验。启用该阶段要求 `channel: msedge`、`headless: false`；首次使用请按[微软任务说明](https://rewards.bing.com/promo/edge-browsing-streak)登录 Edge 的个人 Microsoft 账户。网页登录和浏览器账户登录可能需要分别完成。

程序先读取账户实际分钟和今日完成标记。已满且已确认时直接跳过；未满时打开官方浏览入口，保持可见 Edge，允许正常浏览，每隔配置时间从后台观察页重新核验。程序不会反复抢占焦点。只有页面确认今日完成且分钟达到目标，才报告完成；本地等待够 30 分钟或 60 分钟都不算完成证据。

搜索仅因连续未计分而暂停时，仍继续检查 Edge，并保留整轮的部分失败状态。登录失效、验证码、跨日或页面不兼容会停止。Edge 超时或任务不可用返回部分未完成；Ctrl+C 保存最后观察到的进度，退出码为 `130`。

长阶段可能额外等待最多 60 分钟。新安装或显式更新的计划任务总时限为 120 分钟，运行时间仍由 `-At` 决定。已有计划需要再次执行 `schedule-install` 才会采用新时限，程序不会自动修改现有计划。

## 完成判断与兼容范围

脚本兼容旧版 `dashboard.dailySetPromotions`，以及新版 Next.js 仪表板的 `dailySetItems` 和“积分赚取”页的 `activityCards`。首页每日活动按本机日期选择；积分赚取页读取当前可见活动数据，过滤推广卡片和零积分项目。新版页面仅解析内嵌 JSON，不执行脚本文本。点击前会等待卡片的交互处理程序就绪，再进行真实点击，触发网站自己的活动记录逻辑。

仅处理有明确完成标记、可识别类型且链接位于允许的微软域名的任务。数据中同一活动出现相互冲突的完成状态时，会留待人工检查。常见测验和投票会尝试识别已知按钮。**投票会按页面顺序选择首个可点击选项，测验不保证答对。**若你希望自行选择，请先手动完成这些任务，再运行脚本。

活动操作后会重新读取仪表板，只有同一 `offerId` 的 `complete` 或新版 `isCompleted` 明确为真，才记为完成。打不开、未确认、未知类型等情况不会报告获得积分。页面不再提供预期数据或按钮时，需要按实际页面适配；本机日期和服务端任务日期不一致也可能读不到当天活动。待领取积分需要自行在网站领取，脚本不操作兑换、抽奖或捐赠。

原生手机签到、手机阅读、Xbox 实际游戏、购买和兑换任务不在当前范围内。导航遇到不支持的外部域名会停止。验证码、登录失效或账户验证需要手动处理。

**Bing 搜索按可观察到的进度记录结果。**开启积分核验时，JSON 中的 `quota_before`、`quota_after` 和 `observed_points_gained` 保存页面显示的进度变化，观察到增加时 `searches.credit` 为 `observed`。关闭核验的兼容模式只记录页面打开，积分状态保持 `unverified`。顶层 `completed` 表示本次全部启用阶段正常结束；是否达到搜索上限需查看 `quota_after` 和 `stopped_reason`。

启用 Edge 阶段后，报告增加 `edge_browsing`：包含 `minutes_before`、`minutes_after`、`target_minutes`、`completed_before`、`completed_after`、`waited_seconds`、`status` 和 `stopped_reason`。BAT 会显示分钟、今日标记及等待时长。任一启用阶段未确认成功，其他阶段的完成不会覆盖它。

## 日志、查看和移除

`.logs/rewards.log` 保存滚动日志；每次运行产生一份 JSON 报告，含任务状态和搜索数量。报告不保存 Cookie、密码或完整搜索 URL。

| 状态 | 含义 |
| --- | --- |
| `already_completed` | 开始运行前已完成 |
| `completed` | 仪表板确认同一活动完成 |
| `unverified` | 已尝试，未确认完成或积分 |
| `manual` | 当前不支持，或超过本次任务上限 |
| `needs_human` | 需要登录或验证，已停止 |
| `failed` | 浏览器操作失败，已停止 |

退出码：`0` 为登录/检查成功，或全部启用阶段正常结束；`1` 为部分未完成、计分暂停、Edge 等待超时或运行错误；`2` 为需要人工登录/验证；`3` 为页面/日期数据不兼容；`130` 为用户中断且已保存观察结果。

```powershell
# 查看任务及上次运行结果
.\Rewards.bat schedule-show

# 预览安装命令，不修改任务
powershell -NoProfile -ExecutionPolicy Bypass -File .\Schedule.ps1 -Action Install -WhatIf

# 移除本目录对应的定时任务
.\Rewards.bat schedule-remove
```

`.state/` 含有效的浏览器登录会话，请像密码一样保管，不要上传或分享。`.state/`、`.logs/`、`.venv/` 和个人 `searches.txt` 已加入 Git 忽略。移除计划任务不会删除会话或日志。

## 本地验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\test_windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\test_launcher.ps1
.\.venv\Scripts\python.exe -m pip check
```

浏览器测试调用真实 Edge，使用临时配置和完全拦截的本地测试页面；不访问真实账户或积分服务。Windows 测试模拟计划任务操作。它们验证程序行为，真实账户的任务兼容性和实际积分必须另行确认。

实现参考：[Playwright 持久浏览器会话](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[登录状态保管](https://playwright.dev/python/docs/auth)、[Windows 计划任务](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask)。
