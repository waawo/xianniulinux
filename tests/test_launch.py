from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xianniu_linux.launch import privileged_launch, validate_launch, warframe
from xianniu_linux.__main__ import main


class LaunchTests(unittest.TestCase):
    def test_cli_help_shows_game_wrapper_and_hides_internal_command(self):
        output = io.StringIO()
        with patch('sys.argv', ['xianniu', '--help']), redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit:
                main()
        self.assertEqual(exit.exception.code, 0)
        self.assertIn('%command%', output.getvalue())
        self.assertNotIn('_warframe-helper', output.getvalue())

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.session = self.root/'session.json'
        self.session.write_text('{}')
        self.environment = {'SteamAppId': '230410', 'SteamGameId': '230410',
                            'STEAM_COMPAT_APP_ID': '230410', 'PATH': '/usr/bin',
                            'LD_PRELOAD': '/steam/overlay.so', 'LD_LIBRARY_PATH': '/steam/lib',
                            'STEAM_COMPAT_DATA_PATH': '/games/compatdata/230410', 'DISPLAY': ':0'}
        self.application = ['/games/Proton/proton', 'waitforexitandrun', '/games/Warframe/Launcher.exe']

    def test_only_warframe_app_id_is_accepted(self):
        validate_launch(self.application, self.environment)
        for environment in ({}, {'SteamAppId': '570'}, {'SteamAppId': '230410', 'SteamGameId': '570'}):
            with self.subTest(environment=environment), self.assertRaisesRegex(ValueError, 'only accepts Warframe'):
                validate_launch(self.application, environment)

    def test_no_shell_command_or_steam_client_substitution(self):
        for command in ([], ['%command%'], ['steam'], ['/usr/bin/steam.sh'], ['bad\0command']):
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_launch(command, self.environment)

    def test_environment_cannot_inject_env_options(self):
        for environment in ({'SteamAppId': '230410', '=BAD': 'x'},
                            {'SteamAppId': '230410', 'KEY': 'bad\0value'},
                            {'SteamAppId': '230410', 'KEY': 12}):
            with self.assertRaises(ValueError):
                validate_launch(self.application, environment)

    def test_wrapper_transfers_proton_environment_over_stdin_only(self):
        args = Namespace(application=['--', *self.application], account=None, session=self.session,
                         hev='hev-socks5-tunnel', auth='pkexec')
        with patch('xianniu_linux.launch.sys.platform', 'linux'), \
                patch('xianniu_linux.launch.os.getuid', return_value=1000, create=True), \
                patch('xianniu_linux.launch.os.environ', dict(self.environment)), \
                patch('xianniu_linux.launch.shutil.which', side_effect=lambda name: '/usr/bin/'+name), \
                patch('xianniu_linux.launch.subprocess.Popen') as popen:
            popen.return_value.returncode = 0
            self.assertEqual(warframe(args), 0)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], '/usr/bin/pkexec')
        self.assertIn('-I', command)
        self.assertEqual(command[-1], '_warframe-helper')
        self.assertNotIn('LD_PRELOAD', popen.call_args.kwargs['env'])
        self.assertNotIn('LD_LIBRARY_PATH', popen.call_args.kwargs['env'])
        request = json.loads(popen.return_value.communicate.call_args.args[0])
        self.assertEqual(request['environment'], self.environment)
        self.assertEqual(request['application'], self.application)
        self.assertEqual(request['cwd'], str(Path.cwd()))

    def test_other_game_is_rejected_before_authorization(self):
        args = Namespace(application=self.application)
        with patch('xianniu_linux.launch.sys.platform', 'linux'), \
                patch('xianniu_linux.launch.os.getuid', return_value=1000, create=True), \
                patch('xianniu_linux.launch.os.environ', {'SteamAppId': '570'}), \
                patch('xianniu_linux.launch.subprocess.Popen') as popen:
            with self.assertRaises(ValueError):
                warframe(args)
            popen.assert_not_called()

    def helper(self, uid=1000, parent_marker='birth', **changes):
        request = dict(application=self.application, environment=self.environment,
                       uid=uid, parent_pid=1234, cwd=str(self.root), hev=str(self.root/'hev'),
                       account=None, session=str(self.session))
        request.update(changes)
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))
        with patch('xianniu_linux.launch.sys.platform', 'linux'), \
                patch('xianniu_linux.launch.os.geteuid', return_value=0, create=True), \
                patch.dict(os.environ, {'PKEXEC_UID': '1000'}, clear=True), \
                patch('xianniu_linux.launch.sys.stdin', stdin), \
                patch('xianniu_linux.launch.process_identity', return_value=parent_marker), \
                patch('xianniu_linux.netns.run', return_value=0) as run:
            code = privileged_launch()
            return code, run.call_args.args[0]

    def test_helper_preserves_launch_context_and_watches_parent(self):
        code, args = self.helper()
        self.assertEqual(code, 0)
        self.assertEqual(args.game_environment, self.environment)
        self.assertEqual(args.game_cwd, str(self.root))
        self.assertEqual(args.application, self.application)
        with patch('xianniu_linux.launch.process_identity', return_value='birth'):
            self.assertTrue(args.parent_alive())
        with patch('xianniu_linux.launch.process_identity', return_value='reused-pid'):
            self.assertFalse(args.parent_alive())

    def test_helper_rejects_mismatched_user_or_dead_parent(self):
        with self.assertRaisesRegex(ValueError, 'authorized desktop user'):
            self.helper(uid=1001)
        with self.assertRaisesRegex(ValueError, 'already exited'):
            self.helper(parent_marker=None)

    def test_helper_revalidates_game_identity(self):
        with self.assertRaisesRegex(ValueError, 'only accepts Warframe'):
            self.helper(environment={'SteamAppId': '570'})
