import asyncio
import struct
import unittest
from xianniu_linux.classic import read_frame
from xianniu_linux.protocol import MAX_FRAME, ProtocolError, Session, decode_udp, encode_udp, parse_reply, rc4, request
from xianniu_linux.socks import udp_request


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.session = Session('127.0.0.1', 9000, '0123456789', '0123456789abcdef')

    def test_rc4_rfc6229_key_1_40_bits(self):
        # RFC 6229, offset 0, key 01 02 03 04 05.
        self.assertEqual(rc4(b'\0' * 16, bytes.fromhex('0102030405')).hex(),
                         'b2396305f03dc027ccc3524a0a1118a8')

    def test_classic_udp_handshake_fixture(self):
        expected = bytes.fromhex('69040001000000010a000000') + b'0123456789'
        expected += bytes.fromhex('0a000000') + b'0123456789' + b'\0' * 8
        self.assertEqual(request(self.session, 4), expected)

    def test_udp_relay_port_is_little_endian(self):
        self.assertEqual(parse_reply(bytes.fromhex('025a59700a0d676d'), 4), 28761)

    def test_rejection_is_not_accepted_as_success(self):
        for data in (bytes.fromhex('025b59700a0d676d'), bytes.fromhex('045a59700a0d676d'), b'\x02\x5a'):
            with self.subTest(data=data):
                with self.assertRaises(ProtocolError):
                    parse_reply(data, 4)

    def test_udp_capture_layout(self):
        raw = bytes.fromhex('fe0808080800351234')
        self.assertEqual(decode_udp(raw), ('8.8.8.8', 53, b'\x12\x34'))
        self.assertEqual(encode_udp('8.8.8.8', 53, b'\x12\x34'), raw)

    def test_malformed_and_fragmented_udp_is_rejected(self):
        for data in (b'', b'\0\0\1\1' + b'\0' * 8, b'\0\0\0\3\x20short', b'\0\0\0\1' + b'\0' * 6):
            with self.subTest(data=data):
                with self.assertRaises(ProtocolError):
                    udp_request(data)

    def test_session_repr_does_not_disclose_credentials(self):
        self.assertNotIn(self.session.username, repr(self.session))
        self.assertNotIn(self.session.password, repr(self.session))


class FrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_and_coalesced_frames(self):
        reader = asyncio.StreamReader()
        async def feed():
            for chunk in (b'\x03', b'\0\0', b'\0a', b'bc\x02\0\0\0de'):
                reader.feed_data(chunk)
                await asyncio.sleep(0)
            reader.feed_eof()
        task = asyncio.create_task(feed())
        self.assertEqual(await read_frame(reader), b'abc')
        self.assertEqual(await read_frame(reader), b'de')
        await task

    async def test_empty_truncated_body_is_an_error(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack('<I', 5))
        reader.feed_eof()
        with self.assertRaises(ProtocolError):
            await read_frame(reader)

    async def test_frame_limit_checked_before_allocation(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack('<I', MAX_FRAME + 1))
        with self.assertRaises(ProtocolError):
            await read_frame(reader)
