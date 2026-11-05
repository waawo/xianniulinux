import asyncio
from argparse import Namespace
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xianniu_linux.__main__ import serve, main
from xianniu_linux.protocol import Session


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
