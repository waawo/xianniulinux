import asyncio
from argparse import Namespace
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import signal
import threading
import unittest
from unittest.mock import patch

from xianniu_linux.__main__ import serve, main
from xianniu_linux.protocol import Session
from xianniu_linux.api import ApiError
from xianniu_linux.locking import AccountLock


class ServeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_serve_refreshes_before_ready_and_stops_on_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            account = root / 'account.json'
            account.write_text(json.dumps({'base_url': 'https://api1.xianniu.com',
                                          'access_token': 'synthetic-token', 'game_id': 159}))
            session_path = root / 'session.json'
            events = []
            class Manager:
                active = clock_owned = False
                def __init__(self, account, path):
                    self.path = path
                def start(self):
                    self.active = self.clock_owned = True
                    self.path.write_text(json.dumps(asdict(Session('127.0.0.1', 9000, 'u', 'p'))))
                    events.append('start')
                    return {'mode': 'synthetic'}
                def refresh(self, stats):
                    events.append('refresh')
                def stop(self, stats):
                    self.active = self.clock_owned = False
                    events.append('stop-and-pause')
            args = Namespace(listen='127.0.0.1', port=0, max_connections=8, session=session_path,
                             account=account, ready_file=root / 'ready.json', refresh_interval=180,
                             stop_file=root / 'stop')
            with patch('xianniu_linux.__main__.Acceleration', Manager):
                task = asyncio.create_task(serve(args))
                try:
                    async with asyncio.timeout(3):
                        while not args.ready_file.exists():
                            await asyncio.sleep(0.01)
                    self.assertEqual(events, ['start', 'refresh'])
                    args.stop_file.touch()
                    await asyncio.wait_for(task, 3)
                    self.assertEqual(events, ['start', 'refresh', 'stop-and-pause'])
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)


class LoginStorageTests(unittest.TestCase):
    def test_login_does_not_persist_phone_password_or_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'account.json'
            arguments = ['xianniu', 'login', '--account', str(path), '--line-id', '1227', '--area-id', '1726']
            with patch('sys.argv', arguments), patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', return_value='13800000000'), \
                    patch('getpass.getpass', return_value='test-password'), \
                    patch('xianniu_linux.__main__.ApiClient') as factory:
                factory.return_value.login_password.return_value = {'access_token': 'synthetic-token'}
                self.assertEqual(main(), 0)
            stored = path.read_text()
            self.assertNotIn('test-password', stored)
            self.assertNotIn('13800000000', stored)
            self.assertNotIn('password', json.loads(stored))
            self.assertEqual(json.loads(stored)['line_id'], 1227)


class FailureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        account = self.root / 'account.json'
        account.write_text(json.dumps({'base_url': 'https://api1.xianniu.com',
                                       'access_token': 'synthetic', 'game_id': 159}))
        self.args = Namespace(listen='127.0.0.1', port=0, max_connections=8,
                              session=self.root/'session.json', account=account,
                              ready_file=self.root/'ready.json', status_file=self.root/'status.json',
                              refresh_interval=0.01, stop_file=self.root/'stop')
        self.events = []
        self.refresh_errors = []
        self.stop_error = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_refresh = False
        owner = self

        class Manager:
            active = clock_owned = False
            def __init__(self, account, path):
                self.path = path
            def start(self):
                self.active = self.clock_owned = True
                self.path.write_text(json.dumps(asdict(Session('127.0.0.1', 9000, 'u', 'p'))))
                owner.events.append('start')
                return {'mode': 'synthetic'}
            def refresh(self, stats):
                owner.events.append('refresh')
                if owner.events.count('refresh') > 1:
                    if owner.block_refresh:
                        owner.entered.set()
                        if not owner.release.wait(5):
                            raise RuntimeError('Test did not release refresh')
                        owner.events.append('refresh-finished')
                    if owner.refresh_errors:
                        error = owner.refresh_errors.pop(0)
                        if error:
                            raise error
            def stop(self, stats):
                owner.events.append('stop')
                if owner.stop_error:
                    raise owner.stop_error
                self.active = self.clock_owned = False

        self.manager = Manager
        self.lock_path = self.root/'account.lock'
        self.lock_patch = patch('xianniu_linux.locking.lock_path', return_value=self.lock_path)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)

    async def test_unexpected_refresh_errors_stop_and_pause(self):
        for error in (ValueError('secret-value'), OSError('secret-path'), TypeError('secret-type')):
            with self.subTest(error=type(error).__name__):
                self.events.clear()
                self.refresh_errors = [error]
                with patch('xianniu_linux.__main__.Acceleration', self.manager):
                    with self.assertRaises(ApiError) as caught:
                        await asyncio.wait_for(serve(self.args), 3)
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(self.events, ['start', 'refresh', 'refresh', 'stop'])
                self.assertTrue(json.loads(self.args.status_file.read_text())['stop_confirmed'])
                with AccountLock(self.lock_path):
                    pass

    async def test_two_api_failures_exit_with_error_after_cleanup(self):
        self.refresh_errors = [ApiError('offline'), ApiError('offline')]
        with patch('xianniu_linux.__main__.Acceleration', self.manager):
            with self.assertRaisesRegex(ApiError, 'failed twice'):
                await asyncio.wait_for(serve(self.args), 3)
        self.assertEqual(self.events.count('refresh'), 3)
        self.assertEqual(self.events[-1], 'stop')

    async def test_single_api_failure_recovers(self):
        self.refresh_errors = [ApiError('offline'), None]
        with patch('xianniu_linux.__main__.Acceleration', self.manager):
            task = asyncio.create_task(serve(self.args))
            try:
                async with asyncio.timeout(3):
                    while self.events.count('refresh') < 4:
                        await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.args.stop_file.touch()
                await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_stop_reports_unconfirmed_and_restores_handler(self):
        self.stop_error = ApiError('offline')
        self.args.stop_file.touch()
        old_handler = signal.getsignal(signal.SIGTERM)
        with patch('xianniu_linux.__main__.Acceleration', self.manager):
            with self.assertRaises(ApiError):
                await asyncio.wait_for(serve(self.args), 3)
        self.assertFalse(json.loads(self.args.status_file.read_text())['stop_confirmed'])
        self.assertEqual(signal.getsignal(signal.SIGTERM), old_handler)
        with AccountLock(self.lock_path):
            pass

    async def test_cancel_waits_for_refresh_before_stop_and_unlock(self):
        self.block_refresh = True
        with patch('xianniu_linux.__main__.Acceleration', self.manager):
            task = asyncio.create_task(serve(self.args))
            try:
                async with asyncio.timeout(3):
                    while not self.entered.is_set():
                        await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertNotIn('stop', self.events)
                with self.assertRaises(ValueError):
                    with AccountLock(self.lock_path):
                        pass
                self.release.set()
                await asyncio.wait_for(task, 3)
                self.assertLess(self.events.index('refresh-finished'), self.events.index('stop'))
            finally:
                self.release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_copied_profiles_cannot_start_a_second_session(self):
        self.block_refresh = True
        with patch('xianniu_linux.__main__.Acceleration', self.manager):
            task = asyncio.create_task(serve(self.args))
            try:
                async with asyncio.timeout(3):
                    while not self.entered.is_set():
                        await asyncio.sleep(0.01)
                copied = self.root/'another-account.json'
                copied.write_bytes(self.args.account.read_bytes())
                second_args = Namespace(**vars(self.args))
                second_args.account = copied
                second_args.session = self.root/'another-session.json'
                with self.assertRaisesRegex(ValueError, 'Another account operation'):
                    await serve(second_args)
                self.assertEqual(self.events.count('start'), 1)
                self.assertFalse(second_args.session.exists())
            finally:
                self.release.set()
                self.args.stop_file.touch()
                await asyncio.wait_for(task, 3)
