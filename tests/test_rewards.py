import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import rewards


class DashboardTests(unittest.TestCase):
    def test_extracts_json_without_executing_script(self):
        expected = {"dailySetPromotions": {}, "userStatus": {}}
        scripts = ["alert('ignore');", "var dashboard = " + json.dumps(expected) + "; boom();"]
        self.assertEqual(rewards.parse_dashboard(scripts), expected)

    def test_rejects_unknown_schema_and_non_json_script(self):
        for scripts in [["var dashboard = doSomething();"], ["var dashboard = [];"]]:
            with self.subTest(scripts=scripts), self.assertRaises(rewards.InterfaceChanged):
                rewards.parse_dashboard(scripts)

    def test_selects_today_deduplicates_and_preserves_completed(self):
        task = {"offerId": "one", "title": "Today", "destinationUrl": "https://www.bing.com/search?q=one", "promotionType": "urlreward", "complete": False}
        dashboard = {"dailySetPromotions": {"10/24/2026": [dict(task, offerId="old")], "10/25/2026": [task, task, dict(task, offerId="done", complete=True)]}}
        tasks = rewards.tasks_for_day(dashboard, date(2026, 10, 25))
        self.assertEqual([item.offer_id for item in tasks], ["one", "done"])
        self.assertFalse(tasks[0].complete)
        self.assertTrue(tasks[1].complete)

    def test_unpadded_dates_and_string_false(self):
        dashboard = {"dailySetPromotions": {"1/5/2026": [{"offerId": "a", "complete": "false", "promotionType": "urlreward", "destinationUrl": "https://bing.com/"}]}}
        tasks = rewards.tasks_for_day(dashboard, date(2026, 1, 5))
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0].complete)

    def test_does_not_guess_when_today_or_completion_is_missing(self):
        self.assertEqual(rewards.tasks_for_day({"dailySetPromotions": {}}, date(2026, 10, 25)), [])
        dashboard = {"dailySetPromotions": {"10/25/2026": [{"offerId": "a", "destinationUrl": "https://bing.com/", "promotionType": "urlreward"}]}}
        task = rewards.tasks_for_day(dashboard, date(2026, 10, 25))[0]
        self.assertIsNone(task.complete)
        self.assertEqual(task.kind, "manual")

    def test_unknown_kind_is_manual(self):
        dashboard = {"dailySetPromotions": {"10/25/2026": [{"offerId": "a", "promotionType": "shopping", "complete": False}]}}
        self.assertEqual(rewards.tasks_for_day(dashboard, date(2026, 10, 25))[0].kind, "manual")

    def test_malformed_promotion_type_does_not_abort_other_tasks(self):
        for value in (None, 123, [], {}):
            dashboard = {"dailySetPromotions": {"10/25/2026": [{"offerId": "a", "promotionType": value, "complete": False, "destinationUrl": "https://bing.com/"}]}}
            with self.subTest(value=value):
                self.assertEqual(rewards.tasks_for_day(dashboard, date(2026, 10, 25))[0].kind, "manual")

    def test_string_true_is_consistent_with_string_false(self):
        dashboard = {"dailySetPromotions": {"10/25/2026": [{"offerId": "a", "complete": "true", "promotionType": "urlreward", "destinationUrl": "https://bing.com/"}]}}
        self.assertTrue(rewards.tasks_for_day(dashboard, date(2026, 10, 25))[0].complete)

    def test_earn_activity_cards_are_included_with_their_page(self):
        dashboard = {"dailySetPromotions": {}, "activityPromotions": [{"offerId": "extra", "title": "Extra", "destinationUrl": "https://www.bing.com/search?q=extra", "complete": False, "promotionType": "urlreward", "__source": "modern", "__page": "earn", "points": 15}]}
        task = rewards.tasks_for_day(dashboard, date(2026, 10, 25))[0]
        self.assertEqual(task.page, "earn")
        self.assertEqual(task.points, 15)
        self.assertFalse(task.complete)

    def test_conflicting_completion_records_are_left_for_manual_check(self):
        entry = {"offerId": "same", "destinationUrl": "https://bing.com/", "promotionType": "urlreward", "complete": True}
        for values in ([entry, dict(entry, complete=False)], [dict(entry, complete=False), entry]):
            dashboard = {"dailySetPromotions": {"10/25/2026": values}}
            task = rewards.tasks_for_day(dashboard, date(2026, 10, 25))[0]
            self.assertIsNone(task.complete)
            self.assertEqual(task.kind, "manual")


class BoundaryTests(unittest.TestCase):
    def test_navigation_guard_handles_popup_before_frame_exists(self):
        class PopupRequest:
            url = "https://www.bing.com/search?q=fixture"

            def is_navigation_request(self):
                return True

            @property
            def frame(self):
                raise AssertionError("popup frame is not available yet")

        calls = []
        route = SimpleNamespace(request=PopupRequest(), fallback=lambda: calls.append("allowed"),
                                abort=lambda: calls.append("blocked"))
        rewards.restrict_navigation(route)
        self.assertEqual(calls, ["allowed"])
        route.request.url = "https://unknown.test/"
        rewards.restrict_navigation(route)
        self.assertEqual(calls, ["allowed", "blocked"])

    def test_limits_task_navigation_to_known_https_hosts(self):
        for value in ["https://bing.com/search?q=a", "https://www.bing.com/search?q=a", "https://rewards.bing.com/earn", "https://rewards.microsoft.com/"]:
            self.assertTrue(rewards.allowed_task_url(value), value)
        for value in ["http://www.bing.com/", "https://bing.com.attacker.test/", "https://attacker.test/?next=bing.com", "https://user:pass@bing.com/", "https://www.bing.com:999/", "javascript:alert(1)", "file:///C:/secret", "https://127.0.0.1/"]:
            self.assertFalse(rewards.allowed_task_url(value), value)

    def test_bad_config_fails_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for config in [{"max_tasks": 0}, {"max_tasks": True}, {"headless": "false"}, {"visit_seconds": -1}, {"channel": "bogus"}, {"unknown_option": 1}]:
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(config=config), self.assertRaises(ValueError):
                    rewards.load_config(path)

    def test_completion_requires_same_offer_with_explicit_true(self):
        task = rewards.Task("one", "One", "https://bing.com/", "visit", False)
        self.assertEqual(rewards.completion_status(task, []), "unverified")
        self.assertEqual(rewards.completion_status(task, [rewards.Task("two", "Two", "https://bing.com/", "visit", True)]), "unverified")
        self.assertEqual(rewards.completion_status(task, [task]), "unverified")
        self.assertEqual(rewards.completion_status(task, [rewards.Task("one", "One", "https://bing.com/", "visit", True)]), "completed")

    def test_lock_rejects_overlapping_run_and_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.lock"
            with self.assertRaisesRegex(RuntimeError, "example"):
                with rewards.run_lock(path):
                    with self.assertRaises(rewards.AlreadyRunning):
                        with rewards.run_lock(path):
                            self.fail("second run acquired the lock")
                    raise RuntimeError("example")
            with rewards.run_lock(path):
                pass


if __name__ == "__main__":
    unittest.main()
