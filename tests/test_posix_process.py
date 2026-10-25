"""Real Linux process signals/locks; no browser or account is used."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import rewards


@unittest.skipUnless(sys.platform == 'linux', 'real POSIX process behavior')
class PosixProcessTests(unittest.TestCase):
    def wait_ready(self, process, path):
        deadline = time.monotonic() + 10
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), 'fixture child did not become ready')

    def stop_child(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def test_process_lock_rejects_overlap_and_releases_after_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lock, ready = root / 'run.lock', root / 'ready'
            code = """import pathlib,sys,time
import rewards
root=pathlib.Path(sys.argv[1])
with rewards.run_lock(root/'run.lock'):
 (root/'ready').write_text('ready')
 time.sleep(60)
"""
            process = subprocess.Popen([sys.executable, '-c', code, str(root)], cwd=rewards.ROOT,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.wait_ready(process, ready)
                with self.assertRaises(rewards.AlreadyRunning):
                    with rewards.run_lock(lock):
                        self.fail('overlapping process acquired the lock')
                process.kill()
                process.communicate(timeout=10)
                with rewards.run_lock(lock):
                    pass
            finally:
                self.stop_child(process)

    def test_real_sigterm_preserves_report_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code = """import pathlib,sys,time
import rewards
root=pathlib.Path(sys.argv[1])
rewards.STATE=root/'state'; rewards.LOGS=root/'logs'
config=root/'config.json';config.write_text('{"timezone":"Asia/Shanghai"}')
def fixture(command,config,report):
 report['edge_browsing']={'minutes_before':5,'minutes_after':10,'target_minutes':30,'status':'waiting'}
 (root/'ready').write_text('ready')
 time.sleep(60)
rewards.run_browser=fixture
raise SystemExit(rewards.main(['run','--config',str(config)]))
"""
            process = subprocess.Popen([sys.executable, '-c', code, str(root)], cwd=rewards.ROOT,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.wait_ready(process, root / 'ready')
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 143, (output, error))
                path = next((root / 'logs').glob('*.json'))
                report = json.loads(path.read_text())
                self.assertEqual((report['status'], report['signal']), ('interrupted', 'SIGTERM'))
                self.assertEqual(report['edge_browsing']['minutes_after'], 10)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual((root / 'logs').stat().st_mode & 0o777, 0o700)
                with rewards.run_lock(root / 'state/run.lock'):
                    pass
            finally:
                self.stop_child(process)
