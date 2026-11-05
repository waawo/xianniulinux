"""Launcher lifecycle tests: fake OS boundary, real configuration and cleanup code."""
from argparse import Namespace
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from xianniu_linux import netns
from xianniu_linux.protocol import Session


class LauncherLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.runtime = self.root/'runtime'
        self.runtime.mkdir()
        self.account = self.root/'account.json'
        self.account.write_text(json.dumps({'base_url': 'https://api1.xianniu.com',
                                           'access_token': 'synthetic', 'game_id': 159}))
        self.session = self.root/'session.json'
        self.session.write_text(json.dumps(asdict(Session('127.0.0.1', 9000, 'u', 'p'))))
        self.args = Namespace(application=['--', '/games/Proton/proton', 'waitforexitandrun', '/games/Warframe/Launcher.exe'], hev='hev-socks5-tunnel',
                              session=self.session, account=self.account)
        self.commands = []
        self.launches = []
        self.processes = []
        self.kills = []
        self.handlers = {}
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stop_confirmed = True
        self.proxy_exit = 0
        self.proxy_timeout = False
        self.no_status = False
        self.fail_launch = None
        self.fail_command = None
        self.game_exit = 0
        self.crash_proxy = False
        self.crash_tunnel = False
        self.send_signal = False
        self.clock = 0
        self.no_ready = False
        self.no_tun = False
        self.namespace_root = self.root/'netns'

    def command(self, args, **kwargs):
        args = list(map(str, args))
        self.commands.append(args)
        if self.fail_command and self.fail_command(args):
            if kwargs.get('check'):
                raise subprocess.CalledProcessError(1, args)
            return SimpleNamespace(returncode=1, stdout='', stderr='synthetic failure')
        code = 1 if args[0] == 'pgrep' or (self.no_tun and args[-3:] == ['link', 'show', 'xntun']) else 0
        return SimpleNamespace(returncode=code,
                               stdout='411\n412\n' if args[1:3] == ['netns', 'pids'] else '', stderr='')

    def popen(self, args, **kwargs):
        role = ('proxy', 'tunnel', 'game')[len(self.processes)]
        self.launches.append((role, args, kwargs))
        if self.fail_launch == role:
            raise OSError('synthetic launch failure')
        owner = self

        class Process:
            returncode = None
            killed = False
            def poll(self):
                if self.returncode is None:
                    if role == 'game':
                        self.returncode = owner.game_exit
                    elif (role == 'proxy' and owner.crash_proxy) or (role == 'tunnel' and owner.crash_tunnel):
                        self.returncode = 1
                return self.returncode
            def terminate(self):
                pass
            def wait(self, timeout):
                if role == 'proxy' and owner.proxy_timeout and not self.killed:
                    raise subprocess.TimeoutExpired(args, timeout)
                if self.returncode is None:
                    self.returncode = owner.proxy_exit if role == 'proxy' else 0
                if role == 'proxy' and not owner.no_status and not self.killed:
                    (owner.runtime/'status.json').write_text(json.dumps({
                        'account_managed': True, 'stop_confirmed': owner.stop_confirmed}))
                return self.returncode
            def kill(self):
                self.killed = True
                self.returncode = -9

        process = Process()
        self.processes.append(process)
        if role == 'proxy' and not self.no_ready:
            (self.runtime/'ready.json').write_text('{"port": 1080}')
        return process

    def sleep(self, seconds):
        self.clock += 61 if (self.no_ready or self.no_tun) else seconds
        if self.send_signal:
            self.handlers[signal.SIGTERM](signal.SIGTERM, None)

    def run_launcher(self):
        user = SimpleNamespace(pw_name='desktop-user', pw_gid=1000, pw_dir=str(self.root))
        def set_signal(sig, handler):
            previous = self.handlers.get(sig, signal.SIG_DFL)
            self.handlers[sig] = handler
            return previous
        with ExitStack() as stack:
            replacements = {
                'sys.platform': 'linux', 'sys.modules': {'pwd': SimpleNamespace(getpwuid=lambda uid: user)},
                'os.geteuid': lambda: 0, 'os.getgrouplist': lambda name, gid: [1000, 1001],
                'os.chown': lambda *args: None,
                'os.kill': lambda pid, sig: self.kills.append((pid, sig)),
                'shutil.which': lambda name: name,
                'subprocess.check_output': lambda *args, **kwargs: b'[{"dst":"default"}]',
                'subprocess.run': self.command, 'subprocess.Popen': self.popen,
                'tempfile.mkdtemp': lambda **kwargs: str(self.runtime),
                'signal.signal': set_signal, 'signal.SIGKILL': 9, 'time.sleep': self.sleep,
                'time.monotonic': lambda: self.clock,
                'NETNS_ROOT': self.namespace_root,
            }
            for name, value in replacements.items():
                if name == 'sys.modules':
                    stack.enter_context(patch.dict('sys.modules', value))
                else:
                    stack.enter_context(patch('xianniu_linux.netns.'+name, value, create=True))
            stack.enter_context(patch.dict(os.environ, {'SUDO_UID': '1000'}))
            stack.enter_context(redirect_stdout(self.stdout))
            stack.enter_context(redirect_stderr(self.stderr))
            return netns.run(self.args)

    def assert_cleaned(self):
        self.assertTrue(any(cmd[1:3] == ['netns', 'delete'] for cmd in self.commands))
        self.assertFalse(list(self.namespace_root.glob('*/resolv.conf')))
        self.assertEqual(self.handlers[signal.SIGTERM], signal.SIG_DFL)
        self.assertEqual(self.handlers[signal.SIGINT], signal.SIG_DFL)
        self.assertTrue(all(pid in (411, 412) for pid, sig in self.kills))

    def test_normal_exit_confirms_stop_and_drops_game_privileges(self):
        self.assertEqual(self.run_launcher(), 0)
        self.assert_cleaned()
        self.assertIn('Server confirmed', self.stdout.getvalue())
        proxy = self.launches[0]
        self.assertEqual(proxy[2]['user'], 1000)
        self.assertEqual(proxy[2]['extra_groups'], [1000, 1001])
        game_args = self.launches[2][1]
        self.assertIn('runuser', game_args)
        self.assertEqual(game_args[game_args.index('-u')+1], 'desktop-user')
        for cmd in self.commands:
            if 'route' in cmd:
                self.assertEqual(cmd[1], '-n')

    def test_steam_client_cannot_be_launched_in_tunnel(self):
        self.args.application = ['--', 'steam']
        with self.assertRaisesRegex(ValueError, 'all of Steam'):
            self.run_launcher()
        self.assertEqual(self.launches, [])
        self.assertEqual(self.commands, [])

    def test_game_environment_only_applied_after_privilege_drop(self):
        self.args.game_environment = {'SteamAppId': '230410', 'LD_PRELOAD': '/game/overlay.so',
                                      'STEAM_COMPAT_DATA_PATH': '/games/compatdata/230410',
                                      'DISPLAY': ':0', 'HOME': '/wrong'}
        self.args.game_cwd = '/games/Warframe'
        self.assertEqual(self.run_launcher(), 0)
        _, command, options = self.launches[2]
        self.assertEqual(options['cwd'], '/games/Warframe')
        self.assertNotEqual(options['env'].get('LD_PRELOAD'), '/game/overlay.so')
        self.assertIn('LD_PRELOAD=/game/overlay.so', command)
        self.assertIn('SteamAppId=230410', command)
        self.assertIn('HOME='+str(self.root), command)
        self.assertNotIn('HOME=/wrong', command)
        self.assertLess(command.index('desktop-user'), command.index('LD_PRELOAD=/game/overlay.so'))

    def test_steam_stopping_wrapper_stops_game_namespace(self):
        self.game_exit = None
        self.args.parent_alive = lambda: False
        self.assertEqual(self.run_launcher(), 130)
        self.assert_cleaned()

    def test_game_success_cannot_hide_failed_billing_pause(self):
        self.stop_confirmed = False
        self.proxy_exit = 1
        with self.assertRaisesRegex(ValueError, 'billing pause could not be confirmed'):
            self.run_launcher()
        self.assert_cleaned()
        self.assertNotIn('Server confirmed', self.stdout.getvalue())

    def test_missing_status_is_not_treated_as_success(self):
        self.no_status = True
        with self.assertRaisesRegex(ValueError, 'could not be confirmed'):
            self.run_launcher()
        self.assert_cleaned()

    def test_proxy_error_is_reported_even_if_billing_was_paused(self):
        self.proxy_exit = 1
        with self.assertRaisesRegex(ValueError, 'proxy failed'):
            self.run_launcher()
        self.assert_cleaned()
        self.assertIn('Server confirmed', self.stdout.getvalue())

    def test_hung_proxy_is_killed_and_pause_remains_unconfirmed(self):
        self.proxy_timeout = True
        with self.assertRaisesRegex(ValueError, 'could not be confirmed'):
            self.run_launcher()
        self.assertTrue(self.processes[0].killed)
        self.assert_cleaned()

    def test_tunnel_launch_failure_still_stops_proxy_and_removes_network(self):
        self.fail_launch = 'tunnel'
        with self.assertRaises(OSError):
            self.run_launcher()
        self.assert_cleaned()
        self.assertEqual(self.processes[0].returncode, 0)

    def test_partial_network_creation_is_rolled_back(self):
        self.fail_command = lambda cmd: cmd[1:3] == ['link', 'add']
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_launcher()
        self.assert_cleaned()
        self.assertEqual(self.launches, [])

    def test_sigterm_stops_children_and_confirms_pause(self):
        self.game_exit = None
        self.send_signal = True
        self.assertEqual(self.run_launcher(), 130)
        self.assert_cleaned()
        self.assertIn((411, signal.SIGTERM), self.kills)
        self.assertIn((412, 9), self.kills)

    def test_proxy_crash_is_not_hidden_by_game_still_running(self):
        self.game_exit = None
        self.crash_proxy = True
        self.no_status = True
        with self.assertRaisesRegex(ValueError, 'could not be confirmed'):
            self.run_launcher()
        self.assert_cleaned()

    def test_tunnel_crash_stops_session(self):
        self.game_exit = None
        self.crash_tunnel = True
        with self.assertRaisesRegex(ValueError, 'Acceleration process exited'):
            self.run_launcher()
        self.assert_cleaned()

    def test_proxy_ready_timeout_cleans_up(self):
        self.no_ready = True
        with self.assertRaisesRegex(ValueError, 'Proxy could not start'):
            self.run_launcher()
        self.assert_cleaned()

    def test_tun_ready_timeout_cleans_up(self):
        self.no_tun = True
        with self.assertRaisesRegex(ValueError, 'TUN could not start'):
            self.run_launcher()
        self.assert_cleaned()

    def test_cleanup_failure_does_not_skip_remaining_resources(self):
        self.fail_command = lambda cmd: cmd[1:3] == ['netns', 'delete']
        with self.assertRaisesRegex(ValueError, 'Cleanup incomplete'):
            self.run_launcher()
        self.assert_cleaned()
        self.assertTrue(any(cmd[1:3] == ['link', 'delete'] for cmd in self.commands))
        self.assertIn('cleanup incomplete', self.stderr.getvalue())

    def test_session_mode_does_not_claim_to_pause_billing(self):
        self.args.account = None
        self.no_status = True
        self.assertEqual(self.run_launcher(), 0)
        self.assertNotIn('Server confirmed', self.stdout.getvalue())
        self.assert_cleaned()
