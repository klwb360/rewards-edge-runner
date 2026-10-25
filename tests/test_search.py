"""Search input, daily history, and real Edge fixture checks (no live network)."""

import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import rewards


class SearchInputTests(unittest.TestCase):
    def test_terms_skip_blank_comments_and_casefold_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "terms.txt"
            path.write_text("\n  # comment\n  Weather Today  \nweather today\n网络协议\n\n", encoding="utf-8")
            self.assertEqual(rewards.read_search_terms(path), ["Weather Today", "网络协议"])

    def test_empty_oversized_and_long_term_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "terms.txt"
            for content in ["\n# comment\n", "#" + "x" * 65536, "x" * 201]:
                path.write_text(content, encoding="utf-8")
                with self.subTest(length=len(content)), self.assertRaises(ValueError):
                    rewards.read_search_terms(path)

    def test_two_hundred_character_term_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "terms.txt"
            path.write_text("x" * 200, encoding="utf-8")
            self.assertEqual(rewards.read_search_terms(path), ["x" * 200])

    def test_daily_selection_is_deterministic_unique_and_bounded(self):
        terms = ["alpha", "beta", "gamma"]
        first = rewards.select_search_terms(terms, date(2026, 10, 25), 2)
        self.assertEqual(first, rewards.select_search_terms(terms, date(2026, 10, 25), 2))
        self.assertEqual(len(first), 2)
        self.assertEqual(len(set(first)), 2)
        self.assertTrue(set(first).issubset(terms))
        self.assertCountEqual(rewards.select_search_terms(terms, date(2026, 10, 25), 20), terms)


class SearchBrowserTests(unittest.TestCase):
    def run_with_fixture(self, directory, terms, count, mode="normal", relative=False):
        from playwright.sync_api import sync_playwright

        root = Path(directory)
        terms_path = root / "fixture-terms.txt"
        terms_path.write_text("\n".join(terms), encoding="utf-8")
        state = root / "state"
        hits = []
        history_checks = []
        config = rewards.DEFAULTS | {
            "headless": True,
            "search_count": count,
            "search_interval_seconds": 0,
            "search_terms_file": "fixture-terms.txt" if relative else str(terms_path),
            "timeout_ms": 5000,
        }
        report = {}

        def intercept(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname == "www.bing.com" and parsed.path == "/search":
                term = parse_qs(parsed.query)["q"][0]
                hits.append(term)
                history = (state / "search-history.json").read_text(encoding="utf-8")
                # History must already exist before the request is allowed to load.
                history_checks.append((term, history))
                body = '<div id="b_captcha">Verify manually</div>' if mode == "captcha" else "<main>Fixture results</main>"
                route.fulfill(status=503 if mode == "http_error" else 200, content_type="text/html", body=body)
            else:
                route.abort()

        with patch.object(rewards, "ROOT", root), patch.object(rewards, "STATE", state):
            with sync_playwright() as playwright:
                context = playwright.chromium.launch(headless=True, channel="msedge").new_context()
                context.route("**/*", intercept)
                try:
                    rewards.run_searches(context, config, report, date(2026, 10, 25))
                finally:
                    context.browser.close()
        return report, hits, history_checks

    def test_encoded_queries_history_before_navigation_and_same_day_skip(self):
        terms = ["天气 上海 / 网络 & 安全?", "Python pathlib examples"]
        with tempfile.TemporaryDirectory(prefix="rewards-search-fixture-") as folder:
            report, hits, histories = self.run_with_fixture(folder, terms, 2, relative=True)
            self.assertCountEqual(hits, terms)
            self.assertEqual(report["searches"]["requested"], 2)
            self.assertEqual(report["searches"]["opened"], 2)
            self.assertEqual(report["searches"]["credit"], "unverified")
            for term, history in histories:
                self.assertIn(hashlib.sha256(term.casefold().encode("utf-8")).hexdigest(), history)
                self.assertNotIn(term, history)
                self.assertIn("2026-10-25", history)
                json.loads(history)
            again, repeated_hits, _ = self.run_with_fixture(folder, terms, 2)
            self.assertEqual(repeated_hits, [])
            self.assertEqual(again["searches"]["opened"], 0)
            self.assertEqual(again["searches"]["skipped_today"], 2)
            self.assertEqual(again["searches"]["credit"], "unverified")

    def test_captcha_stops_searches_and_preserves_attempt_history(self):
        terms = ["fixture first topic", "fixture second topic"]
        with tempfile.TemporaryDirectory(prefix="rewards-search-fixture-") as folder:
            with self.assertRaises(rewards.NeedsHuman):
                self.run_with_fixture(folder, terms, 2, mode="captcha")
            history = (Path(folder) / "state" / "search-history.json").read_text(encoding="utf-8")
            found = [hashlib.sha256(term.casefold().encode("utf-8")).hexdigest() in history for term in terms]
            self.assertEqual(sum(found), 1)

    def test_http_error_is_not_retried_on_same_day(self):
        terms = ["fixture error topic"]
        with tempfile.TemporaryDirectory(prefix="rewards-search-fixture-") as folder:
            with self.assertRaises(RuntimeError):
                self.run_with_fixture(folder, terms, 1, mode="http_error")
            report, hits, _ = self.run_with_fixture(folder, terms, 1)
            self.assertEqual(hits, [])
            self.assertEqual(report["searches"]["opened"], 0)
            self.assertEqual(report["searches"]["skipped_today"], 1)


if __name__ == "__main__":
    unittest.main()
