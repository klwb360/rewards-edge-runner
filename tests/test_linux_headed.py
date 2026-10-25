"""Opt-in real Linux runtime checks, with every browser request fulfilled locally.

Run as a non-root user with desktop dependencies and Edge installed:
REWARDS_LINUX_RUNTIME_TESTS=1 python -m unittest discover -s tests -p test_linux_headed.py -v
"""

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import linux_desktop


RUNTIME_ENABLED = sys.platform.startswith("linux") and os.environ.get("REWARDS_LINUX_RUNTIME_TESTS") == "1"


@unittest.skipUnless(RUNTIME_ENABLED, "requires opt-in non-root Linux desktop/Edge runtime")
class LinuxHeadedRuntimeTests(unittest.TestCase):
    def test_first_login_password_creation_works_on_pty_without_echo(self):
        import pty
        import select
        import termios

        with tempfile.TemporaryDirectory(prefix="linux-first-login-") as folder:
            state = Path(folder) / "state"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            code = (
                "from pathlib import Path; from linux_desktop import virtual_desktop; "
                f"state=Path({str(state)!r}); "
                f"\nwith virtual_desktop(state, vnc=True, vnc_port={port}):\n print('FIRST_LOGIN_READY', flush=True)\n"
            )
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            transcript = b""
            responses = [(b"Enter VNC password:", b"vnc9724!\n"),
                         (b"Verify password:", b"vnc9724!\n"), (b"[y]/n", b"y\n")]
            sent = 0
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        try:
                            block = os.read(master, 4096)
                        except OSError:
                            break  # Linux PTY returns EIO after its last slave closes.
                        if not block:
                            break
                        transcript += block
                    if sent < len(responses) and responses[sent][0] in transcript:
                        # x11vnc prints its prompt before disabling terminal echo.
                        if sent >= 2 or not termios.tcgetattr(master)[3] & termios.ECHO:
                            os.write(master, responses[sent][1])
                            sent += 1
                    if process.poll() is not None and not readable:
                        break
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0, transcript.decode(errors="replace"))
                self.assertEqual(sent, 3)
                self.assertIn(b"FIRST_LOGIN_READY", transcript)
                self.assertNotIn(b"vnc9724!", transcript)
                self.assertEqual((state / "vnc.pass").stat().st_mode & 0o777, 0o600)
                self.assertFalse(any(state.glob("desktop-*")))
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                os.close(master)

    def test_real_vnc_is_loopback_only_and_requires_password_authentication(self):
        with tempfile.TemporaryDirectory(prefix="linux-vnc-fixture-") as folder:
            state = Path(folder) / "state"
            state.mkdir(mode=0o700)
            password = state / "vnc.pass"
            # Public fixture password only; no user credential is supplied.
            subprocess.run(["x11vnc", "-norc", "-storepasswd", "fixture-only", str(password)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with linux_desktop.virtual_desktop(state, vnc=True, vnc_port=port):
                listeners = []
                ipv6_listeners = []
                for name, result in (("tcp", listeners), ("tcp6", ipv6_listeners)):
                    for line in Path("/proc/net/" + name).read_text().splitlines()[1:]:
                        fields = line.split()
                        address, local_port = fields[1].split(":")
                        if fields[3] == "0A" and int(local_port, 16) == port:
                            result.append(address)
                self.assertEqual(listeners, ["0100007F"])
                self.assertEqual(ipv6_listeners, [])
                with socket.create_connection(("127.0.0.1", port), timeout=3) as viewer, viewer.makefile("rb") as stream:
                    version = stream.read(12)
                    self.assertEqual(version, b"RFB 003.008\n")
                    viewer.sendall(version)
                    count = stream.read(1)[0]
                    security = stream.read(count)
                    self.assertEqual(len(security), count)
                    self.assertIn(2, security)
                    self.assertNotIn(1, security)
            self.assertFalse(any(state.glob("desktop-*")))

    def test_real_headed_edge_uses_private_desktop_and_cleans_process_groups(self):
        from playwright.sync_api import sync_playwright
        import rewards

        owned = []
        launch = linux_desktop._launch

        def capture(argv, **kwargs):
            process = launch(argv, **kwargs)
            if Path(argv[0]).name in {"Xvfb", "openbox"}:
                owned.append(process)
            return process

        original = {key: os.environ.get(key) for key in ("DISPLAY", "XAUTHORITY")}
        with tempfile.TemporaryDirectory(prefix="linux-headed-fixture-") as folder:
            state = Path(folder) / "state"
            with patch.object(linux_desktop, "_launch", side_effect=capture):
                with linux_desktop.virtual_desktop(state):
                    self.assertEqual(len(owned), 2)
                    self.assertTrue(all(os.getsid(process.pid) == process.pid for process in owned))
                    self.assertEqual(Path(os.environ["XAUTHORITY"]).stat().st_mode & 0o777, 0o600)
                    with sync_playwright() as playwright:
                        options = rewards.browser_options("run", rewards.DEFAULTS | {"headless": False, "channel": "msedge"})
                        browser = playwright.chromium.launch_persistent_context(
                            str(state / "fixture-profile"), service_workers="block", **options,
                        )
                        try:
                            browser.route("**/*", lambda route: route.fulfill(
                                content_type="text/html", body='<!doctype html><title>Linux fixture</title><main id="fixture">Owned desktop ready</main>'))
                            page = browser.pages[0]
                            page.goto("https://fixture.invalid/", wait_until="domcontentloaded")
                            self.assertEqual(page.locator("#fixture").inner_text(), "Owned desktop ready")
                            self.assertEqual(page.title(), "Linux fixture")
                            self.assertEqual(page.evaluate("navigator.language"), "zh-CN")
                            page.screenshot(path=str(state / "headed-fixture.png"))
                        finally:
                            browser.close()
            self.assertFalse(any(state.glob("desktop-*")))
        for process in owned:
            self.assertIsNotNone(process.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        self.assertEqual({key: os.environ.get(key) for key in original}, original)

    def test_sigterm_during_owned_desktop_cleans_up_and_returns_143(self):
        with tempfile.TemporaryDirectory(prefix="linux-signal-fixture-") as folder:
            state = Path(folder) / "state"
            ready = Path(folder) / "ready"
            code = (
                "import pathlib,time; from linux_desktop import virtual_desktop; "
                f"state=pathlib.Path({str(state)!r}); ready=pathlib.Path({str(ready)!r}); "
                "\nwith virtual_desktop(state):\n ready.write_text('ready'); time.sleep(60)\n"
            )
            process = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "owned desktop did not become ready")
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 143, (output, error))
                self.assertEqual(list(state.iterdir()), [])
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
