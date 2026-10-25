import json
import unittest
from unittest.mock import Mock, patch
from datetime import date, timedelta
from types import SimpleNamespace

from edge_browsing import (EdgeInterfaceChanged, EdgeProgress, parse_edge_progress,
                           read_edge_progress, run_edge_browsing)


def edge_item(**changes):
    return dict(title="DailyCheckIn_Edge_Title", partner="edge", activitiesCompleted=0,
                activitiesTotal=30, isCurrentDayCompleted=False, isEnabled=True,
                activationActivityType=714, hash="never-copy-this") | changes


def scripts_for(value, cuts=()):
    stream = "a:" + json.dumps(value, ensure_ascii=False) + "\n"
    starts = (0, *cuts)
    ends = (*cuts, len(stream))
    return ["self.__next_f.push(" + json.dumps([1, stream[start:end]]) + ");"
            for start, end in zip(starts, ends)]


class EdgeParserTests(unittest.TestCase):
    def test_reads_zero_partial_full_and_exact_server_completion(self):
        for current, completed in [(0, False), (5, False), (30, False), (30, True)]:
            with self.subTest(current=current, completed=completed):
                result = parse_edge_progress(scripts_for({"model": {"streak": edge_item(
                    activitiesCompleted=current, isCurrentDayCompleted=completed)}}, cuts=(18, 90)))
                self.assertEqual(result, EdgeProgress(current, 30, completed, True))
                self.assertNotIn("hash", vars(result))

    def test_partner_identifies_edge_and_disabled_is_not_missing(self):
        item = edge_item(title="Localized Edge", isEnabled=False)
        self.assertEqual(parse_edge_progress(scripts_for({"streak": item})), EdgeProgress(0, 30, False, False))

    def test_known_edge_title_requires_consistent_edge_partner(self):
        for partner in ("bing", "", None):
            item = edge_item(partner=partner)
            if partner is None:
                del item["partner"]
            with self.subTest(partner=partner), self.assertRaises(EdgeInterfaceChanged):
                parse_edge_progress(scripts_for({"streak": item}))

    def test_unrelated_partner_metadata_does_not_invalidate_actual_streak(self):
        value = {
            "dailySetItems": [],
            "metadata": {"partner": "edge"},
            "model": {"streak": edge_item(activitiesCompleted=30, isCurrentDayCompleted=True)},
        }
        self.assertEqual(parse_edge_progress(scripts_for(value)), EdgeProgress(30, 30, True))

    def test_partner_metadata_outside_streak_cannot_report_completion(self):
        for metadata in [{"partner": "edge"}, edge_item(
                title="Unrelated metadata", activitiesCompleted=30, isCurrentDayCompleted=True)]:
            with self.subTest(metadata=metadata):
                self.assertIsNone(parse_edge_progress(scripts_for({
                    "dailySetItems": [], "metadata": metadata,
                    "model": {"streak": {"title": "Other streak", "partner": "bing"}},
                })))

    def test_supported_page_without_edge_returns_none_ignoring_other_streaks(self):
        for value in [{"dailySetItems": []}, {"activityCards": []},
                      {"streak": {"title": "Other streak", "partner": "bing", "hash": "ignored"}}]:
            with self.subTest(value=value):
                self.assertIsNone(parse_edge_progress(scripts_for(value)))

    def test_identical_duplicates_collapse_but_conflicts_stop(self):
        item = edge_item()
        self.assertEqual(parse_edge_progress(scripts_for([{"streak": item}, {"streak": item}])), EdgeProgress(0, 30, False))
        with self.assertRaises(EdgeInterfaceChanged):
            parse_edge_progress(scripts_for([{"streak": item}, {"streak": edge_item(activitiesCompleted=5)}]))

    def test_unknown_malformed_and_invalid_fields_stop(self):
        for scripts in [["var something = {};"], scripts_for({"unrecognized": []}),
                        ['self.__next_f.push([1,"a:{broken}\\n"]);']]:
            with self.subTest(scripts=scripts), self.assertRaises(EdgeInterfaceChanged):
                parse_edge_progress(scripts)
        for change in [dict(activitiesCompleted=-1), dict(activitiesCompleted=31),
                       dict(activitiesCompleted=True), dict(activitiesTotal=0),
                       dict(activitiesTotal="30"), dict(isCurrentDayCompleted="true"),
                       dict(isEnabled=1), dict(activationActivityType=999)]:
            with self.subTest(change=change), self.assertRaises(EdgeInterfaceChanged):
                parse_edge_progress(scripts_for({"streak": edge_item(**change)}))
        missing = edge_item()
        del missing["isEnabled"]
        with self.assertRaises(EdgeInterfaceChanged):
            parse_edge_progress(scripts_for({"streak": missing}))


class FakePage:
    def __init__(self, label):
        self.label = label
        self.closed = False
        self.foreground_calls = 0
        self.urls = []
        self.waited_ms = []

    def goto(self, url, **kwargs):
        self.urls.append(url)

    def bring_to_front(self):
        self.foreground_calls += 1

    def close(self):
        self.closed = True

    def wait_for_timeout(self, milliseconds):
        self.waited_ms.append(milliseconds)


class FakeContext:
    def __init__(self):
        self.pages = []

    def new_page(self):
        page = FakePage("observer" if not self.pages else "browsing")
        self.pages.append(page)
        return page


class VirtualClock:
    def __init__(self):
        self.seconds = 0

    def __call__(self):
        return self.seconds

    def wait(self, seconds):
        self.seconds += seconds


class EdgeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.context = FakeContext()
        self.report = {}
        self.clock = VirtualClock()
        self.day = date(2026, 10, 25)
        self.config = dict(edge_max_wait_minutes=60, edge_poll_seconds=60, timeout_ms=5000)

    def run_sequence(self, values, **overrides):
        iterator = iter(values)
        latest = [None]

        def reader():
            latest[0] = next(iterator, latest[0])
            return latest[0]

        arguments = dict(check_page=lambda page: None, progress_reader=reader,
                         clock=self.clock, wait=self.clock.wait, today=lambda: self.day)
        arguments.update(overrides)
        return run_edge_browsing(self.context, self.config, self.report, self.day, **arguments)

    def test_initial_completion_does_not_open_browsing_tab(self):
        self.assertTrue(self.run_sequence([EdgeProgress(30, 30, True)]))
        self.assertEqual(len(self.context.pages), 1)
        self.assertTrue(self.context.pages[0].closed)
        self.assertEqual(self.report["edge_browsing"]["status"], "already_completed")

    def test_initial_fetch_time_is_not_reported_as_browsing_wait(self):
        def reader():
            self.clock.seconds += 7
            return EdgeProgress(30, 30, True)

        self.assertTrue(run_edge_browsing(self.context, self.config, self.report, self.day,
            check_page=lambda page: None, progress_reader=reader, clock=self.clock,
            wait=self.clock.wait, today=lambda: self.day))
        summary = self.report["edge_browsing"]
        self.assertEqual(summary["status"], "already_completed")
        self.assertEqual(summary["stopped_reason"], "already_completed")
        self.assertEqual(summary["waited_seconds"], 0)

    def test_expired_wait_does_not_start_another_server_read(self):
        calls = []
        def reader():
            calls.append(self.clock.seconds)
            return EdgeProgress(5, 30, False)
        def overshoot(seconds):
            self.clock.seconds += 3601

        self.assertFalse(run_edge_browsing(self.context, self.config, self.report, self.day,
            check_page=lambda page: None, progress_reader=reader, clock=self.clock,
            wait=overshoot, today=lambda: self.day))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.report["edge_browsing"]["status"], "timeout")
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_over_budget_reader_result_cannot_turn_task_completed(self):
        self.config["edge_max_wait_minutes"] = 1
        self.config["edge_poll_seconds"] = 1
        calls = []
        def reader():
            calls.append(self.clock.seconds)
            if len(calls) > 1:
                self.clock.seconds += 61
                return EdgeProgress(30, 30, True)
            return EdgeProgress(5, 30, False)

        self.assertFalse(run_edge_browsing(self.context, self.config, self.report, self.day,
            check_page=lambda page: None, progress_reader=reader, clock=self.clock,
            wait=self.clock.wait, today=lambda: self.day))
        summary = self.report["edge_browsing"]
        self.assertEqual(summary["status"], "timeout")
        self.assertEqual(summary["minutes_after"], 5)
        self.assertFalse(summary["completed_after"])
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_expired_reader_timeout_is_reported_as_stage_timeout(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from edge_browsing import EdgeReadTimeout
        self.config["edge_max_wait_minutes"] = 1
        self.config["edge_poll_seconds"] = 1
        for exception_type in (BrowserTimeout, EdgeReadTimeout):
            self.context = FakeContext()
            self.clock = VirtualClock()
            calls = []
            def reader():
                calls.append(self.clock.seconds)
                if len(calls) > 1:
                    self.clock.seconds += 61
                    raise exception_type("fixture read timeout")
                return EdgeProgress(5, 30, False)

            with self.subTest(exception_type=exception_type):
                self.assertFalse(run_edge_browsing(self.context, self.config, self.report, self.day,
                    check_page=lambda page: None, progress_reader=reader, clock=self.clock,
                    wait=self.clock.wait, today=lambda: self.day))
                self.assertEqual(self.report["edge_browsing"]["status"], "timeout")
                self.assertEqual(self.report["edge_browsing"]["minutes_after"], 5)
                self.assertTrue(all(page.closed for page in self.context.pages))

    def test_reader_normalizes_navigation_timeout_without_reclassifying_model_errors(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from edge_browsing import EdgeReadTimeout
        page = Mock()
        page.goto.side_effect = BrowserTimeout("fixture read timeout")
        with self.assertRaises(EdgeReadTimeout):
            read_edge_progress(page, 5000, lambda item: None)

    def test_real_reader_keeps_malformed_model_error_at_stage_deadline(self):
        from edge_browsing import EdgeReadTimeout
        clock = self.clock
        class Observer(FakePage):
            def __init__(self):
                super().__init__("observer")
                self.reads = 0
            def goto(self, url, **kwargs):
                self.reads += 1
                return SimpleNamespace(status=200)
            def locator(self, selector):
                return self
            def all_text_contents(self):
                if self.reads == 1:
                    return scripts_for({"streak": edge_item(activitiesCompleted=5)})
                return ['self.__next_f.push([1,"a:{broken}\\n"]);']
            def wait_for_timeout(self, milliseconds):
                clock.wait(milliseconds / 1000)

        class Context(FakeContext):
            def new_page(self):
                page = Observer() if not self.pages else FakePage("browsing")
                self.pages.append(page)
                return page

        self.context = Context()
        self.config.update(edge_max_wait_minutes=1, edge_poll_seconds=59)
        with patch("edge_browsing.monotonic", clock):
            with self.assertRaises(EdgeInterfaceChanged) as raised:
                run_edge_browsing(self.context, self.config, self.report, self.day,
                    check_page=lambda page: None, clock=clock, wait=clock.wait,
                    today=lambda: self.day)
        self.assertNotIsInstance(raised.exception, EdgeReadTimeout)
        self.assertEqual(clock.seconds, 60)
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_auth_redirect_is_checked_before_expired_timeout_is_classified(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        class AuthenticationRequired(RuntimeError):
            pass
        original_goto = FakePage.goto
        for phase in ("observer", "browsing"):
            self.context = FakeContext()
            self.clock = VirtualClock()
            self.config.update(edge_max_wait_minutes=1, edge_poll_seconds=1)
            calls = []
            def slow_goto(page, url, **kwargs):
                if phase == "browsing" and page.label == "browsing":
                    page.url = "https://login.live.com/"
                    self.clock.wait(61)
                    raise BrowserTimeout("navigation timed out after login redirect")
                return original_goto(page, url, **kwargs)
            def reader():
                calls.append(True)
                if phase == "observer" and len(calls) > 1:
                    self.context.pages[0].url = "https://login.live.com/"
                    self.clock.wait(61)
                    raise BrowserTimeout("read timed out after login redirect")
                return EdgeProgress(5, 30, False)
            def auth(page):
                if getattr(page, "url", "").startswith("https://login.live.com/"):
                    raise AuthenticationRequired("manual sign-in required")

            with self.subTest(phase=phase), patch.object(FakePage, "goto", slow_goto):
                with self.assertRaises(AuthenticationRequired):
                    run_edge_browsing(self.context, self.config, self.report, self.day,
                        check_page=auth, progress_reader=reader, clock=self.clock,
                        wait=self.clock.wait, today=lambda: self.day)
                self.assertTrue(all(page.closed for page in self.context.pages))

    def test_reader_timeout_before_stage_expiry_still_propagates(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        def reader():
            raise BrowserTimeout("fixture read timeout")

        with self.assertRaises(BrowserTimeout):
            run_edge_browsing(self.context, self.config, self.report, self.day,
                check_page=lambda page: None, progress_reader=reader, clock=self.clock,
                wait=self.clock.wait, today=lambda: self.day)
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_expired_model_and_auth_errors_are_not_swallowed_as_timeout(self):
        class AuthenticationRequired(RuntimeError):
            pass

        for exception in (EdgeInterfaceChanged("invalid model"), AuthenticationRequired("verification required")):
            self.context = FakeContext()
            self.clock = VirtualClock()
            def reader():
                self.clock.seconds += 3601
                raise exception
            with self.subTest(exception=type(exception)), self.assertRaises(type(exception)):
                run_edge_browsing(self.context, self.config, self.report, self.day,
                    check_page=lambda page: None, progress_reader=reader, clock=self.clock,
                    wait=self.clock.wait, today=lambda: self.day)
            self.assertTrue(all(page.closed for page in self.context.pages))

    def test_expired_entry_navigation_timeout_is_reported_as_stage_timeout(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        original_goto = FakePage.goto
        def slow_goto(page, url, **kwargs):
            if page.label == "browsing":
                self.clock.seconds += 3601
                raise BrowserTimeout("fixture navigation timeout")
            return original_goto(page, url, **kwargs)

        with patch.object(FakePage, "goto", slow_goto):
            self.assertFalse(self.run_sequence([EdgeProgress(5, 30, False)]))
        self.assertEqual(self.report["edge_browsing"]["status"], "timeout")
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_initial_poll_and_finish_progress_are_logged(self):
        with self.assertLogs("edge_browsing", level="INFO") as messages:
            self.assertTrue(self.run_sequence([EdgeProgress(0, 30, False),
                EdgeProgress(5, 30, False), EdgeProgress(30, 30, True)]))
        combined = "\n".join(messages.output)
        for counter in ("0/30", "5/30", "30/30"):
            self.assertIn(counter, combined)

    def test_requires_server_flag_after_minutes_target_and_focuses_once(self):
        self.assertTrue(self.run_sequence([EdgeProgress(0, 30, False), EdgeProgress(5, 30, False),
                                          EdgeProgress(30, 30, False), EdgeProgress(30, 30, True)]))
        summary = self.report["edge_browsing"]
        self.assertEqual((summary["minutes_before"], summary["minutes_after"]), (0, 30))
        self.assertEqual(summary["waited_seconds"], 180)
        self.assertFalse(summary["completed_before"])
        self.assertTrue(summary["completed_after"])
        self.assertEqual(len(self.context.pages), 2)
        self.assertEqual(self.context.pages[1].foreground_calls, 1)
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_stagnation_runs_to_sixty_minute_bound_without_false_completion(self):
        self.assertFalse(self.run_sequence([EdgeProgress(5, 30, False)]))
        summary = self.report["edge_browsing"]
        self.assertEqual(summary["status"], "timeout")
        self.assertEqual(summary["waited_seconds"], 3600)
        self.assertEqual(summary["minutes_after"], 5)
        self.assertFalse(summary["completed_after"])
        self.assertEqual(self.context.pages[1].foreground_calls, 1)
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_missing_or_disabled_feature_is_unavailable(self):
        for progress in [None, EdgeProgress(0, 30, False, False)]:
            with self.subTest(progress=progress):
                self.context = FakeContext()
                self.assertFalse(self.run_sequence([progress]))
                self.assertEqual(self.report["edge_browsing"]["status"], "unavailable")
                self.assertEqual(len(self.context.pages), 1)

    def test_missing_task_does_not_report_unobserved_completion_as_false(self):
        self.assertFalse(self.run_sequence([None]))
        summary = self.report["edge_browsing"]
        self.assertIsNone(summary["completed_before"])
        self.assertIsNone(summary["completed_after"])
        self.assertIsNone(summary["minutes_before"])
        self.assertIsNone(summary["minutes_after"])

    def test_date_change_stops_and_closes_only_owned_pages(self):
        def changed_day():
            return self.day if self.clock.seconds == 0 else self.day + timedelta(days=1)

        with self.assertRaises(EdgeInterfaceChanged):
            self.run_sequence([EdgeProgress(5, 30, False)], today=changed_day)
        self.assertEqual(self.report["edge_browsing"]["stopped_reason"], "date_changed")
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_cancel_preserves_last_server_progress_and_reraises(self):
        def cancel(seconds):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_sequence([EdgeProgress(5, 30, False)], wait=cancel)
        summary = self.report["edge_browsing"]
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["minutes_after"], 5)
        self.assertFalse(summary["completed_after"])
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_auth_check_exception_propagates_and_owned_pages_close(self):
        class AuthenticationRequired(RuntimeError):
            pass

        def auth(page):
            if page.label == "browsing":
                raise AuthenticationRequired

        with self.assertRaises(AuthenticationRequired):
            self.run_sequence([EdgeProgress(5, 30, False)], check_page=auth)
        self.assertTrue(all(page.closed for page in self.context.pages))

    def test_default_wait_pumps_playwright_instead_of_sleeping(self):
        self.config["edge_poll_seconds"] = 1
        def reader():
            if len(self.context.pages) == 2 and self.context.pages[1].waited_ms:
                self.clock.seconds = 1
                return EdgeProgress(30, 30, True)
            return EdgeProgress(0, 30, False)

        self.assertTrue(run_edge_browsing(self.context, self.config, self.report, self.day,
            check_page=lambda page: None, progress_reader=reader, clock=self.clock, today=lambda: self.day))
        self.assertEqual(self.context.pages[1].waited_ms, [1000])


class EdgeReaderBrowserTests(unittest.TestCase):
    def test_delayed_flight_data_is_read_and_only_owned_observer_closes(self):
        from playwright.sync_api import sync_playwright
        payload = [1, "a:" + json.dumps({"model": {"streak": edge_item(
            activitiesCompleted=30, isCurrentDayCompleted=True)}}) + "\n"]
        requests = []
        checks = []

        def fixture(route):
            requests.append(route.request.url)
            if route.request.url == "https://rewards.bing.com/earn":
                route.fulfill(content_type="text/html; charset=utf-8", body="""
                    <!doctype html><meta charset="utf-8"><main>Fixture</main>
                    <script>setTimeout(() => {
                        const data = """ + json.dumps(payload) + """;
                        const script = document.createElement('script');
                        script.type = 'application/json';
                        script.textContent = 'self.' + '__next_f.push(' + JSON.stringify(data) + ');';
                        document.body.appendChild(script);
                    }, 300);</script>
                """)
            else:
                route.abort()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context()
            context.route("**/*", fixture)
            try:
                original = context.new_page()
                report = {}
                self.assertTrue(run_edge_browsing(context,
                    dict(edge_max_wait_minutes=60, edge_poll_seconds=60, timeout_ms=5000),
                    report, date.today(), check_page=lambda page: checks.append(page)))
                self.assertEqual(context.pages, [original])
                self.assertTrue(report["edge_browsing"]["completed_before"])
                self.assertEqual(requests.count("https://rewards.bing.com/earn"), 1)
                self.assertTrue(checks)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
