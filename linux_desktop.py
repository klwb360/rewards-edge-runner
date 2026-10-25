"""A private, owned X11 desktop for one Linux browser invocation."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time


class DesktopError(RuntimeError):
    """The isolated desktop could not be started safely."""


def _check_runtime():
    if not sys.platform.startswith("linux"):
        raise DesktopError("虚拟桌面仅支持 Linux。")
    if os.geteuid() == 0:
        raise DesktopError("请使用普通用户运行，不能以 root 启动浏览器桌面。")


def _check_password_metadata(info, uid):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
            or info.st_mode & 0o077 or not info.st_mode & stat.S_IRUSR
            or info.st_size != 8):
        raise DesktopError("VNC 密码文件必须是当前用户拥有的私有普通文件（chmod 600），且为有效的 8 字节密码文件。")


def _validate_password(path):
    _check_password_metadata(path.lstat(), os.geteuid())


def _validate_state_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise DesktopError("桌面状态目录必须由当前用户拥有，且不能允许其他用户写入；请检查 .state 目录权限。")


def _check_port(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise DesktopError("VNC 端口必须是 1024–65535 之间的整数。")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise DesktopError(f"VNC 端口 127.0.0.1:{port} 不可用；请停止占用程序或选择其他端口。") from exc


@contextmanager
def _termination_handler():
    # The application normally supplies its reporting handler. Cover startup
    # too when this context is used directly, without replacing that handler.
    previous = signal.getsignal(signal.SIGTERM)
    installed = previous == signal.SIG_DFL and threading.current_thread() is threading.main_thread()

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    if installed:
        signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGTERM, previous)


def _stop_owned(process):
    # Every launched process is its own session/group leader. Never select
    # processes by name, display number, or a caller's inherited DISPLAY.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # A leader may have exited while an owned child still lives.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _launch(argv, *, env, cwd, owned, output=None, pass_fds=(), interactive=False):
    try:
        process = subprocess.Popen(
            argv, env=env, cwd=cwd, start_new_session=True, close_fds=True,
            pass_fds=pass_fds, stdin=None if interactive else subprocess.DEVNULL,
            stdout=None if interactive else output,
            stderr=None if interactive else subprocess.STDOUT,
        )
    except OSError as exc:
        raise DesktopError(f"无法启动 {Path(argv[0]).name}：{exc}") from exc
    owned.append(process)
    return process


def _write_authority(path):
    # Xauthority: network-byte-order family, then four length-prefixed fields.
    # FamilyWild (65535) permits this private cookie to match the display that
    # Xvfb chooses through -displayfd. The cookie never appears in argv/logs.
    fields = (b"", b"", b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16))
    record = struct.pack(">H", 65535)
    for field in fields:
        record += struct.pack(">H", len(field)) + field
    path.write_bytes(record)


def _read_display(process, number_file, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        number_file.seek(0)
        result = number_file.read(64)
        if b"\n" in result:
            number = result.strip()
            if not number.isdigit() or int(number) > 65535:
                raise DesktopError("Xvfb 返回了无效的显示号。")
            return ":" + number.decode("ascii")
        if process.poll() is not None:
            raise DesktopError("Xvfb 启动失败；请检查已安装的 Xvfb 和 X11 依赖。")
        time.sleep(0.05)
    raise DesktopError("等待 Xvfb 启动超时。")


def _wait_for_window_manager(xvfb, manager, xprop, env, cwd, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if xvfb.poll() is not None or manager.poll() is not None:
            raise DesktopError("Xvfb 或 Openbox 在桌面就绪前退出。")
        probes = []
        try:
            probe = _launch([xprop, "-root", "_NET_SUPPORTING_WM_CHECK"],
                            env=env, cwd=cwd, owned=probes, output=subprocess.PIPE)
            output, _ = probe.communicate(timeout=2)
            if probe.returncode == 0 and re.search(rb"window id # 0x[1-9a-fA-F][0-9a-fA-F]*", output):
                return
        except subprocess.TimeoutExpired:
            pass
        finally:
            for process in reversed(probes):
                _stop_owned(process)
        time.sleep(0.05)
    raise DesktopError("等待 Openbox 桌面就绪超时；请检查 X11 授权和 xprop。")


def _wait_for_vnc(process, port, log_path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DesktopError(f"x11vnc 启动失败，无法监听 127.0.0.1:{port}；端口可能已被占用。")
        # Read the owned server's startup marker, not just any listener on the
        # port; a competing process must never be mistaken for our VNC server.
        if re.search(rf"(?m)^PORT={port}\s*$", log_path.read_text(encoding="utf-8", errors="replace")):
            return
        time.sleep(0.05)
    raise DesktopError("等待 x11vnc 启动超时。")


def _ensure_password(path, *, executable, env, cwd, owned):
    if os.path.lexists(path):
        _validate_password(path)
        return
    if not sys.stdin.isatty():
        raise DesktopError("首次创建 VNC 密码需要交互终端（TTY）；请在 SSH 终端运行 login。")
    pending = cwd / "vnc.pass.new"
    process = _launch([executable, "-norc", "-storepasswd", str(pending)],
                      env=env, cwd=cwd, owned=owned, interactive=True)
    try:
        if process.wait() != 0 or not pending.exists():
            raise DesktopError("VNC 密码创建已取消或失败。")
    finally:
        _stop_owned(process)
        owned.remove(process)
    _validate_password(pending)
    try:
        # Publish without overwriting a file created by another invocation.
        os.link(pending, path)
    except FileExistsError as exc:
        raise DesktopError("VNC 密码文件在创建期间发生变化，请重试。") from exc
    _validate_password(path)


@contextmanager
def virtual_desktop(state_dir: Path, vnc=False, vnc_port=5901):
    """Set DISPLAY/XAUTHORITY for a new Xvfb + Openbox, then restore them.

    Optional VNC is authenticated, IPv4-loopback-only, and intended for an SSH
    tunnel during manual login. Every process and temporary file is owned by
    this invocation; an existing local or SSH-forwarded desktop is never used.
    """
    _check_runtime()
    names = ["Xvfb", "openbox", "xprop"] + (["x11vnc"] if vnc else [])
    executables = {name: shutil.which(name) for name in names}
    missing = [name for name, executable in executables.items() if not executable]
    if missing:
        raise DesktopError("缺少 Linux 桌面依赖：" + ", ".join(missing))
    if vnc:
        _check_port(vnc_port)
    state_dir = Path(state_dir)
    if state_dir.is_symlink():
        raise DesktopError("桌面状态目录不能是符号链接。")
    original = {key: os.environ.get(key) for key in ("DISPLAY", "XAUTHORITY")}
    owned = []
    with _termination_handler():
        previous_umask = os.umask(0o077)
        try:
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            _validate_state_directory(state_dir)
            state_dir = state_dir.resolve()
            with tempfile.TemporaryDirectory(prefix="desktop-", dir=state_dir) as folder:
                run_dir = Path(folder)
                env = dict(os.environ)
                env.pop("DISPLAY", None)
                env.pop("XAUTHORITY", None)
                # Desktop helpers must not read/write the user's desktop config.
                for key, child in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                   ("XDG_RUNTIME_DIR", "runtime")):
                    (run_dir / child).mkdir(mode=0o700)
                    env[key] = str(run_dir / child)
                env["ICEAUTHORITY"] = str(run_dir / "ICEauthority")
                authority = run_dir / "Xauthority"
                _write_authority(authority)
                env["XAUTHORITY"] = str(authority)
                config = run_dir / "openbox.xml"
                config.write_text('<openbox_config xmlns="http://openbox.org/3.4/rc"><desktops><number>1</number></desktops></openbox_config>', encoding="utf-8")
                try:
                    if vnc:
                        _ensure_password(state_dir / "vnc.pass", executable=executables["x11vnc"],
                                         env=env, cwd=run_dir, owned=owned)
                    with (run_dir / "display-number").open("w+b") as number_file, \
                         (run_dir / "desktop.log").open("ab", buffering=0) as output:
                        xvfb = _launch([executables["Xvfb"], "-displayfd", str(number_file.fileno()),
                                        "-screen", "0", "1440x900x24", "-nolisten", "tcp", "-auth", str(authority)],
                                       env=env, cwd=run_dir, owned=owned, output=output,
                                       pass_fds=(number_file.fileno(),))
                        env["DISPLAY"] = _read_display(xvfb, number_file)
                        manager = _launch([executables["openbox"], "--sm-disable", "--config-file", str(config)],
                                          env=env, cwd=run_dir, owned=owned, output=output)
                        _wait_for_window_manager(xvfb, manager, executables["xprop"], env, run_dir)
                        if vnc:
                            _check_port(vnc_port)
                            server = _launch([executables["x11vnc"], "-norc", "-display", env["DISPLAY"],
                                              "-auth", str(authority), "-listen", "127.0.0.1", "-localhost", "-no6",
                                              "-rfbport", str(vnc_port), "-rfbauth", str(state_dir / "vnc.pass"),
                                              "-forever", "-shared", "-safer", "-noremote"],
                                             env=env, cwd=run_dir, owned=owned, output=output)
                            _wait_for_vnc(server, vnc_port, run_dir / "desktop.log")
                        os.environ.update({key: env[key] for key in original})
                        yield dict(os.environ)
                finally:
                    for process in reversed(owned):
                        _stop_owned(process)
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.umask(previous_umask)
