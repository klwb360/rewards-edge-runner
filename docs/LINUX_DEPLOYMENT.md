# Linux 服务器每日运行

适用于 Ubuntu 22.04/24.04、Debian 12/13 的 x86_64 服务器；主要验收环境为 Ubuntu 24.04。需要普通用户、Python 3.10 及以上、官方 Microsoft Edge，以及支持用户级服务的 systemd。以下步骤由你在目标服务器执行，本项目不会自动连接服务器。

每日流程仍是网页活动、Bing 搜索、Edge 浏览核验。手机阅读没有接入。虚拟桌面仅提供有界面浏览器环境，不能证明账户具备 Edge 任务资格或能够计分；成功必须由服务器实际分钟和今日完成标记确认。

## 安装和环境检查

先使用自己的普通用户登录服务器。若服务器尚无 Python、venv 或 Git，请管理员先安装基础工具：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git sudo
git clone https://github.com/klwb360/rewards-edge-runner.git
cd rewards-edge-runner
bash Rewards.sh setup --system-deps
bash Rewards.sh doctor
```

`setup --system-deps` 显式请求 sudo 安装 apt 系统依赖；缺少 Edge 时使用 Playwright 随附的微软官方安装程序。已有 Edge 直接复用。运行脚本和浏览器都使用普通用户，不要执行 `sudo bash Rewards.sh ...`。

普通 `bash Rewards.sh setup` 只创建或更新本项目 `.venv` 并安装固定 Python 依赖，不安装系统软件。图形依赖包括 Xvfb、Openbox、xprop/x11-utils；登录 VNC 另需 x11vnc。`doctor` 不启动浏览器、不访问账户或积分服务；它检查配置、依赖、浏览器位置及私有目录权限。

`.state` 和 `.logs` 使用当前用户私有目录；新建文件的 umask 为 `077`。目录权限应为 `700`，VNC 密码为 `600`。若 doctor 报告已有目录归属或权限错误，先核对这些确实是自己的项目文件，再手动修复。不要把 Windows 的 `.state/profile` 复制到服务器，首次登录在服务器本地完成。

## 通过 SSH 隧道首次登录

在自己电脑的终端建立隧道，并保留该 SSH 会话：

```bash
ssh -L 5901:127.0.0.1:5901 user@server
```

在该服务器终端进入项目目录后运行：

```bash
bash Rewards.sh login
```

首次会提示设置本机 VNC 访问密码；这是 VNC 密码，不是 Microsoft 密码。然后使用电脑上的 VNC Viewer 连接 `127.0.0.1:5901`，输入刚设置的 VNC 密码。在打开的独立 Edge 中手动完成网页账户和 Edge 个人账户登录、双重验证或验证码，进入 Rewards 仪表板，再回服务器终端按 Enter。

VNC 只监听服务器的 IPv4 本机地址，使用 SSH 隧道访问；每日运行不会启动 VNC。端口被占用时可使用 `login --vnc-port 5902`，并将隧道与 Viewer 端口同时改为 5902。

每次 `login`、`inspect`、`run` 都建立自己的 Xvfb/Openbox 图形环境，自动分配显示编号。退出时关闭本次浏览器及桌面进程，保留浏览器登录配置。网页语言设为 `zh-CN`；页面结构不兼容会停止，不会靠访问次数推定积分。

```bash
bash Rewards.sh inspect
bash Rewards.sh run
bash Rewards.sh report
```

运行前后核对搜索进度和 Edge 实际分钟。Edge 已满且今日完成时跳过；否则最长等待 60 分钟，每 60 秒读取进度。Linux 上未观察到计分时保留未完成结果。登录过期时重新运行 `login`，不要反复启动自动任务尝试绕过验证。

## 安装每日定时器

配置文件随附 `"timezone": "Asia/Shanghai"`，默认在北京时间每天 20:00 运行。服务器可以保留 UTC 系统时区。旧配置省略 `timezone` 或设为 `null` 时使用服务器本机时区。修改时区或计划时间后重新安装定时器。

为使用户服务能在退出 SSH 或重启后运行，先检查用户管理器和 Linger：

```bash
systemctl --user show-environment
loginctl show-user "$(id -u)" --property=Linger
```

若 Linger 未启用，请管理员执行一次，再重新登录检查：

```bash
sudo loginctl enable-linger "$(id -u)"
```

脚本不会自动启用 Linger。用户管理器不可用或 Linger 未满足时，安装会失败并给出准备命令。

```bash
bash Rewards.sh schedule-install --at 20:00
bash Rewards.sh schedule-show
```

定时器名称由项目绝对路径计算；重复安装更新本项目单元，遇到其他归属、符号链接或未管理的覆盖目录会拒绝修改。服务不配置失败重试；正在运行时不会并发启动，核心还会取得文件锁。

服务为 `Type=oneshot`，`TimeoutStartSec=120min`。停止时先发送 SIGTERM，预留 45 秒保存报告和清理，之后 systemd 才强制终止该服务的进程组。`Persistent=true` 可在恢复运行后补触发一次；程序只处理当前配置时区日期，不补做历史任务。

查看最近一次启动结果及日志，可使用 `schedule-show` 输出的实际单元名称：

```bash
systemctl --user status microsoft-rewards-实际编号.service
journalctl --user -u microsoft-rewards-实际编号.service -n 100 --no-pager
bash Rewards.sh report
```

同一账户选择一台主运行机器；不要把用户会话发布到 GitHub。移动目录前先从旧目录移除定时器，再在新目录安装。

## 报告、退出与移除

JSON 报告位于 `.logs/`，网页、搜索和 Edge 字段与 Windows 一致。`report` 只选择有效的 `run` 报告，会标出历史日期；提交次数和本地等待时长都不作为成功证据。

| 退出码 | 含义 |
| --- | --- |
| `0` | 检查成功或本次启用流程正常结束，仍需核对实际进度 |
| `1` | 未完成、搜索暂停、依赖或操作错误 |
| `2` | 需要手动登录或账户验证 |
| `3` | 页面结构或日期不兼容 |
| `130` | Ctrl+C 中断，保留最后观察进度 |
| `143` | SIGTERM 停止，保留最后观察进度 |

```bash
bash Rewards.sh schedule-remove
```

移除只处理本项目定时器及服务，保留登录会话、搜索历史和运行报告。没有安装任何 cron 任务或系统级服务。

## 验证边界

GitHub Actions 执行 Windows、Ubuntu 及 Debian 隔离检查。Linux 测试包括有界面 Edge、中文页面语言、VNC 回环监听和认证、首次密码交互、文件锁及 SIGTERM 清理；浏览器请求由本地夹具拦截，不使用真实账户。Docker 构建排除会话、日志、私人搜索词和个人检查点。

本次交付不包含实际服务器部署、重启后定时触发验收或 Linux 真实积分验收。部署后需自行核验服务器实际分钟；Windows 既有计分证据不能替代 Linux 账户验收。规则及使用边界见 [README](../README.md)。

参考：[Playwright 有界面运行](https://playwright.dev/python/docs/ci#running-headed)、[systemd 用户定时器](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)、[微软 Edge 浏览任务](https://rewards.bing.com/promo/edge-browsing-streak)。
