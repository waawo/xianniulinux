"""Async transports for the single classic protocol variant."""
import asyncio
import socket
import struct
import logging
import time
from .protocol import MAX_FRAME, ProtocolError, Session, decode_udp, encode_udp, frame, parse_reply, request

CONNECT_TIMEOUT = 10


async def open_control(session: Session, command: int, host='', port=1):
    endpoint = (session.tcp_server or session.server, session.tcp_port or session.port) if command == 1 else (session.server, session.port)
    reader = writer = None
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            reader, writer = await asyncio.open_connection(*endpoint, family=socket.AF_INET)
            writer.write(request(session, command, host, port))
            await writer.drain()
            reply = await reader.readexactly(8)
            relay_port = parse_reply(reply, command)
        sock = writer.get_extra_info('socket')
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return reader, writer, relay_port
    except BaseException:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        raise


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    while True:
        length = struct.unpack('<I', await reader.readexactly(4))[0]
        if length > MAX_FRAME:
            raise ProtocolError('Node TCP frame exceeds supported maximum')
        if length:
            try:
                return await reader.readexactly(length)
            except asyncio.IncompleteReadError:
                raise ProtocolError('Truncated node TCP frame body') from None


class NodeDatagrams(asyncio.DatagramProtocol):
    def __init__(self, received):
        self.received = received
        self.lost = asyncio.get_running_loop().create_future()
        self.invalid_packets = 0

    def datagram_received(self, data, addr):
        try:
            self.received(*decode_udp(data))
        except (ProtocolError, ValueError):
            self.invalid_packets += 1

    def error_received(self, exc):
        logging.getLogger(__name__).debug('UDP transport error: %s', exc)
        if not self.lost.done():
            self.lost.set_result(None)

    def connection_lost(self, exc):
        if not self.lost.done():
            self.lost.set_result(None)


class UdpAssociation:
    def __init__(self, reader, writer, transport, protocol):
        self.reader = reader
        self.writer = writer
        self.transport = transport
        self.protocol = protocol

    @classmethod
    async def open(cls, session, received):
        reader, writer, port = await open_control(session, 4)
        try:
            transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: NodeDatagrams(received), remote_addr=(session.server, port),
                family=socket.AF_INET)
            return cls(reader, writer, transport, protocol)
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    def send(self, host, port, payload):
        self.transport.sendto(encode_udp(host, port, payload))

    async def close(self):
        self.transport.close()
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


async def bridge_tcp(local_reader, local_writer, node_reader, node_writer, stats):
    async def upload():
        while data := await local_reader.read(65536):
            node_writer.write(frame(data))
            await node_writer.drain()
            stats['tcp_upload_bytes'] += len(data)
            stats['last_activity_tick'] = int(time.monotonic() * 1000)
        if node_writer.can_write_eof():
            node_writer.write_eof()

    async def download():
        try:
            while True:
                data = await read_frame(node_reader)
                local_writer.write(data)
                await local_writer.drain()
                stats['tcp_download_bytes'] += len(data)
                stats['last_activity_tick'] = int(time.monotonic() * 1000)
        except asyncio.IncompleteReadError as error:
            if error.partial:
                raise ProtocolError('Truncated node TCP frame') from None
            return

    tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if tasks[0] in done and tasks[1] in pending:
            # Allow replies after a client's TCP write half-close, but bound a
            # node that never closes the other direction.
            await asyncio.wait_for(tasks[1], 15)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
