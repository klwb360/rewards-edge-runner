from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import rewards


class FakeSearchBox:
    def __init__(self, page):
        self.page = page
        self.term = ''

    def fill(self, term, **kwargs):
        self.term = term

    def press(self, key, **kwargs):
        if key != 'Enter':
            raise AssertionError('Only real search form submissions are expected')
        self.page.urls.append('https://www.bing.com/search?' + urlencode({'q': self.term}))


class FakeSearchPage:
    def __init__(self):
        self.urls = []
        self.visits = self.urls
        self.navigations = []
        self.closed = False
        self.search_box = FakeSearchBox(self)

    def goto(self, url, **kwargs):
        self.navigations.append(url)
        if '/search?' in url:
            self.urls.append(url)
        return SimpleNamespace(status=200)

    def locator(self, selector):
        if selector != '#sb_form_q':
            raise AssertionError('Unexpected selector: ' + selector)
        return self.search_box

    def wait_for_load_state(self, state, **kwargs):
        pass

    def wait_for_timeout(self, milliseconds):
        pass

    def close(self):
        self.closed = True


class FakeSearchContext:
    def __init__(self):
        self.page = FakeSearchPage()
        self.created_pages = 0

    def new_page(self):
        self.created_pages += 1
        return self.page


def progress(current, maximum=60, today_points=None):
    return SimpleNamespace(current=current, maximum=maximum, today_points=today_points)


@contextmanager
def search_fixture(count=10):
    with tempfile.TemporaryDirectory(prefix='rewards-progress-test-') as directory:
        root = Path(directory)
        terms = root / 'terms.txt'
        terms.write_text('\n'.join(f'Topic {index}' for index in range(12)), encoding='utf-8')
        fixture = SimpleNamespace(
            context=FakeSearchContext(), report={}, day=date.today(), state=root / 'state',
            config=rewards.DEFAULTS | {'search_count': count, 'search_terms_file': 'terms.txt',
                                       'search_interval_seconds': 0, 'timeout_ms': 5000})
        with patch.object(rewards, 'ROOT', root), patch.object(rewards, 'STATE', fixture.state), \
                patch.object(rewards, 'check_page', return_value=None):
            yield fixture


class SearchProgressControlTests(unittest.TestCase):
    def test_already_at_cap_records_quota_without_opening_a_search_page(self):
        reader = Mock(return_value=progress(60, today_points=135))
        with search_fixture() as fixture:
            rewards.run_searches(fixture.context, fixture.config, fixture.report, fixture.day,
                                 progress_reader=reader)
            summary = fixture.report['searches']
            self.assertEqual(fixture.context.created_pages, 0)
            self.assertEqual(summary['opened'], 0)
            self.assertEqual(summary['stopped_reason'], 'limit_reached')
            self.assertEqual(summary['quota_before'], {'current': 60, 'maximum': 60, 'today_points': 135})
            self.assertEqual(summary['quota_after'], summary['quota_before'])
            self.assertEqual(summary['observed_points_gained'], 0)
            self.assertEqual(summary['credit'], 'unverified')
            self.assertFalse((fixture.state / 'search-history.json').exists())
            reader.assert_called_once_with()

    def test_stops_after_two_queries_reach_cap_even_when_ten_were_requested(self):
        reader = Mock(side_effect=[progress(54, today_points=100), progress(57, today_points=103),
                                   progress(60, today_points=106)])
        with search_fixture(count=10) as fixture:
            rewards.run_searches(fixture.context, fixture.config, fixture.report, fixture.day,
                                 progress_reader=reader)
            summary = fixture.report['searches']
            history = json.loads((fixture.state / 'search-history.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['requested'], 10)
            self.assertEqual(len(fixture.context.page.visits), 2)
            self.assertEqual(fixture.context.page.navigations, ['https://www.bing.com/'])
            self.assertEqual(summary['opened'], 2)
            self.assertEqual(len(history[fixture.day.isoformat()]), 2)
            self.assertEqual(summary['stopped_reason'], 'limit_reached')
            self.assertEqual(summary['quota_before'], {'current': 54, 'maximum': 60, 'today_points': 100})
            self.assertEqual(summary['quota_after'], {'current': 60, 'maximum': 60, 'today_points': 106})
            self.assertEqual(summary['observed_points_gained'], 6)
            self.assertEqual(summary['credit'], 'observed')
            self.assertEqual(reader.call_count, 3)
            self.assertTrue(fixture.context.page.closed)

    def test_two_queries_without_credit_stop_before_third_and_preserve_history(self):
        reader = Mock(side_effect=[progress(9), progress(9), progress(9)])
        with search_fixture() as fixture:
            with self.assertRaises(rewards.SearchCreditStopped):
                rewards.run_searches(fixture.context, fixture.config, fixture.report, fixture.day,
                                     progress_reader=reader)
            summary = fixture.report['searches']
            history = json.loads((fixture.state / 'search-history.json').read_text(encoding='utf-8'))
            self.assertEqual(len(fixture.context.page.visits), 2)
            self.assertEqual(len(history[fixture.day.isoformat()]), 2)
            self.assertEqual(summary['opened'], 2)
            self.assertEqual(summary['stopped_reason'], 'no_credit')
            self.assertEqual(summary['observed_points_gained'], 0)
            self.assertEqual(summary['credit'], 'unverified')
            self.assertEqual(summary['quota_after'], {'current': 9, 'maximum': 60, 'today_points': None})
            self.assertEqual(reader.call_count, 3)
            self.assertTrue(fixture.context.page.closed)

    def test_positive_increment_resets_no_credit_streak(self):
        reader = Mock(side_effect=[progress(9), progress(9), progress(12), progress(12), progress(12)])
        with search_fixture() as fixture:
            with self.assertRaises(rewards.SearchCreditStopped):
                rewards.run_searches(fixture.context, fixture.config, fixture.report, fixture.day,
                                     progress_reader=reader)
            summary = fixture.report['searches']
            self.assertEqual(len(fixture.context.page.visits), 4)
            self.assertEqual(summary['opened'], 4)
            self.assertEqual(summary['stopped_reason'], 'no_credit')
            self.assertEqual(summary['observed_points_gained'], 3)
            self.assertEqual(summary['credit'], 'observed')
            self.assertEqual(reader.call_count, 5)


if __name__ == '__main__':
    unittest.main()
