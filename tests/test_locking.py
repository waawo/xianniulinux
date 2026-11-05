from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from xianniu_linux.__main__ import main
from xianniu_linux.locking import AccountLock


class AccountLockTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'account.lock'

    def child_acquire(self):
        return subprocess.run([sys.executable, '-c',
                               'import sys; from xianniu_linux.locking import AccountLock; '
                               'lock = AccountLock(sys.argv[1]); lock.__enter__()', str(self.path)],
                              capture_output=True, text=True, timeout=10)

    def test_other_process_rejected_until_owner_releases(self):
        with AccountLock(self.path):
            result = self.child_acquire()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Another account operation', result.stderr)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.child_acquire().returncode, 0)

    def test_crashed_owner_does_not_leave_a_stale_lock(self):
        child = subprocess.Popen([sys.executable, '-c',
                                  'import sys,time; from xianniu_linux.locking import AccountLock; '
                                  'lock=AccountLock(sys.argv[1]); lock.__enter__(); '
                                  'print("locked",flush=True); time.sleep(30)', str(self.path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(self.child_acquire().returncode, 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_force_stop_cannot_interrupt_another_managed_session(self):
        import json
        account = Path(self.folder.name) / 'copied-account.json'
        account.write_text(json.dumps({'base_url': 'https://api1.xianniu.com',
                                       'access_token': 'synthetic', 'game_id': 159}))
        with AccountLock(self.path), patch('xianniu_linux.locking.lock_path', return_value=self.path), \
                patch('sys.argv', ['xianniu', 'stop', '--account', str(account)]), \
                patch('xianniu_linux.__main__.Acceleration') as manager:
            self.assertEqual(main(), 1)
            manager.return_value.stop.assert_not_called()

    def test_release_even_after_exception(self):
        with self.assertRaises(RuntimeError):
            with AccountLock(self.path):
                raise RuntimeError('synthetic failure')
        self.assertEqual(self.child_acquire().returncode, 0)
