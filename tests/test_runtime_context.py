import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import rewards
from runtime_context import configured_now, configured_today, schedule_start_boundary, validate_timezone


class ClockTests(unittest.TestCase):
    def test_configured_date_does_not_depend_on_host_timezone(self):
        instant = datetime(2026, 10, 25, 17, 30, tzinfo=timezone.utc)
        config = {'timezone': 'Asia/Shanghai'}
        self.assertEqual(configured_now(config, instant).isoformat(), '2026-10-26T01:30:00+08:00')
        self.assertEqual(configured_today(config, instant).isoformat(), '2026-10-26')
        self.assertEqual(configured_now({}, instant), instant.astimezone())

    def test_config_validates_zone_and_preserves_legacy_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text('{}', encoding='utf-8')
            self.assertIsNone(rewards.load_config(path)['timezone'])
            for value in ['Asia/Shanghai', 'UTC']:
                validate_timezone(value)
            for value in ['', True, 123, 'Does/NotExist', '/etc/passwd']:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    validate_timezone(value)

    def test_scheduler_returns_offset_boundary_and_legacy_none(self):
        instant = datetime(2026, 10, 25, 17, 30, tzinfo=timezone.utc)
        self.assertEqual(schedule_start_boundary({'timezone': 'Asia/Shanghai'}, '20:00', instant),
                         '2026-10-26T20:00:00+08:00')
        self.assertIsNone(schedule_start_boundary({}, '20:00', instant))
        with self.assertRaises(ValueError):
            schedule_start_boundary({'timezone': 'America/New_York'}, '20:00', instant)
        with self.assertRaises(ValueError):
            schedule_start_boundary({'timezone': 'UTC'}, '25:00', instant)

    def test_scheduler_cli_emits_only_json_with_space_in_config_path(self):
        with tempfile.TemporaryDirectory(prefix='rewards clock ') as folder:
            path = Path(folder) / 'config file.json'
            path.write_text('{"timezone":"Asia/Shanghai"}', encoding='utf-8')
            result = subprocess.run([sys.executable, str(rewards.ROOT / 'runtime_context.py'),
                '--config', str(path), '--at', '20:00'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data['timezone'], 'Asia/Shanghai')
            self.assertTrue(data['start_boundary'].endswith('T20:00:00+08:00'))

    def test_report_clock_cli_converts_original_instant_and_current_day(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text('{"timezone":"Asia/Shanghai"}', encoding='utf-8')
            result = subprocess.run([sys.executable, str(rewards.ROOT / 'runtime_context.py'),
                '--config', str(path), '--report-start', '2026-10-25T20:00:00+00:00'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data['started_at'], '2026-10-26T04:00:00+08:00')
            self.assertTrue(data['now'].endswith('+08:00'))


class CoreRuntimeTests(unittest.TestCase):
    def test_cleanup_error_does_not_replace_primary_interrupt(self):
        resource = types.SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError('closed')))
        with self.assertRaises(rewards.Terminated):
            try:
                raise rewards.Terminated()
            finally:
                rewards.close_preserving_error(resource)
        with self.assertRaises(RuntimeError):
            rewards.close_preserving_error(resource)

    def test_search_stops_at_configured_midnight_before_next_submission(self):
        day = datetime(2026, 10, 25).date()
        page = types.SimpleNamespace(goto=lambda *a, **kw: types.SimpleNamespace(status=200),
            locator=lambda *a: types.SimpleNamespace(first=types.SimpleNamespace(is_visible=lambda: False)),
            url='https://www.bing.com/', wait_for_timeout=lambda *a: None, close=lambda: None)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'terms.txt').write_text('one\ntwo', encoding='utf-8')
            config = rewards.DEFAULTS | {'search_count': 2, 'search_terms_file': str(root / 'terms.txt'),
                'timezone': 'Asia/Shanghai'}
            report = {}
            with patch.object(rewards, 'STATE', root / 'state'), \
                 patch.object(rewards, 'configured_today', side_effect=[day, day + timedelta(days=1)]):
                with self.assertRaises(rewards.InterfaceChanged):
                    rewards.run_searches(types.SimpleNamespace(new_page=lambda: page), config, report, day)
            history = json.loads((root / 'state/search-history.json').read_text())
            self.assertEqual(len(history[day.isoformat()]), 1)
            self.assertEqual(report['searches']['opened'], 1)

    def test_browser_options_force_chinese_and_keep_login_headed(self):
        config = rewards.DEFAULTS | {'headless': True}
        self.assertEqual(rewards.browser_options('run', config)['locale'], 'zh-CN')
        self.assertFalse(rewards.browser_options('login', config)['headless'])

    def test_edge_uses_configured_day_callback(self):
        import edge_browsing
        config = rewards.DEFAULTS | {'timezone': 'Asia/Shanghai'}
        expected = datetime(2026, 10, 26).date()
        with patch.object(rewards, 'configured_today', return_value=expected), \
             patch.object(edge_browsing, 'run_edge_browsing', return_value=True) as runner:
            self.assertTrue(rewards.run_edge_phase(object(), config, {}, expected))
            self.assertEqual(runner.call_args.kwargs['today'](), expected)

    def test_sigterm_saves_progress_returns_143_and_restores_handler(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config.json'
            config.write_text('{"timezone":"Asia/Shanghai"}', encoding='utf-8')
            previous = signal.getsignal(signal.SIGTERM)
            def terminate(command, supplied, report):
                report['edge_browsing'] = {'minutes_before': 5, 'minutes_after': 10,
                    'target_minutes': 30, 'status': 'waiting'}
                signal.raise_signal(signal.SIGTERM)
            with patch.object(rewards, 'STATE', root / 'state'), patch.object(rewards, 'LOGS', root / 'logs'), \
                 patch.object(rewards, 'run_browser', terminate), \
                 patch.object(rewards, 'RotatingFileHandler', return_value=logging.NullHandler()), \
                 patch.object(rewards.logging, 'basicConfig'):
                self.assertEqual(rewards.main(['run', '--config', str(config)]), 143)
            saved = json.loads(next((root / 'logs').glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual((saved['status'], saved['exit_code']), ('interrupted', 143))
            self.assertEqual(saved['edge_browsing']['minutes_after'], 10)
            self.assertEqual(saved['edge_browsing']['status'], 'interrupted')
            self.assertTrue(saved['finished_at'].endswith('+08:00'))
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


if __name__ == '__main__':
    unittest.main()
