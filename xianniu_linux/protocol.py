"""Codec for the classic 0x69 variant observed on this user's active session.

This is not standard SOCKS4/5. RC4 is used only for compatibility with the
observed hostname field; this module makes no transport-security claims.
"""
from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
import struct

MAX_FRAME = 204800
MAX_UDP_PAYLOAD = 65500
DOMAIN_SENTINEL = bytes.fromhex('00004900')


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class Session:
    server: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    tcp_server: str | None = None
    tcp_port: int | None = None

    def __post_init__(self):
        ipaddress.IPv4Address(self.server)
        if not 1 <= self.port <= 65535:
            raise ValueError('Invalid session server port')
        user = self.username.encode('latin1')
        key = self.password.encode('latin1')
        if not 1 <= len(user) <= 255 or not 1 <= len(key) <= 255:
            raise ValueError('Invalid session authentication field length')
        if (self.tcp_server is None) != (self.tcp_port is None):
            raise ValueError('TCP server and port must be specified together')
        if self.tcp_server is not None:
            ipaddress.IPv4Address(self.tcp_server)
            if not 1 <= self.tcp_port <= 65535:
                raise ValueError('Invalid TCP server port')

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding='utf-8'))
        return cls(**{name: data[name] for name in
                      ('server', 'port', 'username', 'password', 'tcp_server', 'tcp_port')
                      if name in data})


def rc4(data: bytes, key: bytes) -> bytes:
    if not key:
        raise ValueError('RC4 key must not be empty')
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 255
        state[i], state[j] = state[j], state[i]
    i = j = 0
    output = bytearray()
    for byte in data:
        i = (i + 1) & 255
        j = (j + state[i]) & 255
        state[i], state[j] = state[j], state[i]
        output.append(byte ^ state[(state[i] + state[j]) & 255])
    return bytes(output)


def length_field(data: bytes) -> bytes:
    if len(data) > 255:
        raise ProtocolError('Handshake field exceeds supported limit')
    return struct.pack('<I', len(data)) + data


def request(session: Session, command: int, host: str = '', port: int = 1) -> bytes:
    user = session.username.encode('latin1')
    if command == 4:
        header = bytes.fromhex('6904000100000001')
        name = b''
    elif command == 1:
        if not host or not 1 <= port <= 65535 or '\0' in host:
            raise ValueError('Invalid TCP destination')
        # Numeric IPv4 destinations use the same observed domain field to avoid
        # assuming an unverified alternate binary-address representation.
        encoded = host.encode('idna')
        if len(encoded) > 253 or ':' in host:
            raise ValueError('Only IPv4 and DNS-name destinations are supported')
        name = rc4(encoded, session.password.encode('latin1'))
        header = b'\x69\x01' + struct.pack('!H', port) + DOMAIN_SENTINEL
    else:
        raise ValueError('Only observed CONNECT and UDP association are supported')
    # Both identity fields equalled the issued username in every captured
    # classic-mode request. Do not substitute this for other protocol modes.
    return header + length_field(user) + length_field(user) + length_field(name) + length_field(b'')


def parse_reply(data: bytes, command: int) -> int:
    if len(data) != 8:
        raise ProtocolError('Truncated node handshake reply')
    expected_version = 2 if command == 4 else 4
    if data[0] != expected_version or data[1] != 0x5a:
        raise ProtocolError(f'Node rejected classic mode (version={data[0]}, status={data[1]})')
    if command == 4:
        port = struct.unpack_from('<H', data, 2)[0]
        if not port:
            raise ProtocolError('Node returned an empty UDP relay port')
        return port
    return 0


def encode_udp(host: str, port: int, payload: bytes) -> bytes:
    if not 1 <= port <= 65535 or len(payload) > MAX_UDP_PAYLOAD:
        raise ValueError('Invalid UDP destination or payload length')
    return b'\xfe' + ipaddress.IPv4Address(host).packed + struct.pack('!H', port) + payload


def decode_udp(data: bytes) -> tuple[str, int, bytes]:
    if len(data) < 7 or data[0] != 0xfe:
        raise ProtocolError('Invalid classic UDP frame')
    host = str(ipaddress.IPv4Address(data[1:5]))
    port = struct.unpack_from('!H', data, 5)[0]
    if port == 0:
        raise ProtocolError('Invalid UDP source port')
    return host, port, data[7:]


def frame(payload: bytes) -> bytes:
    if not 0 < len(payload) <= MAX_FRAME:
        raise ValueError('Invalid classic TCP frame length')
    return struct.pack('<I', len(payload)) + payload
