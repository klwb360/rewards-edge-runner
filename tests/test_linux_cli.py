from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import linux_cli as cli


class LinuxCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        (self.root / "requirements.txt").write_text("playwright==1.64.1\n", encoding="utf-8")

    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "require_user"), redirect_stdout(out), redirect_stderr(err):
            result = cli.main(argv, project_root=self.root)
        return result, out.getvalue() + err.getvalue()

    def test_rejects_root_and_non_linux_before_runtime_actions(self):
        with patch.object(cli.sys, "platform", "linux"), patch.object(cli.os, "geteuid", return_value=0, create=True):
            with self.assertRaisesRegex(RuntimeError, "root"):
                cli.require_user()
        with patch.object(cli.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "Linux"):
                cli.require_user()

    def test_browser_commands_create_desktop_and_propagate_core_exit(self):
        events = []

        @contextmanager
        def desktop(state_dir, **kwargs):
            events.append(("enter", state_dir, kwargs))
            try:
                yield {}
            finally:
                events.append("exit")

        def core(argv):
            events.append(argv)
            return 3

        with patch.dict(sys.modules, {"linux_desktop": SimpleNamespace(virtual_desktop=desktop)}), patch.object(cli, "core_main", core):
            for command in ["login", "inspect", "run"]:
                events.clear()
                result, _ = self.invoke([command])
                self.assertEqual(result, 3)
                self.assertEqual(events, [("enter", self.root / ".state", {"vnc": command == "login", "vnc_port": 5901}), [command, "--config", str(self.root / "config.json")], "exit"])

    def test_directory_alias_uses_canonical_state_config_and_venv_paths(self):
        alias = self.root / "project-alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            # This still exercises real path normalization on Windows hosts
            # where the user cannot create symlinks, without skipping coverage.
            nested = self.root / "nested"
            nested.mkdir()
            alias = nested / ".."
        else:
            self.addCleanup(alias.unlink)
        state_paths, core_arguments, setup_arguments = [], [], []

        @contextmanager
        def desktop(state_dir, **kwargs):
            state_paths.append(state_dir)
            yield {}

        def core(argv):
            core_arguments.append(argv)
            return 3

        with patch.object(cli, "require_user"), patch.dict(sys.modules, {"linux_desktop": SimpleNamespace(virtual_desktop=desktop)}), patch.object(cli, "core_main", core):
            self.assertEqual(cli.main(["inspect"], project_root=alias), 3)
        with patch.object(cli, "find_edge", return_value="/usr/bin/microsoft-edge"), patch.object(cli.subprocess, "run", side_effect=lambda argv, **kw: setup_arguments.append(argv) or subprocess.CompletedProcess(argv, 0)), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.setup(alias), 0)
        self.assertEqual(state_paths, [self.root / ".state"])
        self.assertEqual(core_arguments, [["inspect", "--config", str(self.root / "config.json")]])
        self.assertEqual(setup_arguments, [[sys.executable, "-m", "venv", str(self.root / ".venv")], [str(self.root / ".venv/bin/python"), "-m", "pip", "install", "--requirement", str(self.root / "requirements.txt")]])

    def test_core_exception_still_tears_down_desktop(self):
        events = []

        @contextmanager
        def desktop(*args, **kwargs):
            try:
                yield
            finally:
                events.append("closed")

        with patch.dict(sys.modules, {"linux_desktop": SimpleNamespace(virtual_desktop=desktop)}), patch.object(cli, "core_main", side_effect=RuntimeError("fixture failure")):
            result, text = self.invoke(["run"])
        self.assertEqual(result, 1)
        self.assertIn("fixture failure", text)
        self.assertEqual(events, ["closed"])

    def test_report_ignores_corrupt_inspect_and_invalid_calendar_filename(self):
        logs = self.root / ".logs"
        logs.mkdir()
        report = {"command": "run", "started_at": "2026-10-24T12:00:00+08:00", "status": "partial", "tasks": [{"title": "pending card", "status": "unverified"}], "exit_code": 1, "searches": {"opened": 20, "requested": 20, "skipped_today": 0, "credit": "unverified"}, "edge_browsing": {"status": "timeout", "minutes_before": 0, "minutes_after": 10, "target_minutes": 30, "completed_before": False, "completed_after": False, "waited_seconds": 600, "stopped_reason": "timeout"}}
        (logs / "20261024-120000.json").write_text(json.dumps(report), encoding="utf-8")
        (logs / "20261025-120000.json").write_text('{"command":"inspect"}', encoding="utf-8")
        (logs / "20261026-120000.json").write_text("bad json", encoding="utf-8")
        (logs / "20269999-120000.json").write_text(json.dumps(report | {"status": "completed"}), encoding="utf-8")
        result, output = self.invoke(["report"])
        self.assertEqual(result, 0)
        for text in ["partial", "timeout", "pending card", "20", "0/30", "10/30", "未核验", "历史", "手机"]:
            self.assertIn(text, output)
        self.assertNotIn("本次全部启用阶段正常结束", output)

    def test_report_no_data_returns_nonzero_and_no_desktop_import(self):
        with patch.dict(sys.modules, {"linux_desktop": None}):
            result, output = self.invoke(["report"])
        self.assertEqual(result, 1)
        self.assertIn("报告", output)

    def test_setup_creates_venv_and_pip_without_browser_or_system_changes(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(cli, "find_edge", return_value="/usr/bin/microsoft-edge"), patch.object(cli.subprocess, "run", runner):
            self.assertEqual(cli.setup(self.root), 0)
        self.assertEqual(calls, [[sys.executable, "-m", "venv", str(self.root / ".venv")], [str(self.root / ".venv/bin/python"), "-m", "pip", "install", "--requirement", str(self.root / "requirements.txt")]])

    def test_setup_reuses_existing_edge_and_venv(self):
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("fixture")
        calls = []
        with patch.object(cli, "find_edge", return_value="/usr/bin/microsoft-edge-stable"), patch.object(cli, "install_system_dependencies") as dependencies, patch.object(cli.subprocess, "run", side_effect=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)):
            self.assertEqual(cli.setup(self.root, system_deps=True), 0)
        dependencies.assert_called_once_with()
        self.assertEqual(len(calls), 1)
        self.assertIn("pip", calls[0])

    def test_setup_only_explicit_system_option_installs_missing_edge(self):
        calls = []
        with patch.object(cli, "find_edge", return_value=None), patch.object(cli, "install_system_dependencies"), patch.object(cli.subprocess, "run", side_effect=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.setup(self.root), 0)
                self.assertFalse(any("msedge" in call for call in calls))
                self.assertEqual(cli.setup(self.root, system_deps=True), 0)
        self.assertIn([str(self.root / ".venv/bin/python"), "-m", "playwright", "install", "msedge"], calls)

    def test_failed_dependency_install_does_not_continue(self):
        with patch.object(cli, "install_system_dependencies", side_effect=RuntimeError("apt failed")), patch.object(cli.subprocess, "run") as runner:
            with self.assertRaisesRegex(RuntimeError, "apt failed"):
                cli.setup(self.root, system_deps=True)
        runner.assert_not_called()

    def test_supported_debian_system_install_uses_only_apt_and_unsupported_refuses(self):
        for release, machine in [({"ID": "debian", "VERSION_ID": "13"}, "x86_64"), ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "amd64")]:
            calls = []
            with patch.object(cli, "os_release", return_value=release), patch.object(cli.platform, "machine", return_value=machine), patch.object(cli.shutil, "which", return_value="/usr/bin/sudo"), patch.object(cli.subprocess, "run", side_effect=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)):
                cli.install_system_dependencies()
            self.assertEqual(calls[0], ["sudo", "apt-get", "update"])
            self.assertEqual(calls[1][:4], ["sudo", "apt-get", "install", "-y"])
            self.assertIn("xvfb", calls[1])
        with patch.object(cli, "os_release", return_value={"ID": "debian", "VERSION_ID": "11"}), patch.object(cli.subprocess, "run") as runner:
            with self.assertRaisesRegex(RuntimeError, "支持"):
                cli.install_system_dependencies()
            runner.assert_not_called()

    def test_setup_pip_failure_is_reported_by_cli(self):
        def runner(argv, **kwargs):
            if "pip" in argv:
                raise subprocess.CalledProcessError(8, argv)
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(cli.subprocess, "run", side_effect=runner):
            result, output = self.invoke(["setup"])
        self.assertEqual(result, 1)
        self.assertIn("失败", output)

    def test_doctor_does_not_launch_browser_or_contact_account(self):
        with patch.object(cli, "find_edge", return_value=None), patch.object(cli.shutil, "which", return_value=None), patch.object(cli.importlib.util, "find_spec", return_value=None), patch.object(cli, "core_main") as core, patch.object(cli.subprocess, "run") as runner:
            result, output = self.invoke(["doctor"])
        self.assertEqual(result, 1)
        self.assertIn("Xvfb", output)
        self.assertIn("xprop", output)
        self.assertIn("Edge", output)
        core.assert_not_called()
        runner.assert_not_called()

    def test_invalid_config_fails_before_desktop_or_browser_launch(self):
        (self.root / "config.json").write_text('{"headless": "false"}', encoding="utf-8")
        with patch.dict(sys.modules, {"linux_desktop": None}), patch.object(cli, "core_main") as core:
            result, output = self.invoke(["run"])
        self.assertEqual(result, 1)
        self.assertIn("headless", output)
        core.assert_not_called()

    def test_report_uses_configured_day_and_does_not_infer_edge_completion(self):
        logs = self.root / ".logs"
        logs.mkdir()
        data = {"command": "run", "started_at": "2026-10-26T02:00:00+08:00", "status": "partial", "tasks": [], "edge_browsing": {"minutes_after": 30, "target_minutes": 30, "completed_after": False, "status": "timeout"}}
        (logs / "20261026-020000.json").write_text(json.dumps(data), encoding="utf-8")
        import runtime_context
        with patch.object(runtime_context, "configured_now", return_value=datetime(2026, 10, 25, 22, tzinfo=timezone.utc)):
            result, output = self.invoke(["report"])
        self.assertEqual(result, 0)
        self.assertNotIn("历史", output)
        self.assertIn("30/30", output)
        self.assertIn("未完成", output)
        self.assertNotIn("已完成", output)


@unittest.skipIf(os.name == "nt", "Bash launcher integration runs in Linux validation")
class BashLauncherTests(unittest.TestCase):
    def test_quoted_path_arguments_and_exit_code_for_setup_and_venv_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'repo space % "quote" $literal'
            root.mkdir()
            launcher = root / "Rewards.sh"
            shutil.copyfile(Path(__file__).resolve().parents[1] / "Rewards.sh", launcher)
            capture = root / "arguments"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            bootstrap = fake_bin / "python3"
            venv = root / ".venv/bin/python"
            venv.parent.mkdir(parents=True)
            for executable in (bootstrap, venv):
                executable.write_text('#!/bin/sh\nprintf \'%s\\0\' "$0" "$@" > "$CAPTURE"\nexit 7\n', encoding="utf-8")
                executable.chmod(0o700)
            env = os.environ | {"PATH": str(fake_bin) + os.pathsep + os.environ["PATH"], "CAPTURE": str(capture)}
            for arguments, expected_python in [(["setup", "--system-deps"], bootstrap), (["run", "--config", "config space.json"], venv), (["--config", "config space.json", "setup"], bootstrap)]:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(["bash", str(launcher), *arguments], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 7, result.stderr)
                    self.assertEqual(capture.read_bytes().split(b"\0")[:-1], [str(expected_python).encode(), str(root / "linux_cli.py").encode(), *[arg.encode() for arg in arguments]])


if __name__ == "__main__":
    unittest.main()
