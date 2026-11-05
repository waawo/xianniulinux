import asyncio
import socket
import struct
import unittest
from xianniu_linux.classic import read_frame
from xianniu_linux.protocol import Session, decode_udp, encode_udp, frame, rc4
from xianniu_linux.socks import SocksServer, read_address


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = set()
        self.resources = []
        self.node_requests = []
        self.reject = False
        self.close_udp_control = False
        self.node = await asyncio.start_server(self.handle_node, '127.0.0.1', 0)
        node_port = self.node.sockets[0].getsockname()[1]
        self.session = Session('127.0.0.1', node_port, 'test-user', 'test-domain-key')
        self.adapter = SocksServer(self.session)
        self.server = await asyncio.start_server(self.adapter.handle, '127.0.0.1', 0)
        self.proxy_port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for resource in self.resources:
            resource.close()
        self.server.close()
        self.node.close()
        await self.server.wait_closed()
        await self.node.wait_closed()
        await self.adapter.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.sleep(0)

    async def handle_node(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        udp = None
        try:
            header = await reader.readexactly(8)
            fields = []
            for _ in range(4):
                length = struct.unpack('<I', await reader.readexactly(4))[0]
                fields.append(await reader.readexactly(length))
            self.node_requests.append((header, fields))
            command = header[1]
            if self.reject:
                writer.write(bytes([4 if command == 1 else 2, 0x5b]) + b'\0' * 6)
            elif command == 1:
                self.assertEqual(rc4(fields[2], self.session.password.encode()), b'game.test')
                writer.write(b'\x04\x5a' + b'\0' * 6)
                await writer.drain()
                self.assertEqual(await read_frame(reader), b'client hello')
                # Send a reply in small chunks to exercise TCP stream framing.
                response = frame(b'node reply')
                for part in (response[:1], response[1:3], response[3:]):
                    writer.write(part)
                    await writer.drain()
                    await asyncio.sleep(0)
                return
            else:
                class Echo(asyncio.DatagramProtocol):
                    def connection_made(self, transport):
                        self.transport = transport
                    def datagram_received(self, data, addr):
                        host, port, payload = decode_udp(data)
                        self.transport.sendto(encode_udp(host, port, b'reply:' + payload), addr)
                udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    Echo, local_addr=('127.0.0.1', 0))
                port = udp.get_extra_info('sockname')[1]
                writer.write(b'\x02\x5a' + struct.pack('<H', port) + b'\x0a\0\0\x01')
            await writer.drain()
            if command == 4 and self.close_udp_control:
                self.resources.append(udp)
                udp = None
                return
            await reader.read()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if udp:
                udp.close()
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)

    async def connect(self, command, host, port):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.proxy_port)
        self.resources.append(writer)
        writer.write(b'\x05\x01\0')
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b'\x05\0')
        encoded = host.encode()
        writer.write(b'\x05' + bytes([command]) + b'\0\x03' + bytes([len(encoded)]) + encoded + struct.pack('!H', port))
        await writer.drain()
        header = await reader.readexactly(4)
        bound = await read_address(reader, header[3])
        return reader, writer, header[1], bound

    async def test_tcp_connect_and_split_node_frames(self):
        reader, writer, status, _ = await self.connect(1, 'game.test', 443)
        self.assertEqual(status, 0)
        writer.write(b'client hello')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(10), 2), b'node reply')

    async def test_udp_roundtrip_through_local_socks_and_node(self):
        # The observed Xianniu node may close the setup TCP connection while
        # its allocated UDP relay remains usable. This is unlike local SOCKS5.
        self.close_udp_control = True
        reader, writer, status, bound = await self.connect(3, '0.0.0.0', 0)
        self.assertEqual(status, 0)
        future = asyncio.get_running_loop().create_future()
        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if not future.done():
                    future.set_result(data)
        udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(Receiver, remote_addr=bound)
        self.resources.append(udp)
        packet = b'\0\0\0\x01\x08\x08\x08\x08\0\x35payload'
        udp.sendto(packet)
        answer = await asyncio.wait_for(future, 2)
        self.assertEqual(answer, packet[:10] + b'reply:payload')
        self.assertEqual(self.adapter.stats['udp_download_bytes'], len(b'reply:payload'))

    async def test_rejected_node_does_not_fall_back_to_direct(self):
        self.reject = True
        reader, writer, status, _ = await self.connect(1, 'game.test', 443)
        self.assertEqual(status, 1)
        self.assertEqual(await reader.read(), b'')
        self.assertEqual(self.adapter.stats['tcp_connections'], 0)

    async def test_unsupported_command_does_not_contact_node(self):
        reader, writer, status, _ = await self.connect(2, 'game.test', 443)
        self.assertEqual(status, 7)
        self.assertEqual(self.node_requests, [])
