"""Linux-only per-application routing via a disposable network namespace.

Only namespace routes and a temporary veth are created. The host's default
route, IP forwarding, DNS, and firewall rules are not modified.
"""
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .protocol import Session
from .api import Account


def select_subnet(routes):
    occupied = []
    for route in routes:
        destination = route.get('dst', 'default')
        if destination == 'default':
            continue
        try:
            occupied.append(ipaddress.IPv4Network(destination, strict=False))
        except ValueError:
            continue
    choices = list(range(16, 240))
    secrets.SystemRandom().shuffle(choices)
    for third in choices:
        candidate = ipaddress.IPv4Network(f'169.254.{third}.0/30')
        if not any(candidate.overlaps(network) for network in occupied):
            return candidate
    raise ValueError('No unused local link subnet found for the temporary namespace')


def hev_config(host, port):
    # See the upstream conf/main.yml. UDP-in-UDP is required here.
    ipaddress.IPv4Address(host)
    return f'''tunnel:
  name: xntun
  mtu: 1400
  multi-queue: false
  ipv4: 198.18.0.1
  icmp: 'off'
socks5:
  address: {host}
  port: {port}
  udp: 'udp'
misc:
  log-level: warn
  log-file: stderr
  connect-timeout: 10000
  tcp-read-write-timeout: 300000
  udp-read-write-timeout: 60000
'''


def run(args):
    if sys.platform != 'linux':
        raise ValueError('The per-application launcher requires Linux')
    if os.geteuid() != 0 or not os.environ.get('SUDO_UID'):
        raise ValueError('Run this launcher with sudo -E from your normal desktop user')
    import pwd
    uid = int(os.environ['SUDO_UID'])
    if uid <= 0:
        raise ValueError('Refusing to run the game as root')
    account = pwd.getpwuid(uid)
    gid = account.pw_gid
    groups = os.getgrouplist(account.pw_name, gid)
    application = list(args.application)
    if application[:1] == ['--']:
        application.pop(0)
    if not application:
        raise ValueError('Specify an application after --, for example -- steam')
    ip = shutil.which('ip')
    runuser = shutil.which('runuser')
    hev = shutil.which(args.hev)
    if not ip or not runuser or not hev:
        raise ValueError('Required commands: ip (iproute2), runuser (util-linux), hev-socks5-tunnel')
    session_path = args.session.resolve()
    account_path = args.account.resolve(strict=True) if args.account else None
    if account_path:
        Account.load(account_path)
    else:
        Session.load(session_path)
    if Path(application[0]).name == 'steam' and shutil.which('pgrep'):
        if subprocess.run(['pgrep', '-u', str(uid), '-x', 'steam'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise ValueError('Fully exit the existing Steam client before launching Steam here')

    root = Path(__file__).resolve().parents[1]
    suffix = secrets.token_hex(3)
    namespace = f'xn-{uid}-{suffix}'
    host_if, guest_if = f'xnh{suffix}', f'xng{suffix}'
    routes = json.loads(subprocess.check_output([ip, '-j', '-4', 'route', 'show', 'table', 'all']))
    subnet = select_subnet(routes)
    host_ip, guest_ip = str(subnet[1]), str(subnet[2])
    runtime = Path(tempfile.mkdtemp(prefix='xianniu-', dir='/tmp'))
    os.chown(runtime, uid, gid)
    os.chmod(runtime, 0o700)
    configuration = runtime / 'hev.yml'
    ready_file = runtime / 'ready.json'
    netns_config = Path('/etc/netns') / namespace
    processes = []
    log_handles = []
    created_namespace = False
    created_veth = False
    created_config = False
    interrupted = False

    def command(*parts, check=True):
        return subprocess.run([str(part) for part in parts], check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def stop(signum, frame):
        nonlocal interrupted
        interrupted = True

    previous_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    environment = dict(os.environ)
    environment.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name,
                       XDG_RUNTIME_DIR=f'/run/user/{uid}')
    environment.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path=/run/user/{uid}/bus')
    environment.pop('PYTHONPATH', None)

    try:
        command(ip, 'netns', 'add', namespace)
        created_namespace = True
        command(ip, 'link', 'add', host_if, 'type', 'veth', 'peer', 'name', guest_if)
        created_veth = True
        command(ip, 'link', 'set', guest_if, 'netns', namespace)
        command(ip, 'addr', 'add', f'{host_ip}/30', 'dev', host_if)
        command(ip, 'link', 'set', host_if, 'up')
        command(ip, '-n', namespace, 'addr', 'add', f'{guest_ip}/30', 'dev', guest_if)
        command(ip, '-n', namespace, 'link', 'set', guest_if, 'up')
        command(ip, '-n', namespace, 'link', 'set', 'lo', 'up')
        netns_config.mkdir(parents=True, exist_ok=False)
        created_config = True
        (netns_config / 'resolv.conf').write_text('nameserver 8.8.8.8\noptions timeout:2 attempts:2\n')
        # Disable IPv6 in this namespace only: this protocol variant supports
        # IPv4, so IPv6 must not silently become an unaccelerated fallback.
        command(ip, 'netns', 'exec', namespace, 'sysctl', '-q', '-w',
                'net.ipv6.conf.all.disable_ipv6=1', 'net.ipv6.conf.default.disable_ipv6=1')

        proxy_log = open(runtime / 'proxy.log', 'w', encoding='utf-8')
        log_handles.append(proxy_log)
        proxy_command = [sys.executable, '-m', 'xianniu_linux', 'serve',
                         '--session', str(session_path), '--listen', host_ip,
                         '--port', '0', '--ready-file', str(ready_file)]
        if account_path:
            proxy_command += ['--account', str(account_path)]
        proxy = subprocess.Popen(proxy_command,
                                 cwd=root, env=environment, user=uid, group=gid, extra_groups=groups,
                                 stdout=proxy_log, stderr=subprocess.STDOUT)
        processes.append(proxy)
        deadline = time.monotonic() + 60
        while not ready_file.exists():
            if interrupted or proxy.poll() is not None or time.monotonic() >= deadline:
                raise ValueError(f'Proxy could not start; diagnostic directory: {runtime}')
            time.sleep(0.05)
        port = json.loads(ready_file.read_text())['port']
        configuration.write_text(hev_config(host_ip, port), encoding='utf-8')
        tunnel_log = open(runtime / 'tunnel.log', 'w', encoding='utf-8')
        log_handles.append(tunnel_log)
        tunnel = subprocess.Popen([ip, 'netns', 'exec', namespace, hev, str(configuration)],
                                  stdout=tunnel_log, stderr=subprocess.STDOUT)
        processes.append(tunnel)
        deadline = time.monotonic() + 10
        while command(ip, '-n', namespace, 'link', 'show', 'xntun', check=False).returncode:
            if interrupted or tunnel.poll() is not None or time.monotonic() >= deadline:
                raise ValueError(f'TUN could not start; diagnostic directory: {runtime}')
            time.sleep(0.05)
        command(ip, '-n', namespace, 'link', 'set', 'xntun', 'up')
        command(ip, '-n', namespace, 'route', 'add', 'default', 'dev', 'xntun')
        # There is no NAT or host forwarding: the guest reaches the local SOCKS
        # adapter over the connected veth; only that adapter reaches the node.
        print(f'Launching application in {namespace}; TCP/UDP use classic mode.', flush=True)
        game = subprocess.Popen([ip, 'netns', 'exec', namespace, runuser, '-u', account.pw_name,
                                 '--', 'env', f'HOME={account.pw_dir}', f'XDG_RUNTIME_DIR=/run/user/{uid}',
                                 f'DBUS_SESSION_BUS_ADDRESS={environment["DBUS_SESSION_BUS_ADDRESS"]}',
                                 *application], env=environment, cwd=account.pw_dir)
        processes.append(game)
        while not interrupted:
            result = game.poll()
            if result is not None:
                return result
            if proxy.poll() is not None or tunnel.poll() is not None:
                raise ValueError(f'Acceleration process exited; diagnostic directory: {runtime}')
            time.sleep(0.2)
        return 130
    finally:
        # Only kill processes in the namespace we just created, never Steam or
        # other applications outside it. Always remove temporary networking.
        if created_namespace:
            result = command(ip, 'netns', 'pids', namespace, check=False)
            for pid_text in result.stdout.split():
                try:
                    os.kill(int(pid_text), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=60 if process is processes[0] else 3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if created_namespace:
            result = command(ip, 'netns', 'pids', namespace, check=False)
            for pid_text in result.stdout.split():
                try:
                    os.kill(int(pid_text), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            command(ip, 'netns', 'delete', namespace, check=False)
        if created_veth:
            command(ip, 'link', 'delete', host_if, check=False)
        if created_config:
            (netns_config / 'resolv.conf').unlink(missing_ok=True)
            netns_config.rmdir()
        for handle in log_handles:
            handle.close()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        # Keep the small diagnostic directory so a failure can be inspected.
        print(f'Acceleration networking removed. Diagnostics: {runtime}', flush=True)
