"""Daily orchestration checks; no real Rewards account or network is used."""

import json
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import rewards


class EdgeDailyIntegrationTests(unittest.TestCase):
    def execute(self, *, edge_result=True, search_pause=None, command="run", enabled=True, read_error=None):
        events = []
        report = {"status": "started", "tasks": []}
        edge = types.ModuleType("edge_browsing")
        edge.EdgeInterfaceChanged = type("EdgeInterfaceChanged", (RuntimeError,), {})
        progress = types.SimpleNamespace(current_minutes=30, target_minutes=30, completed_today=True, available=True)
        edge.read_edge_progress = Mock(return_value=progress, side_effect=read_error)

        def browse(context, config, state, day, *, check_page, today):
            events.append("edge")
            state["edge_browsing"] = dict(minutes_before=5, minutes_after=30 if edge_result else 5,
                target_minutes=30, waited_seconds=60, status="completed" if edge_result else "timeout")
            return edge_result

        edge.run_edge_browsing = Mock(side_effect=browse)

        def searches(context, config, state, day, progress_reader=None):
            events.append("search")
            state["searches"] = {"opened": 0, "stopped_reason": search_pause}
            if search_pause:
                raise rewards.SearchCreditStopped("fixture search stop")

        def route(request_route):
            parsed = urlsplit(request_route.request.url)
            if parsed.hostname == "rewards.microsoft.com":
                request_route.fulfill(content_type="text/html", body="<script>var dashboard = " +
                    json.dumps({"dailySetPromotions": {}}) + ";</script>")
            else:
                request_route.abort()

        task = rewards.Task("done", "Fixture done", "https://www.bing.com/", "visit", True)
        config = rewards.DEFAULTS | dict(headless=True, search_count=0, timeout_ms=5000,
                                        edge_daily_enabled=enabled)
        with tempfile.TemporaryDirectory(prefix="edge-integration-") as folder:
            with patch.dict("sys.modules", {"edge_browsing": edge}), patch.object(rewards, "STATE", Path(folder)), \
                 patch.object(rewards, "read_task_pages", return_value=[task]), \
                 patch.object(rewards, "restrict_navigation", route), patch.object(rewards, "run_searches", searches):
                code = rewards.run_browser(command, config, report)
        return code, report, events, edge

    def test_search_pause_still_checks_edge_and_preserves_partial_result(self):
        code, report, events, _ = self.execute(search_pause="no_credit")
        self.assertEqual(events, ["search", "edge"])
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["edge_browsing"]["status"], "completed")
        self.assertEqual(report["searches"]["stopped_reason"], "no_credit")

    def test_unexpected_search_counter_change_does_not_continue_edge(self):
        with self.assertRaises(rewards.SearchCreditStopped):
            self.execute(search_pause="counter_changed")

    def test_edge_timeout_prevents_overall_success(self):
        code, report, events, _ = self.execute(edge_result=False)
        self.assertEqual(events, ["search", "edge"])
        self.assertEqual((code, report["status"]), (1, "partial"))

    def test_edge_success_can_finish_normal_run(self):
        code, report, events, edge = self.execute()
        self.assertEqual(events, ["search", "edge"])
        self.assertEqual((code, report["status"]), (0, "completed"))
        edge.run_edge_browsing.assert_called_once()

    def test_old_configuration_does_not_start_edge_phase(self):
        code, report, events, edge = self.execute(enabled=False)
        self.assertEqual(code, 0)
        self.assertEqual(events, ["search"])
        edge.run_edge_browsing.assert_not_called()
        self.assertNotIn("edge_browsing", report)

    def test_inspect_records_minutes_without_starting_or_waiting(self):
        code, report, events, edge = self.execute(command="inspect")
        self.assertEqual(code, 0)
        self.assertEqual(events, [])
        edge.read_edge_progress.assert_called_once()
        edge.run_edge_browsing.assert_not_called()
        self.assertEqual(report["edge_browsing"]["minutes_after"], 30)


class EdgeConfigurationAndInterruptTests(unittest.TestCase):
    def test_edge_about_redirect_requires_login_without_changing_other_page_rules(self):
        page = Mock(url="https://rewards.bing.com/about")
        page.locator.return_value.first.is_visible.return_value = False
        with self.assertRaises(rewards.NeedsHuman):
            rewards.check_edge_page(page)
        page.url = "https://www.bing.com/"
        rewards.check_edge_page(page)

    def test_old_config_keeps_edge_disabled_and_validates_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text("{}", encoding="utf-8")
            config = rewards.load_config(path)
            self.assertFalse(config["edge_daily_enabled"])
            self.assertEqual((config["edge_max_wait_minutes"], config["edge_poll_seconds"]), (60, 60))
            for invalid in [dict(edge_daily_enabled="true"), dict(edge_daily_enabled=True, headless=True),
                            dict(edge_daily_enabled=True, channel="chromium"), dict(edge_max_wait_minutes=0),
                            dict(edge_max_wait_minutes=61), dict(edge_poll_seconds=0)]:
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    rewards.load_config(path)

    def test_keyboard_interrupt_saves_last_observed_progress_and_returns_130(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "config.json"
            path.write_text("{}", encoding="utf-8")

            def interrupted(command, config, report):
                report["edge_browsing"] = {"minutes_before": 5, "minutes_after": 10,
                    "target_minutes": 30, "status": "interrupted", "waited_seconds": 60}
                raise KeyboardInterrupt()

            with patch.object(rewards, "STATE", root / "state"), patch.object(rewards, "LOGS", root / "logs"), \
                 patch.object(rewards, "run_browser", interrupted), \
                 patch.object(rewards, "RotatingFileHandler", return_value=logging.NullHandler()), \
                 patch.object(rewards.logging, "basicConfig"):
                code = rewards.main(["run", "--config", str(path)])
            self.assertEqual(code, 130)
            saved = json.loads(next((root / "logs").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual((saved["status"], saved["exit_code"]), ("interrupted", 130))
            self.assertEqual(saved["edge_browsing"]["minutes_after"], 10)


if __name__ == "__main__":
    unittest.main()
