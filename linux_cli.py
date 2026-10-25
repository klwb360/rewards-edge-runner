"""Linux launcher for the shared Rewards core, without importing browser dependencies at startup."""

from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
SYSTEM_PACKAGES = ["python3-venv", "python3-pip", "xvfb", "xauth", "x11-utils", "openbox",
                   "x11vnc", "dbus-x11", "fonts-noto-cjk", "ca-certificates"]


def require_user() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Rewards.sh 仅支持 Linux；Windows 请使用 Rewards.bat。")
    if os.geteuid() == 0:
        raise RuntimeError("请以普通用户运行，不能使用 root 或 sudo 启动浏览器；仅 setup --system-deps 内部请求 sudo 安装系统软件。")


def find_edge() -> str | None:
    return shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable")


def os_release() -> dict:
    if hasattr(platform, "freedesktop_os_release"):
        return platform.freedesktop_os_release()
    values = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip('"\'')
    return values


def install_system_dependencies() -> None:
    release = os_release()
    supported = {("ubuntu", "22.04"), ("ubuntu", "24.04"), ("debian", "12"), ("debian", "13")}
    if (release.get("ID"), release.get("VERSION_ID")) not in supported or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("系统依赖自动安装仅支持 Ubuntu 22.04/24.04、Debian 12/13 的 x86_64；其他系统请手动安装依赖与官方 Edge。")
    if shutil.which("sudo") is None or shutil.which("apt-get") is None:
        raise RuntimeError("安装系统依赖需要 sudo 与 apt-get，请联系管理员安装。")
    subprocess.run(["sudo", "apt-get", "update"], check=True)
    subprocess.run(["sudo", "apt-get", "install", "-y", "--no-install-recommends", *SYSTEM_PACKAGES], check=True)


def setup(project_root: Path, *, system_deps: bool = False) -> int:
    root = Path(project_root).resolve()
    if system_deps:
        install_system_dependencies()
    python = root / ".venv/bin/python"
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--requirement", str(root / "requirements.txt")], check=True)
    if find_edge() is None:
        if system_deps:
            # Playwright's bundled msedge installer uses Microsoft's official package source.
            subprocess.run([str(python), "-m", "playwright", "install", "msedge"], check=True)
        else:
            print("未找到 Edge。可手动安装微软官方 Edge，或运行 bash Rewards.sh setup --system-deps；普通 setup 不会安装系统软件。")
    print("Python 环境已准备。下一步：bash Rewards.sh doctor；首次登录：bash Rewards.sh login。")
    return 0


def _load_config(config_path: Path) -> dict:
    # rewards imports only stdlib/parser modules; Playwright itself is lazy in run_browser.
    from rewards import load_config
    return load_config(config_path)


def doctor(project_root: Path, config_path: Path) -> int:
    """Read-only local checks. No browser, display, account or network is opened."""
    problems = []
    try:
        config = _load_config(config_path)
        print(f"配置有效：{config_path}；时区：{config.get('timezone') or '本机本地时间'}")
    except (OSError, ValueError) as exc:
        problems.append(f"配置无效：{exc}")
    if sys.version_info < (3, 10):
        problems.append("需要 Python 3.10 或更新版本。")
    python = project_root / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        problems.append("缺少可执行的 .venv/bin/python；先运行 setup。")
    if importlib.util.find_spec("playwright") is None:
        problems.append("缺少 Playwright Python 依赖；请在项目 venv 中运行 setup。")
    edge = find_edge()
    if edge:
        print(f"Edge：{edge}")
    else:
        problems.append("未找到微软 Edge（microsoft-edge / microsoft-edge-stable）。")
    for binary in ("Xvfb", "xprop", "openbox", "x11vnc", "systemctl", "loginctl"):
        if shutil.which(binary) is None:
            problems.append(f"缺少命令：{binary}")
    uid = os.getuid() if hasattr(os, "getuid") else None
    for name in (".state", ".logs"):
        path = project_root / name
        if path.is_symlink():
            problems.append(f"{name} 是符号链接，拒绝用于私有状态。")
        elif path.exists():
            info = path.stat()
            if not path.is_dir() or (uid is not None and info.st_uid != uid) or stat.S_IMODE(info.st_mode) & 0o077:
                problems.append(f"{name} 应是当前用户拥有、权限为 700 的目录；请核对后手动修复。")
            elif not os.access(path, os.W_OK | os.X_OK):
                problems.append(f"{name} 不可写。")
        elif not os.access(project_root, os.W_OK | os.X_OK):
            problems.append(f"无法在项目目录创建 {name}。")
    for problem in problems:
        print("检查失败：" + problem)
    print("本次只检查本机环境；账号、登录会话、网络、积分和定时服务状态均未核验。")
    return 1 if problems else 0


def core_main(argv: list[str]) -> int:
    from rewards import main as rewards_main
    return rewards_main(argv)


def latest_run_report(logs: Path) -> tuple[Path, dict, datetime] | None:
    if not logs.is_dir():
        return None
    for path in sorted(logs.glob("*.json"), key=lambda item: item.name, reverse=True):
        if not path.is_file() or re.fullmatch(r"\d{8}-\d{6}(?:-\d{6})?\.json", path.name) is None:
            continue
        try:
            datetime.strptime(path.stem, "%Y%m%d-%H%M%S-%f" if len(path.stem) > 15 else "%Y%m%d-%H%M%S")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or data.get("command") != "run" or not isinstance(data.get("tasks"), list) or not isinstance(data.get("status"), str) or not data["status"].strip():
                continue
            started = datetime.fromisoformat(data["started_at"])
            if started.tzinfo is None or not all(isinstance(item, dict) for item in data["tasks"]):
                continue
            return path, data, started
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _value(value) -> str:
    return "未记录" if value is None else str(value)


def _completion(value) -> str:
    return ("已完成" if value else "未完成") if type(value) is bool else "未核验"


def report(project_root: Path, config_path: Path) -> int:
    item = latest_run_report(project_root / ".logs")
    if item is None:
        print("尚无有效的运行报告，请先执行 bash Rewards.sh run。")
        return 1
    path, data, started = item
    from runtime_context import configured_now
    now = configured_now(_load_config(config_path))
    started = started.astimezone(now.tzinfo)
    print(f"运行时间：{started.isoformat(sep=' ', timespec='seconds')}\n报告文件：{path}")
    if started.date() != now.date():
        print("注意：这是历史运行报告，不能代表今天的任务状态。")
    labels = {"completed": "本次全部启用阶段正常结束", "partial": "部分任务未确认完成", "needs_human": "需要手动登录或验证", "interface_changed": "页面或日期数据不兼容", "search_paused": "搜索计分暂停，已停止", "error": "运行出错", "started": "尚无结束结果", "interrupted": "用户已中断运行"}
    print(f"运行状态：{labels.get(data['status'], '报告中的状态')}（{data['status']}）；退出码：{_value(data.get('exit_code'))}")
    completed = sum(task.get("status") == "completed" for task in data["tasks"])
    already = sum(task.get("status") == "already_completed" for task in data["tasks"])
    print(f"网页任务：本轮确认 {completed}，此前完成 {already}，其余待处理/未确认 {len(data['tasks']) - completed - already}，合计 {len(data['tasks'])}。")
    for task in data["tasks"]:
        if task.get("status") not in {"completed", "already_completed"}:
            print(f"  {_value(task.get('status'))}：{_value(task.get('title'))}")
    edge = data.get("edge_browsing")
    if isinstance(edge, dict):
        print(f"Edge 浏览阶段：{_value(edge.get('status'))}。")
        print(f"Edge 页面分钟进度：{_value(edge.get('minutes_before'))}/{_value(edge.get('target_minutes'))} → {_value(edge.get('minutes_after'))}/{_value(edge.get('target_minutes'))}。")
        print(f"Edge 页面完成标记：{_completion(edge.get('completed_before'))} → {_completion(edge.get('completed_after'))}；本轮等待 {_value(edge.get('waited_seconds'))} 秒。")
        print(f"Edge 停止原因：{_value(edge.get('stopped_reason'))}")
    search = data.get("searches")
    if not isinstance(search, dict):
        print("Bing 搜索：本份报告未记录搜索结果。")
    else:
        print(f"Bing 搜索：已提交 {_value(search.get('opened'))}，本轮配置上限 {_value(search.get('requested'))}，当天去重跳过 {_value(search.get('skipped_today'))}。")
        before, after = search.get("quota_before"), search.get("quota_after")
        if isinstance(after, dict) and after.get("current") is not None and after.get("maximum") is not None:
            before_text = f"{_value(before.get('current'))}/{_value(before.get('maximum'))}" if isinstance(before, dict) else "未记录"
            print(f"页面显示的搜索积分：{before_text} → {after['current']}/{after['maximum']}；本轮观察到增加 {_value(search.get('observed_points_gained'))} 分。")
        else:
            print("搜索积分：未核验，提交次数不代表积分到账。")
        print(f"计分记录：{_value(search.get('credit'))}")
        if search.get("stopped_reason"):
            print(f"搜索停止原因：{search['stopped_reason']}")
    if data.get("reason"):
        print(f"运行说明：{data['reason']}")
    print("手机 App 签到/阅读任务：未核验，请在手机端查看。")
    return 0


def main(argv=None, *, project_root: Path | None = None) -> int:
    root = Path(project_root).resolve() if project_root is not None else ROOT
    parser = argparse.ArgumentParser(description="Microsoft Rewards Linux 单账户入口（普通用户）")
    parser.add_argument("--config", type=Path, default=root / "config.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("setup", "doctor", "login", "inspect", "run", "report", "schedule-install", "schedule-show", "schedule-remove"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if command == "setup":
            subparser.add_argument("--system-deps", action="store_true", help="显式允许 sudo apt 安装依赖和缺少的官方 Edge")
        if command == "login":
            subparser.add_argument("--vnc-port", type=int, default=5901)
        if command == "schedule-install":
            subparser.add_argument("--at", default="20:00", help="HH:MM；按配置时区运行，未设时区时按本机时间")
    args = parser.parse_args(argv)
    try:
        require_user()
        os.umask(0o077)
        config_path = args.config.resolve()
        if args.command == "setup":
            return setup(root, system_deps=args.system_deps)
        if args.command == "doctor":
            return doctor(root, config_path)
        if args.command == "report":
            return report(root, config_path)
        if args.command.startswith("schedule-"):
            import linux_schedule
            if args.command == "schedule-install":
                name = linux_schedule.install(root, _load_config(config_path), at=args.at, config_path=config_path)
                print(f"已安装每日定时任务：{name}；Persistent 仅补跑当前日期，不重放历史任务。")
            elif args.command == "schedule-show":
                print(linux_schedule.show(root))
            else:
                print("已移除本项目定时任务。" if linux_schedule.remove(root) else "本项目未安装定时任务。")
            return 0
        _load_config(config_path)
        from linux_desktop import virtual_desktop
        with virtual_desktop(root / ".state", vnc=args.command == "login", vnc_port=getattr(args, "vnc_port", 5901)):
            return core_main([args.command, "--config", str(config_path)])
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
