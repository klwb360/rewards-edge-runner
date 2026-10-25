"""Progress parsing and isolated browser checks; all HTTP requests stay local."""

from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import Mock

from rewards_progress import SearchProgress, parse_search_progress, read_search_progress


class ProgressParserTests(unittest.TestCase):
    def test_parses_counter_and_optional_today_points_with_whitespace(self):
        progress = parse_search_progress("积分明细\n今日积分\n28\n必应搜索\n18 / 60\n阅读资讯 0/30")
        self.assertEqual(progress, SearchProgress(current=18, maximum=60, today_points=28))

    def test_today_points_can_be_absent_and_zero_search_points_are_valid(self):
        self.assertEqual(parse_search_progress("必应搜索 0/60"), SearchProgress(0, 60, None))

    def test_missing_negative_disabled_and_impossible_counters_are_rejected(self):
        for text in ["阅读资讯 0/30", "必应搜索 -3/60", "必应搜索 3/-60", "必应搜索 61/60", "必应搜索 0/0", "今日积分 -3\n必应搜索 3/60"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_search_progress(text)

    def test_progress_value_is_frozen(self):
        progress = SearchProgress(9, 60, 19)
        with self.assertRaises(FrozenInstanceError):
            progress.current = 10


class ProgressReaderTests(unittest.TestCase):
    def test_new_page_is_closed_when_navigation_fails(self):
        context = Mock()
        page = context.new_page.return_value
        page.goto.side_effect = RuntimeError("fixture navigation failed")
        with self.assertRaises(RuntimeError):
            read_search_progress(context, timeout_ms=5000)
        page.close.assert_called_once_with()

    def test_real_edge_waits_for_button_hydration_and_async_dialog_counter(self):
        from playwright.sync_api import sync_playwright

        fixture_requests = []
        page_errors = []

        def intercept(route):
            fixture_requests.append(route.request.url)
            if route.request.url == "https://rewards.bing.com/earn":
                route.fulfill(content_type="text/html; charset=utf-8", body="""
                    <!doctype html>
                    <meta charset="utf-8">
                    <button id="today">今日积分</button>
                    <div role="dialog" style="display:none">积分明细：加载中</div>
                    <script>
                        setTimeout(() => {
                            const button = document.getElementById('today');
                            button.onclick = () => {
                                fetch('/fixture-click');
                                const dialog = document.querySelector('[role="dialog"]');
                                dialog.style.display = 'block';
                                setTimeout(() => {
                                    dialog.innerText = '积分明细\\n今日积分\\n28\\n必应搜索\\n18 / 60\\n阅读资讯 0/30';
                                }, 300);
                            };
                            button.__reactProps_fixture = {onClick: () => {}};
                        }, 300);
                    </script>
                """)
            elif route.request.url == "https://rewards.bing.com/fixture-click":
                route.fulfill(content_type="text/plain", body="ok")
            else:
                route.abort()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            context = browser.new_context()
            context.route("**/*", intercept)
            context.on("page", lambda page: page.on("pageerror", lambda error: page_errors.append(str(error))))
            try:
                original_page = context.new_page()
                progress = read_search_progress(context, timeout_ms=5000)
                self.assertEqual(progress, SearchProgress(18, 60, 28))
                self.assertEqual(context.pages, [original_page])
                self.assertIn("https://rewards.bing.com/fixture-click", fixture_requests)
                self.assertEqual(page_errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
