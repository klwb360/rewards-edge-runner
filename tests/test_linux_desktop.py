"""Owned virtual-desktop tests; no host desktop or account is touched."""

import os
import signal
import socket
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import linux_desktop as desktop


class FakeProcess:
    def __init__(self, pid, output=b"", returncode=None):
        self.pid = pid
        self.output = output
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.returncode = 0
        return self.output, b""

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class LinuxDesktopTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="virtual-desktop-test-")
        self.addCleanup(self.folder.cleanup)
        self.state = Path(self.folder.name) / "state"
        self.started = []
        self.signals = []
        self.patches = [
            patch.object(desktop, "_check_runtime"),
            patch.object(desktop, "_validate_state_directory"),
            patch.object(desktop.shutil, "which", side_effect=lambda name: "/usr/bin/" + name),
            patch.object(desktop.subprocess, "Popen", side_effect=self.spawn),
            patch.object(desktop.os, "killpg", side_effect=lambda pid, sig: self.signals.append((pid, sig)), create=True),
            patch.object(desktop.signal, "SIGKILL", 9, create=True),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def spawn(self, argv, **kwargs):
        self.started.append((argv, kwargs))
        process = FakeProcess(42000 + len(self.started))
        if Path(argv[0]).name == "Xvfb":
            number_fd = int(argv[argv.index("-displayfd") + 1])
            os.write(number_fd, b"71\n")
        elif Path(argv[0]).name == "xprop":
            process.output = b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x20020b\n"
        return process

    def test_new_owned_desktop_restores_environment_and_removes_private_state(self):
        with patch.dict(os.environ, {"DISPLAY": ":existing", "XAUTHORITY": "original-auth"}):
            with desktop.virtual_desktop(self.state) as environment:
                self.assertEqual(os.environ["DISPLAY"], ":71")
                self.assertEqual(environment["DISPLAY"], ":71")
                authority = Path(os.environ["XAUTHORITY"])
                self.assertTrue(authority.is_file())
                self.assertTrue(authority.is_relative_to(self.state))
                self.assertIn(b"MIT-MAGIC-COOKIE-1", authority.read_bytes())
                run_dir = authority.parent
            self.assertEqual(os.environ["DISPLAY"], ":existing")
            self.assertEqual(os.environ["XAUTHORITY"], "original-auth")
        self.assertFalse(run_dir.exists())
        argv, options = self.started[0]
        self.assertIn("-displayfd", argv)
        self.assertEqual(argv[argv.index("-nolisten") + 1], "tcp")
        self.assertNotIn("-ac", argv)
        self.assertTrue(all(options["start_new_session"] for _, options in self.started))
        self.assertTrue({pid for pid, _ in self.signals}.issubset({42001, 42002, 42003}))
        self.assertIn((42001, signal.SIGTERM), self.signals)
        self.assertIn((42002, signal.SIGTERM), self.signals)

    def test_keyboard_interrupt_cleans_owned_processes_and_unsets_added_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyboardInterrupt):
                with desktop.virtual_desktop(self.state):
                    raise KeyboardInterrupt()
            self.assertNotIn("DISPLAY", os.environ)
            self.assertNotIn("XAUTHORITY", os.environ)
        self.assertIn((42001, signal.SIGTERM), self.signals)
        self.assertIn((42002, signal.SIGTERM), self.signals)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_startup_error_cleans_xvfb_without_touching_existing_desktop(self):
        spawn = self.spawn

        def fail_openbox(argv, **kwargs):
            if Path(argv[0]).name == "openbox":
                raise OSError("fixture launch error")
            return spawn(argv, **kwargs)

        with patch.object(desktop.subprocess, "Popen", side_effect=fail_openbox):
            with self.assertRaisesRegex(desktop.DesktopError, "openbox"):
                with desktop.virtual_desktop(self.state):
                    self.fail("desktop startup should fail")
        self.assertEqual({pid for pid, _ in self.signals}, {42001})
        self.assertEqual(list(self.state.iterdir()), [])

    def test_invalid_xvfb_display_is_rejected_and_owned_server_is_stopped(self):
        def invalid_display(argv, **kwargs):
            self.started.append((argv, kwargs))
            os.write(int(argv[argv.index("-displayfd") + 1]), b"not-a-display\n")
            return FakeProcess(42001)

        with patch.object(desktop.subprocess, "Popen", side_effect=invalid_display):
            with self.assertRaisesRegex(desktop.DesktopError, "显示号"):
                with desktop.virtual_desktop(self.state):
                    self.fail("invalid display should fail")
        self.assertEqual({pid for pid, _ in self.signals}, {42001})

    def test_default_sigterm_handler_exits_143_and_cleans_owned_desktop(self):
        installed = []
        with patch.object(desktop.signal, "getsignal", return_value=signal.SIG_DFL), \
             patch.object(desktop.signal, "signal", side_effect=lambda sig, handler: installed.append(handler)):
            with self.assertRaises(SystemExit) as raised:
                with desktop.virtual_desktop(self.state):
                    installed[0](signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(installed[-1], signal.SIG_DFL)
        self.assertIn((42001, signal.SIGTERM), self.signals)
        self.assertIn((42002, signal.SIGTERM), self.signals)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_missing_dependencies_fail_before_any_process_starts(self):
        with patch.object(desktop.shutil, "which", return_value=None):
            with self.assertRaisesRegex(desktop.DesktopError, "Xvfb"):
                with desktop.virtual_desktop(self.state):
                    self.fail("missing dependencies should fail")
        self.assertEqual(self.started, [])

    def test_busy_vnc_port_fails_before_prompt_or_process_start(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(desktop.DesktopError, "port|端口"):
                with desktop.virtual_desktop(self.state, vnc=True, vnc_port=port):
                    self.fail("busy VNC port should fail")
        self.assertEqual(self.started, [])

    def test_first_vnc_password_requires_interactive_terminal(self):
        with patch.object(desktop.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(desktop.DesktopError, "terminal|终端|TTY"):
                with desktop.virtual_desktop(self.state, vnc=True):
                    self.fail("password creation should require a terminal")
        self.assertEqual(self.started, [])

    def test_interactive_password_is_created_without_password_argument(self):
        spawn = self.spawn

        def store_password(argv, **kwargs):
            process = spawn(argv, **kwargs)
            if "-storepasswd" in argv:
                Path(argv[-1]).write_bytes(b"fixture!")
            return process

        with patch.object(desktop.sys.stdin, "isatty", return_value=True), \
             patch.object(desktop.subprocess, "Popen", side_effect=store_password), \
             patch.object(desktop, "_validate_password"), patch.object(desktop, "_wait_for_vnc"):
            with desktop.virtual_desktop(self.state, vnc=True, vnc_port=5992):
                self.assertEqual((self.state / "vnc.pass").read_bytes(), b"fixture!")
        argv, options = self.started[0]
        self.assertIn("-storepasswd", argv)
        self.assertNotIn("fixture!", argv)
        self.assertIsNone(options["stdin"])
        self.assertEqual([pid for pid, _ in self.signals].count(42001), 2)

    def test_vnc_server_is_authenticated_and_only_listens_on_ipv4_loopback(self):
        self.state.mkdir()
        password = self.state / "vnc.pass"
        password.write_bytes(b"fixture!")
        with patch.object(desktop, "_validate_password"), patch.object(desktop, "_wait_for_vnc"):
            with desktop.virtual_desktop(self.state, vnc=True, vnc_port=5991):
                pass
        argv = next(argv for argv, _ in self.started if Path(argv[0]).name == "x11vnc")
        self.assertEqual(argv[argv.index("-listen") + 1], "127.0.0.1")
        self.assertIn("-localhost", argv)
        self.assertIn("-no6", argv)
        self.assertIn("-norc", argv)
        self.assertEqual(argv[argv.index("-rfbauth") + 1], str(password.resolve()))
        self.assertEqual(argv[argv.index("-rfbport") + 1], "5991")
        self.assertNotIn("-nopw", argv)


class DesktopValidationTests(unittest.TestCase):
    def test_non_linux_and_root_are_rejected(self):
        with patch.object(desktop.sys, "platform", "win32"):
            with self.assertRaisesRegex(desktop.DesktopError, "Linux"):
                desktop._check_runtime()
        with patch.object(desktop.sys, "platform", "linux"), patch.object(desktop.os, "geteuid", return_value=0, create=True):
            with self.assertRaisesRegex(desktop.DesktopError, "root"):
                desktop._check_runtime()

    def test_password_metadata_rejects_public_foreign_symlink_and_invalid_files(self):
        valid = dict(st_uid=1000, st_mode=stat.S_IFREG | 0o600, st_size=8)
        desktop._check_password_metadata(types.SimpleNamespace(**valid), 1000)
        for invalid in [dict(st_mode=stat.S_IFREG | 0o644), dict(st_uid=1001),
                        dict(st_mode=stat.S_IFLNK | 0o600), dict(st_size=0)]:
            with self.subTest(invalid=invalid), self.assertRaises(desktop.DesktopError):
                desktop._check_password_metadata(types.SimpleNamespace(**(valid | invalid)), 1000)

    def test_writable_or_foreign_state_directory_is_rejected(self):
        valid = dict(st_uid=1000, st_mode=stat.S_IFDIR | 0o700)
        with patch.object(desktop.os, "geteuid", return_value=1000, create=True):
            for invalid in [dict(st_mode=stat.S_IFDIR | 0o777), dict(st_uid=1001)]:
                directory = types.SimpleNamespace(lstat=lambda: types.SimpleNamespace(**(valid | invalid)))
                with self.subTest(invalid=invalid), self.assertRaises(desktop.DesktopError):
                    desktop._validate_state_directory(directory)


if __name__ == "__main__":
    unittest.main()
