"""Local RFC1928 adapter; CONNECT and UDP ASSOCIATE, IPv4/DNS only."""
import asyncio
from collections import Counter
import ipaddress
import logging
import socket
import struct
import time
from pathlib import Path
from .classic import UdpAssociation, bridge_tcp, open_control
from .protocol import ProtocolError, Session

LOG = logging.getLogger(__name__)


async def read_address(reader, atyp):
    if atyp == 1:
        host = socket.inet_ntoa(await reader.readexactly(4))
    elif atyp == 3:
        length = (await reader.readexactly(1))[0]
        if not length:
            raise ProtocolError('Empty SOCKS domain')
        host = (await reader.readexactly(length)).decode('ascii')
    else:
        raise ProtocolError('IPv6 address type is not supported by this mode')
    port = struct.unpack('!H', await reader.readexactly(2))[0]
    return host, port


def reply(status, host='0.0.0.0', port=0):
    return b'\x05' + bytes([status]) + b'\x00\x01' + socket.inet_aton(host) + struct.pack('!H', port)


def udp_request(data):
    if len(data) < 4 or data[:3] != b'\0\0\0':
        raise ProtocolError('Fragmented or malformed SOCKS UDP packet')
    if data[3] == 1:
        if len(data) < 10:
            raise ProtocolError('Truncated SOCKS UDP address')
        host = socket.inet_ntoa(data[4:8])
        offset = 8
    elif data[3] == 3:
        if len(data) < 5 or data[4] == 0:
            raise ProtocolError('Empty SOCKS UDP domain')
        offset = 5 + data[4]
        if len(data) < offset + 2:
            raise ProtocolError('Truncated SOCKS UDP domain')
        host = data[5:offset].decode('ascii')
    else:
        raise ProtocolError('Unsupported SOCKS UDP address type')
    port = struct.unpack_from('!H', data, offset)[0]
    if not port:
        raise ProtocolError('Empty SOCKS UDP destination port')
    return host, port, data[offset + 2:]


class ClientDatagrams(asyncio.DatagramProtocol):
    def __init__(self, peer_ip, peer_port, stats):
        self.peer_ip = peer_ip
        self.peer_port = peer_port or None
        self.peer = None
        self.node = None
        self.transport = None
        self.stats = stats
        self.tasks = set()
        self.dns_cache = {}
        self.closed = False

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if self.closed or addr[0] != self.peer_ip or (self.peer_port and addr[1] != self.peer_port):
            return
        if len(self.tasks) >= 128:
            self.stats['udp_dropped'] += 1
            return
        try:
            host, port, payload = udp_request(data)
        except (ProtocolError, UnicodeError, ValueError):
            self.stats['udp_dropped'] += 1
            return
        if self.peer is None:
            self.peer = addr
            self.peer_port = addr[1]
        task = asyncio.create_task(self.forward(host, port, payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def forward(self, host, port, payload):
        try:
            try:
                ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                cache = self.dns_cache.get(host)
                if cache and cache[1] > time.monotonic():
                    host = cache[0]
                else:
                    info = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                        host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM), 5)
                    address = info[0][4][0]
                    if len(self.dns_cache) >= 256:
                        self.dns_cache.clear()
                    self.dns_cache[host] = (address, time.monotonic() + 60)
                    host = address
            if self.node is not None and not self.closed:
                self.node.send(host, port, payload)
                self.stats['udp_upload_bytes'] += len(payload)
                self.stats['last_activity_tick'] = int(time.monotonic() * 1000)
        except (OSError, ValueError, TimeoutError):
            self.stats['udp_dropped'] += 1

    def received(self, host, port, payload):
        self.stats['udp_node_packets'] += 1
        if self.peer is not None and not self.closed:
            data = b'\0\0\0\x01' + socket.inet_aton(host) + struct.pack('!H', port) + payload
            self.transport.sendto(data, self.peer)
            self.stats['udp_download_bytes'] += len(payload)
            self.stats['last_activity_tick'] = int(time.monotonic() * 1000)

    async def close(self):
        self.closed = True
        if self.transport:
            self.transport.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.node:
            await self.node.close()


class SocksServer:
    def __init__(self, session: Session | Path, max_connections=256):
        self.session = session
        self.max_connections = max_connections
        self.stats = Counter()
        self.clients = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self.clients) >= self.max_connections:
            writer.close()
            await writer.wait_closed()
            return
        self.clients.add(task)
        node_writer = None
        udp = None
        replied = False
        try:
            session = Session.load(self.session) if isinstance(self.session, Path) else self.session
            async with asyncio.timeout(15):
                version, count = await reader.readexactly(2)
                if version != 5 or count == 0:
                    return
                methods = await reader.readexactly(count)
                if 0 not in methods:
                    writer.write(b'\x05\xff')
                    await writer.drain()
                    return
                writer.write(b'\x05\0')
                await writer.drain()
                version, command, reserved, atyp = await reader.readexactly(4)
                if version != 5 or reserved != 0:
                    raise ProtocolError('Malformed SOCKS request')
                if atyp not in (1, 3):
                    writer.write(reply(8))
                    await writer.drain()
                    return
                host, port = await read_address(reader, atyp)
                if command == 1 and port:
                    node_reader, node_writer, _ = await open_control(session, 1, host, port)
                    writer.write(reply(0))
                    await writer.drain()
                    replied = True
                    self.stats['tcp_connections'] += 1
                elif command == 3:
                    peer_ip = writer.get_extra_info('peername')[0]
                    # A UDP association is bound to the source of its TCP
                    # control connection; it cannot relay for arbitrary peers.
                    if host not in ('0.0.0.0', peer_ip):
                        raise ProtocolError('SOCKS UDP source does not match control peer')
                    local_ip = writer.get_extra_info('sockname')[0]
                    udp = ClientDatagrams(peer_ip, port, self.stats)
                    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                        lambda: udp, local_addr=(local_ip, 0), family=socket.AF_INET)
                    udp.node = await UdpAssociation.open(session, udp.received)
                    bound = transport.get_extra_info('sockname')
                    writer.write(reply(0, bound[0], bound[1]))
                    await writer.drain()
                    replied = True
                    self.stats['udp_associations'] += 1
                else:
                    writer.write(reply(7))
                    await writer.drain()
                    return
            if command == 1:
                await bridge_tcp(reader, writer, node_reader, node_writer, self.stats)
            else:
                waiters = [asyncio.create_task(reader.read()),
                           asyncio.ensure_future(asyncio.shield(udp.node.protocol.lost))]
                try:
                    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                    for finished in done:
                        finished.result()
                    if waiters[0] in done:
                        self.stats['udp_client_control_closed'] += 1
                    if waiters[1] in done:
                        self.stats['udp_node_transport_closed'] += 1
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                    await asyncio.gather(*waiters, return_exceptions=True)
        except (OSError, EOFError, ProtocolError, asyncio.IncompleteReadError,
                UnicodeError, ValueError, TimeoutError) as error:
            self.stats['connection_errors'] += 1
            LOG.debug('Connection ended: %s: %s', type(error).__name__, error)
            if not replied and not writer.is_closing():
                writer.write(reply(1))
                try:
                    await writer.drain()
                except OSError:
                    pass
        finally:
            try:
                if udp:
                    await udp.close()
                if node_writer:
                    node_writer.close()
                    try:
                        await node_writer.wait_closed()
                    except OSError:
                        pass
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            finally:
                self.clients.discard(task)

    async def close(self):
        tasks = list(self.clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
