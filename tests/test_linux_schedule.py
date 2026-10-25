import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import linux_schedule as schedule


class SystemdFixture:
    def __init__(self, linger="yes", manager=True):
        self.calls = []
        self.linger = linger
        self.manager = manager

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "show-environment"]:
            return subprocess.CompletedProcess(argv, 0 if self.manager else 1, "", "no bus")
        if argv[:2] == ["loginctl", "show-user"]:
            return subprocess.CompletedProcess(argv, 0, self.linger + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "fixture status\n", "")


class LinuxScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ('repo space % "quote" $dollar' if os.name != "nt" else 'repo space % $dollar')
        self.root.mkdir()
        self.units = Path(self.temp.name) / "units"
        self.runner = SystemdFixture()

    def install(self, config=None, at="20:00"):
        return schedule.install(self.root, config or {}, at=at, unit_dir=self.units, runner=self.runner)

    def test_install_writes_safe_user_units_and_enables_only_timer(self):
        name = self.install({"timezone": "Asia/Shanghai"})
        service = (self.units / (name + ".service")).read_text(encoding="utf-8")
        timer = (self.units / (name + ".timer")).read_text(encoding="utf-8")
        for line in ["Type=oneshot", "TimeoutStartSec=120min", "TimeoutStopSec=45", "KillSignal=SIGTERM", "KillMode=control-group", "Restart=no", "UMask=0077"]:
            self.assertIn(line + "\n", service)
        self.assertIn("%%", service)
        if os.name != "nt":
            self.assertIn('\\"quote\\"', service)
        self.assertIn("$$dollar", service)
        self.assertIn("OnCalendar=*-*-* 20:00:00 Asia/Shanghai\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertIn(["systemctl", "--user", "enable", "--now", name + ".timer"], self.runner.calls)
        self.assertNotIn(["systemctl", "--user", "start", name + ".service"], self.runner.calls)

    def test_legacy_calendar_uses_local_zone_and_update_is_owned(self):
        name = self.install()
        self.install(at="09:15")
        timer = (self.units / (name + ".timer")).read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 09:15:00\n", timer)
        self.assertNotIn("Asia/Shanghai", timer)

    def test_names_are_stable_for_resolved_directory_and_unique_between_projects(self):
        self.assertEqual(schedule.unit_name(self.root), schedule.unit_name(self.root / "."))
        self.assertNotEqual(schedule.unit_name(self.root), schedule.unit_name(self.root.parent))

    def test_invalid_time_or_timezone_never_writes_or_calls_systemd(self):
        for at, zone in [("24:00", None), ("8:00", None), ("20:00\nExecStart=/evil", None), ("20:00", "Bad/Zone"), ("20:00", "Asia/Shanghai\nPersistent=false")]:
            with self.subTest(at=at, zone=zone), self.assertRaises(ValueError):
                self.install({"timezone": zone}, at=at)
        self.assertFalse(self.units.exists())
        self.assertEqual(self.runner.calls, [])

    def test_missing_manager_or_linger_is_actionable_and_no_units_written(self):
        for runner, expected in [(SystemdFixture(manager=False), "systemctl --user"), (SystemdFixture(linger="no"), "enable-linger")]:
            self.runner = runner
            with self.subTest(expected=expected), self.assertRaisesRegex(schedule.ScheduleError, expected):
                self.install()
            self.assertFalse(self.units.exists())
            self.assertFalse(any("enable-linger" in arg for call in runner.calls for arg in call))

    def test_foreign_units_refused_for_install_show_and_remove(self):
        self.units.mkdir()
        path = self.units / (schedule.unit_name(self.root) + ".service")
        path.write_text("[Service]\nExecStart=/foreign\n", encoding="utf-8")
        for action in [lambda: self.install(), lambda: schedule.show(self.root, unit_dir=self.units, runner=self.runner), lambda: schedule.remove(self.root, unit_dir=self.units, runner=self.runner)]:
            with self.assertRaisesRegex(schedule.ScheduleError, "归属|owned|foreign"):
                action()
            self.assertEqual(path.read_text(encoding="utf-8"), "[Service]\nExecStart=/foreign\n")
        self.assertEqual(self.runner.calls, [])

    def test_remove_only_owned_units_is_idempotent(self):
        name = self.install()
        foreign = self.units / "unrelated.timer"
        foreign.write_text("foreign", encoding="utf-8")
        self.assertTrue(schedule.remove(self.root, unit_dir=self.units, runner=self.runner))
        self.assertFalse((self.units / (name + ".service")).exists())
        self.assertFalse((self.units / (name + ".timer")).exists())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign")
        self.assertFalse(schedule.remove(self.root, unit_dir=self.units, runner=self.runner))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_symlink_unit_is_refused_without_touching_target(self):
        self.units.mkdir()
        target = Path(self.temp.name) / "target"
        target.write_text("foreign", encoding="utf-8")
        link = self.units / (schedule.unit_name(self.root) + ".timer")
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(schedule.ScheduleError):
            self.install()
        self.assertEqual(target.read_text(encoding="utf-8"), "foreign")

    def test_partial_owned_pair_is_cleaned_and_foreign_dropin_is_refused(self):
        name = self.install()
        (self.units / (name + ".timer")).unlink()
        dropin = self.units / (name + ".service.d")
        dropin.mkdir()
        (dropin / "override.conf").write_text("[Service]\nExecStart=/foreign\n")
        with self.assertRaisesRegex(schedule.ScheduleError, "drop|覆盖"):
            schedule.remove(self.root, unit_dir=self.units, runner=self.runner)
        self.assertTrue((self.units / (name + ".service")).exists())

    def test_custom_config_path_is_preserved_in_service(self):
        config_path = self.root / "custom config.json"
        name = schedule.install(self.root, {}, config_path=config_path, unit_dir=self.units, runner=self.runner)
        content = (self.units / (name + ".service")).read_text(encoding="utf-8")
        self.assertIn("custom config.json", content)
        self.assertIn('"--config"', content)

    def test_partial_pair_disables_and_stops_loaded_timer_and_removes_owned_dangling_link(self):
        name = self.install()
        timer = self.units / (name + ".timer")
        timer.unlink()
        wants = self.units / "timers.target.wants"
        wants.mkdir()
        link = wants / timer.name
        try:
            # Windows absolute symlinks expose extended-length path prefixes;
            # Linux validation also exercises systemd's absolute link target.
            link.symlink_to(Path("..") / timer.name if os.name == "nt" else timer)
        except OSError:
            self.skipTest("symlink creation unavailable")
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "disable" in argv:
                return subprocess.CompletedProcess(argv, 5, "", f"Failed to disable unit: Unit file {name}.timer does not exist.\n")
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.assertTrue(schedule.remove(self.root, unit_dir=self.units, runner=runner))
        self.assertIn(["systemctl", "--user", "disable", "--now", name + ".timer"], calls)
        self.assertIn(["systemctl", "--user", "stop", name + ".timer"], calls)
        self.assertFalse(link.is_symlink())
        self.assertFalse((self.units / (name + ".service")).exists())

    def test_partial_pair_permission_or_bus_failure_is_not_treated_as_missing(self):
        name = self.install()
        (self.units / (name + ".timer")).unlink()
        service = self.units / (name + ".service")
        for error in ["Failed to disable unit: Access denied", "Failed to connect to bus: No medium found", f"Failed to disable unit: Unit file {name}.timer does not exist.\nAccess denied"]:
            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, "", error)

            with self.subTest(error=error), self.assertRaises(schedule.ScheduleError):
                schedule.remove(self.root, unit_dir=self.units, runner=runner)
            self.assertTrue(service.exists())

    def test_true_not_loaded_is_tolerated_but_stop_permission_failure_is_not(self):
        name = self.install()
        (self.units / (name + ".timer")).unlink()
        service = self.units / (name + ".service")

        def runner(argv, **kwargs):
            if "disable" in argv:
                return subprocess.CompletedProcess(argv, 5, "", f"Failed to disable unit: Unit file {name}.timer does not exist.\n")
            if "stop" in argv and argv[-1].endswith(".timer"):
                return subprocess.CompletedProcess(argv, 1, "", "Failed to stop timer: Access denied")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaises(schedule.ScheduleError):
            schedule.remove(self.root, unit_dir=self.units, runner=runner)
        self.assertTrue(service.exists())

        def missing(argv, **kwargs):
            if "disable" in argv:
                return subprocess.CompletedProcess(argv, 5, "", f"Failed to disable unit: Unit file {name}.timer does not exist.\n")
            if "stop" in argv and argv[-1].endswith(".timer"):
                return subprocess.CompletedProcess(argv, 5, "", f"Failed to stop {name}.timer: Unit {name}.timer not loaded.\n")
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.assertTrue(schedule.remove(self.root, unit_dir=self.units, runner=missing))
        self.assertFalse(service.exists())

    def test_foreign_timer_enable_link_is_refused_without_systemd_calls(self):
        name = self.install()
        wants = self.units / "timers.target.wants"
        wants.mkdir()
        target = Path(self.temp.name) / "foreign.timer"
        target.write_text("foreign", encoding="utf-8")
        link = wants / (name + ".timer")
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.runner.calls.clear()
        with self.assertRaises(schedule.ScheduleError):
            schedule.remove(self.root, unit_dir=self.units, runner=self.runner)
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "foreign")

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("systemd-analyze"), "requires Linux systemd-analyze parser")
    def test_real_systemd_parser_accepts_generated_units_for_literal_paths(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        env = os.environ | {"XDG_RUNTIME_DIR": str(runtime), "LC_ALL": "C"}
        for index, directory in enumerate(["normal", "repo space", "repo %U $dollar", 'repo "quote"', "repo \\backslash", "repo trailing\\", "repo trailing "]):
            with self.subTest(directory=directory):
                root = Path(self.temp.name) / directory
                python = root / ".venv/bin/python"
                python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                units = Path(self.temp.name) / f"verify-units-{index}"
                name = schedule.install(root, {"timezone": "Asia/Shanghai"}, unit_dir=units, runner=SystemdFixture())
                result = subprocess.run(["systemd-analyze", "--user", "verify", str(units / (name + ".service")), str(units / (name + ".timer"))], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
