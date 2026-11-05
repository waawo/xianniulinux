"""Wrap only Warframe's Steam launch command, preserving its user environment."""
from argparse import Namespace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

WARFRAME_APP_ID = '230410'
MAX_REQUEST = 2 * 1024 * 1024
APP_ID_KEYS = ('SteamAppId', 'SteamGameId', 'STEAM_COMPAT_APP_ID')


def validate_launch(application, environment):
    if (not isinstance(application, list) or not application or
            any(not isinstance(value, str) or '\0' in value for value in application) or
            not application[0] or '%command%' in application):
        raise ValueError('Use this wrapper in Warframe Steam launch options, followed by -- %command%')
    if (not isinstance(environment, dict) or
            any(not isinstance(key, str) or not key or '=' in key or '\0' in key or
                not isinstance(value, str) or '\0' in value for key, value in environment.items())):
        raise ValueError('Invalid game environment')
    app_ids = [environment[key] for key in APP_ID_KEYS if environment.get(key)]
    if not app_ids or any(value != WARFRAME_APP_ID for value in app_ids):
        raise ValueError('This launcher only accepts Warframe from Steam (app 230410)')
    if Path(application[0]).name.lower() in ('steam', 'steam.sh', 'steam.exe'):
        raise ValueError('Wrap the Warframe game command, not the Steam client')


def process_identity(pid, uid):
    """Return a birth marker, or None for a dead process/different owner."""
    try:
        path = Path('/proc') / str(pid)
        if path.stat().st_uid != uid:
            return None
        fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def warframe(args):
    if sys.platform != 'linux' or os.getuid() == 0:
        raise ValueError('Launch Warframe from Steam as your normal Linux desktop user')
    application = list(args.application)
    if application[:1] == ['--']:
        application.pop(0)
    environment = dict(os.environ)
    validate_launch(application, environment)
    account = args.account
    if not account and not args.session:
        account = Path.home()/'.config/xianniu-linux/account.json'
    request = {'application': application, 'environment': environment,
               'cwd': str(Path.cwd()), 'uid': os.getuid(), 'parent_pid': os.getpid(),
               'account': str(account.resolve(strict=True)) if account else None,
               'session': str(args.session.resolve(strict=True)) if args.session else None,
               'hev': shutil.which(args.hev)}
    if not request['hev']:
        raise ValueError('Install hev-socks5-tunnel or set --hev /absolute/path')
    authorization = shutil.which(args.auth)
    if not authorization:
        raise ValueError('Install pkexec and enable a desktop polkit authentication agent; '
                         'use --auth sudo only from a terminal with sudo access')
    payload = json.dumps(request).encode('utf-8')
    if len(payload) > MAX_REQUEST:
        raise ValueError('Steam launch environment is too large')
    # No Steam environment is supplied to privileged executables. It travels
    # over stdin and is applied only after runuser drops to the desktop user.
    clean_environment = {key: value for key, value in os.environ.items()
                         if key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TERM', 'DISPLAY',
                                    'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR')}
    package_root = str(Path(__file__).resolve().parents[1])
    bootstrap = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                 'from xianniu_linux.__main__ import main; raise SystemExit(main())')
    child = subprocess.Popen([authorization, sys.executable, '-I', '-c', bootstrap,
                              package_root, '_warframe-helper'],
                             stdin=subprocess.PIPE, env=clean_environment)
    # The root helper watches this process. Steam stopping the wrapper also
    # tears down the game namespace, even though the user cannot signal root.
    child.communicate(payload)
    return child.returncode


def privileged_launch():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('The internal launcher requires desktop authorization')
    uid = int(os.environ.get('PKEXEC_UID', os.environ.get('SUDO_UID', '0')))
    if uid <= 0:
        raise ValueError('Refusing to launch a game as root')
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise ValueError('Launch request exceeds size limit')
    request = json.loads(raw)
    if not isinstance(request, dict) or request.get('uid') != uid:
        raise ValueError('Launch request does not match the authorized desktop user')
    application, environment = request.get('application'), request.get('environment')
    validate_launch(application, environment)
    pid = request.get('parent_pid')
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError('Invalid launch parent')
    identity = process_identity(pid, uid)
    if identity is None:
        raise ValueError('The Steam launch wrapper has already exited')
    for name in ('cwd', 'hev'):
        if not isinstance(request.get(name), str) or not Path(request[name]).is_absolute():
            raise ValueError('Invalid game launch path')
    for name in ('account', 'session'):
        if request.get(name) is not None and (not isinstance(request[name], str) or
                                             not Path(request[name]).is_absolute()):
            raise ValueError('Invalid account/session path')
    account = Path(request['account']).resolve(strict=True) if request.get('account') else None
    session = Path(request['session']).resolve(strict=True) if request.get('session') else None
    if not account and not session:
        raise ValueError('Missing account or session profile')
    if not session:
        session = account.with_name('session-linux.json')
    os.environ['SUDO_UID'] = str(uid)
    from .netns import run
    return run(Namespace(application=application, account=account, session=session, hev=request['hev'],
                         game_environment=environment, game_cwd=request['cwd'],
                         parent_alive=lambda: process_identity(pid, uid) == identity))
