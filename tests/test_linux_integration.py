"""Opt-in real netns/TUN/Hev test using only a synthetic loopback node.

Run from a normal Linux user's readable checkout:
sudo -E env XIANNIU_LINUX_INTEGRATION=1 python3 -m unittest discover -s tests -p test_linux_integration.py -v
No real account, external node, or service billing is used.
"""
from argparse import Namespace
from dataclasses import asdict
import json
import io
import os
from pathlib import Path
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from xianniu_linux.netns import run
from xianniu_linux.protocol import MAX_FRAME, Session
from xianniu_linux.launch import privileged_launch


def read_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        chunk = stream.recv(length - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


class EchoNode(socketserver.ThreadingTCPServer):
    daemon_threads = True


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            header = read_exact(self.request, 8)
            for _ in range(4):
                length = struct.unpack('<I', read_exact(self.request, 4))[0]
                if length > 255:
                    return
                read_exact(self.request, length)
            if header[1] == 1:
                self.request.sendall(b'\x04\x5a' + b'\0' * 6)
                while True:
                    length = struct.unpack('<I', read_exact(self.request, 4))[0]
                    if length > MAX_FRAME:
                        return
                    self.request.sendall(struct.pack('<I', length) + read_exact(self.request, length))
            elif header[1] == 4:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.bind(('127.0.0.1', 0))
                    udp.settimeout(10)
                    self.request.sendall(b'\x02\x5a' + struct.pack('<H', udp.getsockname()[1]) + b'\0' * 4)
                    # The actual observed node is allowed to close this setup TCP socket.
                    self.request.close()
                    while True:
                        data, peer = udp.recvfrom(65535)
                        udp.sendto(data, peer)
        except (OSError, EOFError):
            pass


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('XIANNIU_LINUX_INTEGRATION') == '1',
                     'Opt-in Linux netns/TUN test; no real account is used')
class LinuxTunnelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or int(os.environ.get('SUDO_UID', '0')) <= 0:
            raise RuntimeError('Run with sudo -E from a normal user')
        for executable in ('ip', 'runuser', 'sysctl', 'hev-socks5-tunnel'):
            if not shutil.which(executable):
                raise RuntimeError('Missing integration prerequisite: ' + executable)
        if not Path('/dev/net/tun').exists():
            raise RuntimeError('Missing /dev/net/tun')

    def test_real_tcp_udp_routing_privileges_dns_and_network_cleanup(self):
        self.exercise()

    def test_warframe_helper_preserves_proton_and_leaves_steam_outside(self):
        self.exercise(privileged=True)

    def test_closing_steam_wrapper_cleans_up_game_and_tunnel(self):
        self.exercise(privileged=True, parent_exit=True)

    def exercise(self, privileged=False, parent_exit=False):
        import pwd
        user = pwd.getpwuid(int(os.environ['SUDO_UID']))
        ip = shutil.which('ip')
        def snapshot(*args):
            return subprocess.check_output([ip, *args], text=True)
        original_routes = snapshot('-4', 'route', 'show', 'default')
        original_namespaces = snapshot('netns', 'list')
        original_links = {item['ifname'] for item in json.loads(snapshot('-j', 'link', 'show'))}
        original_netns = os.stat('/proc/self/ns/net').st_ino
        node = EchoNode(('127.0.0.1', 0), EchoHandler)
        thread = threading.Thread(target=node.serve_forever, daemon=True)
        thread.start()
        parent = None
        try:
            with tempfile.TemporaryDirectory(prefix='xianniu-integration-') as directory:
                folder = Path(directory)
                os.chown(folder, user.pw_uid, user.pw_gid)
                session = folder/'session.json'
                session.write_text(json.dumps(asdict(Session('127.0.0.1', node.server_address[1], 'test', 'test'))))
                os.chown(session, user.pw_uid, user.pw_gid)
                os.chmod(session, 0o600)
                result = folder/'result.json'
                application = folder/'probe.py'
                application.write_text('''import json, os, socket, sys, time
from pathlib import Path
with socket.create_connection(('198.51.100.10', 12345), timeout=10) as tcp:
    tcp.sendall(b'tcp-through-tun')
    received = bytearray()
    while len(received) < len(b'tcp-through-tun'):
        block = tcp.recv(1024)
        assert block, 'TCP closed before echo completed'
        received.extend(block)
    assert bytes(received) == b'tcp-through-tun'
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    udp.settimeout(10)
    udp.sendto(b'udp-through-tun', ('198.51.100.10', 12346))
    data, peer = udp.recvfrom(1024)
    assert data == b'udp-through-tun'
    assert peer == ('198.51.100.10', 12346)
Path(sys.argv[1]).write_text(json.dumps({'uid': os.getuid(),
    'dns': Path('/etc/resolv.conf').read_text(),
    'netns': os.stat('/proc/self/ns/net').st_ino, 'cwd': os.getcwd(),
    'app_id': os.environ.get('SteamAppId'),
    'compat': os.environ.get('STEAM_COMPAT_DATA_PATH'),
    'ipv6_disabled': Path('/proc/sys/net/ipv6/conf/all/disable_ipv6').read_text().strip()}))
if os.environ.get('XIANNIU_TEST_WAIT') == '1':
    time.sleep(60)
''', encoding='utf-8')
                os.chmod(application, 0o644)
                args = Namespace(application=['--', sys.executable, str(application), str(result)],
                                 session=session, account=None, hev='hev-socks5-tunnel')
                if privileged:
                    # A separate unprivileged process stands in for Steam's wrapper.
                    # It remains in the host namespace and can still reach loopback.
                    parent_code = '''import os, socket, sys, time
from pathlib import Path
with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=5):
    pass
print(os.stat('/proc/self/ns/net').st_ino, flush=True)
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    if sys.argv[3] == '1' and Path(sys.argv[2]).exists():
        break
    time.sleep(0.05)
'''
                    parent = subprocess.Popen([sys.executable, '-c', parent_code,
                                               str(node.server_address[1]), str(result), str(int(parent_exit))],
                                              user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                                              stdout=subprocess.PIPE, text=True)
                    self.assertEqual(int(parent.stdout.readline()), original_netns)
                    game_env = dict(os.environ, SteamAppId='230410',
                                    STEAM_COMPAT_DATA_PATH=str(folder/'compatdata/230410'),
                                    XIANNIU_TEST_WAIT=str(int(parent_exit)))
                    payload = dict(application=args.application[1:], environment=game_env,
                                   cwd=str(folder), uid=user.pw_uid, parent_pid=parent.pid,
                                   session=str(session), account=None, hev=shutil.which(args.hev))
                    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
                    with patch('sys.stdin', stdin), patch.dict(os.environ, {'PKEXEC_UID': str(user.pw_uid)}):
                        self.assertEqual(privileged_launch(), 130 if parent_exit else 0)
                else:
                    self.assertEqual(run(args), 0)
                outcome = json.loads(result.read_text())
                self.assertEqual(outcome['uid'], user.pw_uid)
                self.assertIn('nameserver 8.8.8.8', outcome['dns'])
                self.assertEqual(outcome['ipv6_disabled'], '1')
                self.assertNotEqual(outcome['netns'], original_netns)
                if privileged:
                    self.assertEqual(outcome['app_id'], '230410')
                    self.assertEqual(outcome['compat'], str(folder/'compatdata/230410'))
                    self.assertEqual(outcome['cwd'], str(folder))
                    if not parent_exit:
                        self.assertIsNone(parent.poll())
                        self.assertEqual(os.stat(f'/proc/{parent.pid}/ns/net').st_ino, original_netns)
        finally:
            if parent:
                if parent.poll() is None:
                    parent.terminate()
                parent.communicate(timeout=5)
            node.shutdown()
            node.server_close()
            thread.join(timeout=5)
        self.assertEqual(snapshot('-4', 'route', 'show', 'default'), original_routes)
        self.assertEqual(snapshot('netns', 'list'), original_namespaces)
        self.assertEqual({item['ifname'] for item in json.loads(snapshot('-j', 'link', 'show'))}, original_links)
