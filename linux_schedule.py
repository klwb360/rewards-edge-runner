"""Project-owned systemd user timers; never enables lingering or changes system units."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ScheduleError(RuntimeError):
    pass


def unit_name(project_root: Path) -> str:
    root = str(Path(project_root).resolve())
    return "microsoft-rewards-" + sha256(root.encode("utf-8")).hexdigest()[:12]


def _marker(root: Path) -> str:
    return "# Microsoft Rewards managed project: " + json.dumps(str(root.resolve()), ensure_ascii=False)


def _quote(value: Path | str, *, command: bool = False) -> str:
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("定时任务路径不能包含控制字符。")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def _working_directory(value: Path) -> str:
    # This directive consumes the complete raw path: it does not unquote or
    # C-unescape like ExecStart. Quotes and backslashes must remain literal.
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("定时任务路径不能包含控制字符。")
    # A final /. has the same directory meaning, and prevents trailing spaces
    # being stripped or a terminal backslash becoming a line continuation.
    return value.replace("%", "%%") + "/."


def _calendar(config: dict, at: str) -> str:
    if not isinstance(at, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at) is None:
        raise ValueError("--at 必须是 24 小时制 HH:MM，例如 20:00。")
    timezone = config.get("timezone")
    if timezone is not None:
        if not isinstance(timezone, str) or not timezone or any(char.isspace() for char in timezone):
            raise ValueError("timezone 必须是有效的 IANA 时区名称或 null。")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone 必须是有效的 IANA 时区名称。") from exc
    return f"*-*-* {at}:00" + (f" {timezone}" if timezone is not None else "")


def _paths(root: Path, unit_dir: Path | None) -> tuple[str, Path, Path]:
    name = unit_name(root)
    if unit_dir is None:
        unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    directory = Path(unit_dir).absolute()
    for ancestor in (directory, *directory.parents):
        if ancestor.is_symlink():
            raise ScheduleError(f"拒绝使用符号链接形式的 systemd 用户单元目录：{ancestor}")
    for suffix in ("service", "timer"):
        dropin = directory / f"{name}.{suffix}.d"
        if dropin.exists() or dropin.is_symlink():
            raise ScheduleError(f"检测到未经管理的 drop-in 覆盖目录，拒绝修改：{dropin}")
    service, timer = directory / (name + ".service"), directory / (name + ".timer")
    for path in (service, timer):
        _check_owned(path, root)
    _check_enable_link(timer)
    return name, service, timer


def _check_owned(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise ScheduleError(f"单元不是本项目 owned 文件（符号链接），拒绝操作：{path}")
    if not path.exists():
        return
    info = path.stat()
    current_uid = os.getuid() if hasattr(os, "getuid") else info.st_uid
    if not stat.S_ISREG(info.st_mode) or info.st_uid != current_uid:
        raise ScheduleError(f"单元文件归属不符，拒绝操作：{path}")
    if path.read_text(encoding="utf-8").splitlines()[:1] != [_marker(root)]:
        raise ScheduleError(f"单元文件归属不符（foreign），拒绝操作：{path}")


def _check_enable_link(timer: Path) -> Path | None:
    """Only a user-owned enable link resolving to this exact managed timer is ours."""
    wants = timer.parent / "timers.target.wants"
    if wants.is_symlink() or (wants.exists() and not wants.is_dir()):
        raise ScheduleError(f"定时器启用目录归属不符，拒绝操作：{wants}")
    link = wants / timer.name
    if not link.is_symlink():
        if link.exists():
            raise ScheduleError(f"定时器启用文件不是本项目的符号链接，拒绝操作：{link}")
        return None
    info = link.lstat()
    current_uid = os.getuid() if hasattr(os, "getuid") else info.st_uid
    target = Path(os.readlink(link))
    if not target.is_absolute():
        target = link.parent / target
    try:
        matches = target.resolve() == timer.resolve()
    except (OSError, RuntimeError) as exc:
        raise ScheduleError(f"无法验证定时器启用链接归属，拒绝操作：{link}") from exc
    if info.st_uid != current_uid or not matches:
        raise ScheduleError(f"定时器启用链接归属不符，拒绝操作：{link}")
    return link


def _run(runner, argv: list[str], *, check: bool = True):
    try:
        result = runner(argv, text=True, capture_output=True, check=False,
                        env=os.environ | {"LC_ALL": "C", "LANG": "C"})
    except OSError as exc:
        raise ScheduleError(f"无法执行 {argv[0]}：{exc}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "命令失败").strip()
        raise ScheduleError(f"{' '.join(argv)} 失败：{detail}")
    return result


def _remove_unit(runner, action: str, unit: str) -> None:
    arguments = ["systemctl", "--user", action]
    if action == "disable":
        arguments.append("--now")
    arguments.append(unit)
    result = _run(runner, arguments, check=False)
    if result.returncode == 0:
        return
    # systemctl has no structured NotFound error output. Use its C-locale,
    # exact unit-specific diagnostics and reject every other failure/extra line.
    escaped = re.escape(unit)
    missing = (rf"Failed to disable unit: Unit file {escaped} does not exist\." if action == "disable"
               else rf"Failed to stop {escaped}: Unit {escaped} not (?:loaded|found)\.")
    if (result.returncode in (1, 5) and not (result.stdout or "").strip()
            and re.fullmatch(missing, (result.stderr or "").strip())):
        return
    detail = (result.stderr or result.stdout or "命令失败").strip()
    raise ScheduleError(f"{' '.join(arguments)} 失败：{detail}")


def _require_background_session(runner) -> None:
    result = _run(runner, ["systemctl", "--user", "show-environment"], check=False)
    if result.returncode:
        raise ScheduleError("systemctl --user 无法连接用户管理器。请用普通用户的真实登录会话执行；先运行 systemctl --user show-environment 检查。")
    uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
    result = _run(runner, ["loginctl", "show-user", uid, "--property=Linger", "--value"], check=False)
    if result.returncode or result.stdout.strip() != "yes":
        raise ScheduleError(f"当前用户尚未启用 Linger，退出登录后不能保证定时运行。请管理员评估并手动运行 sudo loginctl enable-linger {uid}，然后用 loginctl show-user {uid} -p Linger 检查；脚本不会自动启用。")


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(project_root: Path, config: dict, *, at: str = "20:00", config_path: Path | None = None,
            unit_dir: Path | None = None, runner=None) -> str:
    """Install/update only units carrying this resolved project's ownership marker."""
    runner = runner or subprocess.run
    root = Path(project_root).resolve()
    calendar = _calendar(config, at)
    working_directory = _working_directory(root)
    config_path = Path(config_path).resolve() if config_path is not None else root / "config.json"
    # systemd restricts special characters in argv[0] and does not expand $$
    # there. A fixed env executable preserves the absolute venv path as an
    # ordinary escaped argument; env then executes it directly without a shell.
    command = "/usr/bin/env -- " + " ".join(_quote(value, command=True) for value in
                                           (root / ".venv/bin/python", root / "linux_cli.py", "run", "--config", config_path))
    name, service, timer = _paths(root, unit_dir)
    _require_background_session(runner)
    service_text = f"""{_marker(root)}
[Unit]
Description=Microsoft Rewards daily run ({name})

[Service]
Type=oneshot
WorkingDirectory={working_directory}
ExecStart={command}
UMask=0077
TimeoutStartSec=120min
TimeoutStopSec=45
KillSignal=SIGTERM
KillMode=control-group
Restart=no
"""
    timer_text = f"""{_marker(root)}
[Unit]
Description=Microsoft Rewards daily timer ({name})

[Timer]
OnCalendar={calendar}
Persistent=true
Unit={name}.service

[Install]
WantedBy=timers.target
"""
    _write_private(service, service_text)
    _write_private(timer, timer_text)
    _run(runner, ["systemctl", "--user", "daemon-reload"])
    _run(runner, ["systemctl", "--user", "enable", "--now", name + ".timer"])
    return name


def show(project_root: Path, *, unit_dir: Path | None = None, runner=None) -> str:
    runner = runner or subprocess.run
    root = Path(project_root).resolve()
    name, service, timer = _paths(root, unit_dir)
    if not service.exists() and not timer.exists():
        return f"本项目尚未安装定时任务：{name}"
    sections = [f"本项目定时任务：{name}"]
    for path in (service, timer):
        sections.append(f"{path}\n" + (path.read_text(encoding="utf-8") if path.exists() else "单元文件缺失"))
    result = _run(runner, ["systemctl", "--user", "status", "--no-pager", "--full", name + ".timer", name + ".service"], check=False)
    sections.append((result.stdout or "") + (result.stderr or ""))
    return "\n".join(sections)


def remove(project_root: Path, *, unit_dir: Path | None = None, runner=None) -> bool:
    runner = runner or subprocess.run
    root = Path(project_root).resolve()
    name, service, timer = _paths(root, unit_dir)
    if not service.exists() and not timer.exists():
        return False
    # A missing unit file does not mean a loaded/enabled unit has gone away.
    _remove_unit(runner, "disable", name + ".timer")
    _remove_unit(runner, "stop", name + ".timer")
    _remove_unit(runner, "stop", name + ".service")
    link = _check_enable_link(timer)
    if link is not None:
        link.unlink()
    for path in (service, timer):
        _check_owned(path, root)
        if path.exists():
            path.unlink()
    _run(runner, ["systemctl", "--user", "daemon-reload"])
    return True
