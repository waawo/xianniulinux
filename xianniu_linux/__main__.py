import argparse
import asyncio
import ipaddress
import json
import logging
from pathlib import Path
import secrets
import getpass
from dataclasses import asdict, replace
from contextlib import nullcontext
import socket
import signal
import ssl
import struct
import sys

from .classic import UdpAssociation
from .protocol import ProtocolError, Session
from .socks import SocksServer, read_address
from .api import Account, ApiClient, ApiError
from .control import Acceleration
from .control import write_private_json
from .locking import AccountLock


async def serve(args):
    with AccountLock() if args.account else nullcontext():
        await _serve(args)


async def worker(function, *args):
    # Cancellation cannot stop a thread already changing the server/account.
    # Join it before cleanup or before releasing the cross-process lock.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _serve(args):
    address = ipaddress.IPv4Address(args.listen)
    if not (address.is_loopback or address.is_link_local):
        raise ValueError('Listen address must be loopback or a local link address')
    adapter = None
    server = None
    background = []
    stop_confirmed = False
    manager = Acceleration(Account.load(args.account), args.session) if args.account else None
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    old_term = signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(main_task.cancel))
    try:
        if manager:
            print('Starting an authenticated acceleration session (normal service billing).', flush=True)
            started = await worker(manager.start)
            print(json.dumps({'started': started}), flush=True)
            await worker(manager.refresh, {})
        Session.load(args.session)
        adapter = SocksServer(args.session, args.max_connections)
        server = await asyncio.start_server(adapter.handle, str(address), args.port, family=socket.AF_INET)
        actual_port = server.sockets[0].getsockname()[1]
        print(f'Classic TCP/UDP adapter listening at {address}:{actual_port}', flush=True)
        if args.ready_file:
            write_private_json(args.ready_file, {'port': actual_port})

        async def refresh_loop():
            failures = 0
            while True:
                await asyncio.sleep(args.refresh_interval)
                try:
                    await worker(manager.refresh, dict(adapter.stats))
                    failures = 0
                    print('Session credentials refreshed.', flush=True)
                except ApiError as error:
                    failures += 1
                    print(f'Credential refresh failed: {error}', file=sys.stderr, flush=True)
                    if failures >= 2:
                        raise ApiError('Credential refresh failed twice; acceleration is stopping') from None
                except Exception as error:
                    # Unexpected schema/storage errors must also stop service;
                    # do not echo exception values that could contain secrets.
                    raise ApiError(f'Credential refresh failed ({type(error).__name__}); '
                                   'acceleration is stopping') from None

        async def watch_stop_file():
            while not args.stop_file.exists():
                await asyncio.sleep(0.2)

        if manager:
            background.append(asyncio.create_task(refresh_loop()))
        if args.stop_file:
            background.append(asyncio.create_task(watch_stop_file()))
        async with server:
            background.append(asyncio.create_task(server.serve_forever()))
            done, _ = await asyncio.wait(background, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except asyncio.CancelledError:
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            try:
                if server:
                    server.close()
                    await server.wait_closed()
                if adapter:
                    await adapter.close()
            finally:
                stats = dict(adapter.stats) if adapter else {}
                if manager and (manager.active or manager.clock_owned):
                    try:
                        await worker(manager.stop, stats)
                        stop_confirmed = True
                        print('Server confirmed acceleration stopped and billing paused.', flush=True)
                    except Exception:
                        print('Server stop could not be confirmed. Run stop --account <account.json> to retry.',
                              file=sys.stderr, flush=True)
                        raise
                print(json.dumps({'statistics': stats}), flush=True)
        finally:
            try:
                if getattr(args, 'status_file', None):
                    write_private_json(args.status_file, {'account_managed': manager is not None,
                                                          'stop_confirmed': stop_confirmed})
            finally:
                signal.signal(signal.SIGTERM, old_term)


async def check(args):
    """Check the public local adapter too, rather than only the raw codec."""
    adapter = SocksServer(Session.load(args.session))
    server = await asyncio.start_server(adapter.handle, '127.0.0.1', 0)
    proxy_port = server.sockets[0].getsockname()[1]
    results = {'tcp_tls': False, 'udp_dns': False}
    writers = []
    udp_transport = None
    try:
        async def connect(command, host, port):
            reader, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
            writers.append(writer)
            writer.write(b'\x05\x01\x00')
            await writer.drain()
            if await reader.readexactly(2) != b'\x05\x00':
                raise ProtocolError('Local SOCKS negotiation failed')
            domain = host.encode('ascii')
            writer.write(b'\x05' + bytes([command]) + b'\x00\x03' + bytes([len(domain)]) + domain + struct.pack('!H', port))
            await writer.drain()
            header = await reader.readexactly(4)
            bound = await read_address(reader, header[3])
            if header[:3] != b'\x05\x00\x00':
                raise ProtocolError('Local SOCKS connection failed')
            return reader, writer, bound

        try:
            async with asyncio.timeout(25):
                reader, writer, _ = await connect(1, 'api.warframe.com', 443)
                await writer.start_tls(ssl.create_default_context(), server_hostname='api.warframe.com')
                writer.write(b'GET / HTTP/1.1\r\nHost: api.warframe.com\r\nConnection: close\r\n\r\n')
                await writer.drain()
                status = await reader.readline()
                results['tcp_tls'] = status.startswith(b'HTTP/')
                if results['tcp_tls']:
                    results['http_status'] = status.decode('ascii', 'replace').strip()
        except (OSError, ProtocolError, TimeoutError, asyncio.IncompleteReadError) as error:
            results['tcp_error'] = type(error).__name__

        try:
            async with asyncio.timeout(15):
                udp_reader, udp_writer, bound = await connect(3, '0.0.0.0', 0)
                identifier = secrets.token_bytes(2)
                name = b'\x08warframe\x03com\0'
                dns = identifier + bytes.fromhex('01000001000000000000') + name + b'\0\x01\0\x01'
                packet = b'\0\0\0\x01' + socket.inet_aton('8.8.8.8') + struct.pack('!H', 53) + dns
                loop = asyncio.get_running_loop()
                answer_future = loop.create_future()

                class Receiver(asyncio.DatagramProtocol):
                    def datagram_received(self, data, addr):
                        if not answer_future.done():
                            answer_future.set_result(data)

                udp_transport, _ = await loop.create_datagram_endpoint(Receiver, remote_addr=bound,
                                                                       family=socket.AF_INET)
                udp_transport.sendto(packet)
                answer = await answer_future
                results['udp_dns'] = (len(answer) >= 22 and answer[:10] == packet[:10]
                                      and answer[10:12] == identifier and bool(answer[12] & 0x80)
                                      and answer[13] & 15 == 0)
        except (OSError, ProtocolError, TimeoutError, asyncio.IncompleteReadError) as error:
            results['udp_error'] = type(error).__name__
    finally:
        if udp_transport:
            udp_transport.close()
        for writer in writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)
        server.close()
        await server.wait_closed()
        await adapter.close()
    results['statistics'] = dict(adapter.stats)
    args.check_stats = dict(adapter.stats)
    print(json.dumps(results, indent=2))
    return 0 if results['tcp_tls'] and results['udp_dns'] else 1


async def managed_check(args):
    with AccountLock():
        return await _managed_check(args)


async def _managed_check(args):
    manager = Acceleration(Account.load(args.account), args.session)
    try:
        print('Starting one authenticated acceleration session for this check.', flush=True)
        await worker(manager.start)
        await worker(manager.refresh, {})
        print('Session start and credential refresh succeeded.', flush=True)
        return await check(args)
    finally:
        if manager.active or manager.clock_owned:
            await worker(manager.stop, getattr(args, 'check_stats', {}))
            print('Server confirmed test session stopped and billing paused.', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Experimental Xianniu classic-mode TCP/UDP client')
    commands = parser.add_subparsers(dest='command', required=True,
                                    metavar='{login,serve,check,lines,stop,run,warframe}')
    login = commands.add_parser('login', help='Log in with a phone number; prompt for password without saving it')
    login.add_argument('--account', type=Path, required=True)
    login.add_argument('--mobile-code', type=int, default=86)
    login.add_argument('--game-id', type=int, default=159)
    login.add_argument('--line-id', type=int)
    login.add_argument('--area-id', type=int)
    serving = commands.add_parser('serve', help='Expose the observed mode as a local SOCKS5 proxy')
    serving.add_argument('--session', type=Path)
    serving.add_argument('--account', type=Path, help='Start, refresh and stop using an imported account token')
    serving.add_argument('--refresh-interval', type=int, default=180)
    serving.add_argument('--stop-file', type=Path)
    serving.add_argument('--listen', default='127.0.0.1')
    serving.add_argument('--port', type=int, default=1080)
    serving.add_argument('--max-connections', type=int, default=128)
    serving.add_argument('--ready-file', type=Path)
    serving.add_argument('--status-file', type=Path, help=argparse.SUPPRESS)
    serving.add_argument('--debug', action='store_true')
    checking = commands.add_parser('check', help='Check verified TLS and UDP DNS through the local adapter')
    checking.add_argument('--session', type=Path)
    checking.add_argument('--account', type=Path, help='Start a normal billed test session and stop it on completion')
    listing = commands.add_parser('lines', help='Read available lines without starting acceleration')
    listing.add_argument('--account', type=Path, required=True)
    listing.add_argument('--area-id', type=int, default=0)
    stopping = commands.add_parser('stop', help='Ask the server to stop acceleration for this account/game')
    stopping.add_argument('--account', type=Path, required=True)
    namespace = commands.add_parser('run', help='Run a Linux application in an isolated accelerated namespace')
    namespace.add_argument('--session', type=Path)
    namespace.add_argument('--account', type=Path)
    namespace.add_argument('--hev', default='hev-socks5-tunnel')
    namespace.add_argument('application', nargs=argparse.REMAINDER)
    warframe = commands.add_parser('warframe', help='Wrap only Warframe: use in Steam launch options before %%command%%')
    warframe.add_argument('--account', type=Path)
    warframe.add_argument('--session', type=Path)
    warframe.add_argument('--hev', default='hev-socks5-tunnel')
    warframe.add_argument('--auth', choices=('pkexec', 'sudo'), default='pkexec')
    warframe.add_argument('application', nargs=argparse.REMAINDER)
    commands.add_parser('_warframe-helper')
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if getattr(args, 'debug', False) else logging.WARNING,
                        format='%(levelname)s %(message)s')
    try:
        if args.command == 'warframe':
            from .launch import warframe
            return warframe(args)
        if args.command == '_warframe-helper':
            from .launch import privileged_launch
            return privileged_launch()
        if args.command == 'login':
            if args.account.exists():
                account = Account.load(args.account)
            else:
                account = Account(base_url='https://api1.xianniu.com', access_token='pending-login',
                                  game_id=args.game_id, line_id=args.line_id, area_id=args.area_id,
                                  hardware='linux-' + secrets.token_hex(16))
            if not account.hardware:
                account = replace(account, hardware='linux-' + secrets.token_hex(16))
            if args.line_id is not None:
                account = replace(account, line_id=args.line_id)
            if args.area_id is not None:
                account = replace(account, area_id=args.area_id)
            if not sys.stdin.isatty():
                raise ValueError('Login requires an interactive terminal for hidden password entry')
            mobile = input('Phone number: ').strip()
            password = getpass.getpass('Password (not saved): ')
            try:
                with AccountLock():
                    data = ApiClient(account).login_password(mobile, password, args.mobile_code)
                    if not isinstance(data, dict) or not isinstance(data.get('access_token'), str):
                        raise ApiError('Login did not return an authorization token')
                    account = replace(account, access_token=data['access_token'])
                    write_private_json(args.account, asdict(account))
            finally:
                password = ''
            print('Logged in. Authorization saved locally; password not stored.')
            return 0
        if args.command in ('serve', 'check', 'run'):
            if not args.session and not args.account:
                raise ValueError('Provide --account or --session')
            if args.account and not args.session:
                args.session = args.account.resolve().with_name('session-linux.json')
        if args.command == 'run':
            from .netns import run
            return run(args)
        if args.command == 'serve':
            if not 0 <= args.port <= 65535 or not 1 <= args.max_connections <= 1024:
                raise ValueError('Invalid listening port or connection limit')
            if not 60 <= args.refresh_interval <= 600:
                raise ValueError('Refresh interval must be 60 to 600 seconds')
            asyncio.run(serve(args))
            return 0
        if args.command == 'lines':
            account = Account.load(args.account)
            lines = ApiClient(account).call('/client/accelerate/linelist',
                                           {'game_id': account.game_id, 'area_id': args.area_id})
            if not isinstance(lines, list):
                raise ApiError('Unexpected line-list response')
            for line in lines:
                print(json.dumps({key: line.get(key) for key in
                                  ('line_id', 'line_title', 'area_ids', 'line_state')}, ensure_ascii=True))
            return 0
        if args.command == 'stop':
            manager = Acceleration(Account.load(args.account), args.account.with_name('session-linux.json'))
            with AccountLock():
                manager.stop({}, force=True)
            print('Server confirmed acceleration stopped and billing paused.')
            return 0
        return asyncio.run(managed_check(args) if args.account else check(args))
    except KeyboardInterrupt:
        return 130
    except (OSError, EOFError, ValueError, ProtocolError, ApiError) as error:
        # File paths and ordinary diagnostics are OK; never print a Session.
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
