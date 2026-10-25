"""Real Edge checks with isolated profiles and fully intercepted fixture pages.

These checks exercise browser behavior, not the live Rewards service or point credit.
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import rewards


class BrowserFixture:
    def __init__(self, mode):
        self.mode = mode
        self.completed = {"first", "second", "third"} if mode == "modern-earn" else set()
        self.visited = []
        self.markers = 0
        self.requests = []

    def promotions(self):
        identifiers = (
            ["first", "second", "third"] if self.mode == "modern-earn"
            else ["first", "second"] if self.mode == "captcha" else ["first"]
        )
        return [
            {
                "offerId": identifier,
                "title": "Fixture " + identifier,
                "promotionType": "quiz" if self.mode == "quiz" else "urlreward",
                "destinationUrl": "https://www.bing.com/search?fixture=" + identifier,
                "complete": identifier in self.completed,
                "__source": "modern" if self.mode.startswith("modern-") else "legacy",
            }
            for identifier in identifiers
        ]

    def route(self, route):
        # Every request ends here: no route.continue_() or fallback to the internet.
        parsed = urlsplit(route.request.url)
        self.requests.append(route.request.url)
        dashboard_page = parsed.hostname == "rewards.microsoft.com" and parsed.path == "/"
        earn_page = parsed.hostname == "rewards.bing.com" and parsed.path == "/earn"
        if dashboard_page or earn_page:
            dashboard = {
                "dailySetPromotions": {
                    date.today().strftime("%m/%d/%Y"): self.promotions()
                },
                "activityPromotions": [],
            }
            if earn_page and self.mode == "modern-earn":
                dashboard["activityPromotions"] = [
                    {
                        "offerId": identifier,
                        "title": "Fixture " + identifier,
                        "promotionType": "urlreward",
                        "destinationUrl": "https://www.bing.com/search?fixture=" + identifier,
                        "complete": identifier in self.completed,
                        "__source": "modern",
                        "__page": "earn",
                        "points": 15,
                    }
                    for identifier in ["extra-one", "extra-two"]
                ]
            cards = (
                '<a href="https://www.bing.com/search?fixture=first" '
                'target="_blank" onclick="fetch(\'/mark-card\')">'
                '<h3>Fixture first</h3></a>'
                if self.mode == "modern-click" else ""
            )
            if self.mode == "modern-hydration":
                cards = """
                    <a id="hydrating-card" href="https://www.bing.com/search?fixture=first"
                       target="_blank"><h3>Fixture first</h3></a>
                    <script>
                        setTimeout(() => {
                            const card = document.getElementById('hydrating-card');
                            card.onclick = () => fetch('/mark-card');
                            card.__reactProps_fixture = {onClick: () => {}};
                        }, 1000);
                    </script>
                """
            if earn_page and self.mode == "modern-earn":
                cards = "".join(
                    '<a href="https://www.bing.com/search?fixture=' + identifier + '" '
                    'target="_blank" onclick="fetch(\'/mark-card?offer=' + identifier + '\')">'
                    '<h3>Fixture ' + identifier + '</h3></a>'
                    for identifier in ["extra-one", "extra-two"]
                )
            route.fulfill(
                content_type="text/html",
                body="<!doctype html><title>Fixture dashboard</title><script>var dashboard = "
                + json.dumps(dashboard)
                + ";</script><main>Rewards fixture</main>"
                + cards,
            )
        elif parsed.hostname in {"rewards.microsoft.com", "rewards.bing.com"} and parsed.path == "/mark-card":
            self.markers += 1
            self.completed.add(parse_qs(parsed.query).get("offer", ["first"])[0])
            route.fulfill(content_type="text/plain", body="ok")
        elif parsed.hostname == "www.bing.com" and parsed.path == "/search":
            identifier = parse_qs(parsed.query)["fixture"][0]
            self.visited.append(identifier)
            if self.mode == "visit":
                self.completed.add(identifier)
            if self.mode == "captcha":
                body = '<div id="b_captcha">Manual verification required</div>'
            elif self.mode == "quiz":
                body = """
                    <div id="rqQuestion">Fixture question</div>
                    <button id="rqAnswerOption0" onclick="
                        this.disabled = true;
                        fetch('/mark-complete').then(() => {
                            document.getElementById('rqQuestion').textContent = 'Complete';
                        });
                    ">Fixture answer</button>
                """
            else:
                body = "<main>Fixture visit activity</main>"
            route.fulfill(content_type="text/html", body="<!doctype html>" + body)
        elif parsed.hostname == "www.bing.com" and parsed.path == "/mark-complete":
            self.markers += 1
            self.completed.add("first")
            route.fulfill(content_type="text/plain", body="ok")
        else:
            route.abort()


class EdgeBrowserTests(unittest.TestCase):
    def execute_fixture(self, mode):
        fixture = BrowserFixture(mode)
        report = {"status": "started", "tasks": []}
        config = rewards.DEFAULTS | {
            "headless": True,
            "visit_seconds": 0,
            "step_seconds": 0,
            "max_quiz_steps": 5,
            "timeout_ms": 5000,
            "search_count": 0,
        }
        with tempfile.TemporaryDirectory(prefix="rewards-fixture-") as directory:
            profile_root = Path(directory) / "isolated-state"
            with patch.object(rewards, "STATE", profile_root), patch.object(
                rewards, "restrict_navigation", fixture.route
            ):
                exit_code = rewards.run_browser("run", config, report)
        return exit_code, report, fixture

    def test_visit_is_completed_only_after_dashboard_confirmation(self):
        exit_code, report, fixture = self.execute_fixture("visit")
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["tasks"][0]["status"], "completed")
        self.assertEqual(fixture.visited, ["first"])

    def test_opening_activity_without_confirmation_stays_unverified(self):
        exit_code, report, fixture = self.execute_fixture("unverified")
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["tasks"][0]["status"], "unverified")
        self.assertEqual(fixture.visited, ["first"])

    def test_quiz_option_click_updates_fixture_and_is_verified(self):
        exit_code, report, fixture = self.execute_fixture("quiz")
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["tasks"][0]["status"], "completed")
        self.assertEqual(fixture.markers, 1)
        self.assertEqual(fixture.visited, ["first"])

    def test_modern_card_click_records_activity_before_popup_is_verified(self):
        exit_code, report, fixture = self.execute_fixture("modern-click")
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["tasks"][0]["status"], "completed")
        # A direct visit cannot satisfy this fixture: only the card's click handler can.
        self.assertEqual(fixture.markers, 1)
        self.assertEqual(fixture.visited, ["first"])

    def test_modern_earn_page_pending_activities_join_completed_daily_set(self):
        exit_code, report, fixture = self.execute_fixture("modern-earn")
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "completed")
        tasks = {task["offer_id"]: task for task in report["tasks"]}
        self.assertEqual(len(tasks), 5)
        for identifier in ["first", "second", "third"]:
            self.assertEqual(tasks[identifier]["status"], "already_completed")
        for identifier in ["extra-one", "extra-two"]:
            self.assertEqual(tasks[identifier]["status"], "completed")
            self.assertEqual(tasks[identifier]["page"], "earn")
        self.assertEqual(fixture.markers, 2)
        self.assertCountEqual(fixture.visited, ["extra-one", "extra-two"])
        self.assertIn("https://rewards.bing.com/earn", fixture.requests)

    def test_modern_card_waits_for_delayed_hydration_before_clicking(self):
        exit_code, report, fixture = self.execute_fixture("modern-hydration")
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["tasks"][0]["status"], "completed")
        # Until the delayed handler is attached, clicking only opens the href.
        self.assertEqual(fixture.markers, 1)
        self.assertEqual(fixture.visited, ["first"])

    def test_captcha_stops_before_another_task_is_opened(self):
        fixture = BrowserFixture("captcha")
        report = {"status": "started", "tasks": []}
        config = rewards.DEFAULTS | {"headless": True, "visit_seconds": 0, "timeout_ms": 5000, "search_count": 0}
        with tempfile.TemporaryDirectory(prefix="rewards-fixture-") as directory:
            with patch.object(rewards, "STATE", Path(directory) / "isolated-state"), patch.object(
                rewards, "restrict_navigation", fixture.route
            ):
                with self.assertRaises(rewards.NeedsHuman):
                    rewards.run_browser("run", config, report)
        self.assertEqual(fixture.visited, ["first"])
        self.assertNotIn("second", fixture.completed)

    def test_navigation_guard_handles_first_popup_request_and_blocks_foreign_host(self):
        from playwright.sync_api import sync_playwright

        allowed_url = "https://www.bing.com/search?fixture=guard-popup"
        foreign_url = "https://blocked.invalid/activity"
        fixture_requests = []
        page_errors = []

        def intercept(route):
            # Registered below the real guard: every permitted request ends locally.
            fixture_requests.append(route.request.url)
            if route.request.url == rewards.DASHBOARD_URL:
                route.fulfill(
                    content_type="text/html",
                    body=(
                        '<a id="allowed" target="_blank" href="' + allowed_url + '">Allowed</a>'
                        '<a id="foreign" target="_blank" href="' + foreign_url + '">Foreign</a>'
                    ),
                )
            elif route.request.url == allowed_url:
                route.fulfill(content_type="text/html", body="<main>Allowed fixture</main>")
            else:
                route.abort()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            # Native target=_blank navigation needs no JS. Disable unrelated Edge
            # Bing integrations which expect live-page globals on synthetic HTML.
            context = browser.new_context(java_script_enabled=False)
            context.route("**/*", intercept)
            context.route("**/*", rewards.restrict_navigation)
            context.on("page", lambda item: item.on("pageerror", lambda error: page_errors.append(str(error))))
            try:
                page = context.new_page()
                page.goto(rewards.DASHBOARD_URL, wait_until="domcontentloaded")
                with page.expect_popup(timeout=5000) as popup_info:
                    page.locator("#allowed").click()
                popup = popup_info.value
                popup.wait_for_load_state("domcontentloaded", timeout=5000)
                self.assertEqual(popup.locator("main").inner_text(), "Allowed fixture")
                self.assertIn(allowed_url, fixture_requests)
                with context.expect_event("requestfailed", predicate=lambda request: request.url == foreign_url, timeout=5000):
                    with page.expect_popup(timeout=5000):
                        page.locator("#foreign").click()
                self.assertNotIn(foreign_url, fixture_requests)
                self.assertEqual(page_errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
