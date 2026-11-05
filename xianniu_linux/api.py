"""Observed desktop API envelope: JSON -> zlib -> RC4 -> Base64 over HTTPS."""
import base64
from dataclasses import dataclass, field
import json
import hashlib
import platform
import time
from pathlib import Path
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler
import zlib

from .protocol import rc4

API_KEY = b'xi@nNiu001'
MAX_RESPONSE = 2 * 1024 * 1024
ALLOWED_HOSTS = {'api1.xianniu.com', 'user.xianniu.com', 'webapi.xianniu.com', 'backup-user.xianniu.net'}
ALLOWED_PATHS = {'/client/accelerate/token', '/client/accelerate/start',
                 '/client/accelerate/stop', '/client/accelerate/linelist',
                 '/client/accelerate/success', '/client/auth/login',
                 '/client/auth/login_token', '/client/auth/logout',
                 '/client/member/details', '/client/member/recovery', '/client/member/suspend'}


class ApiError(Exception):
    def __init__(self, message, *, code=None, server_message=None):
        super().__init__(message)
        self.code = code
        self.server_message = server_message


def encode_body(body: dict) -> bytes:
    plain = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(plain) > MAX_RESPONSE:
        raise ApiError('API request exceeds size limit')
    return base64.b64encode(rc4(zlib.compress(plain), API_KEY))


def decode_body(raw: bytes) -> dict:
    if len(raw) > MAX_RESPONSE:
        raise ApiError('API response exceeds size limit')
    try:
        compressed = rc4(base64.b64decode(raw.strip(), validate=True), API_KEY)
        decoder = zlib.decompressobj()
        plain = decoder.decompress(compressed, MAX_RESPONSE + 1)
        if len(plain) > MAX_RESPONSE or not decoder.eof or decoder.unused_data:
            raise ApiError('Invalid or oversized compressed API response')
        body = json.loads(plain.decode('utf-8'))
        if not isinstance(body, dict):
            raise ApiError('API response is not an object')
        return body
    except (ValueError, UnicodeError, zlib.error):
        raise ApiError('Unsupported or malformed API response envelope') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError('Refusing to redirect an authenticated API request')


@dataclass(frozen=True)
class Account:
    base_url: str
    access_token: str = field(repr=False)
    game_id: int
    line_id: int | None = None
    area_id: int | None = None
    hardware: str | None = field(default=None, repr=False)
    isp_type: int = 0
    channel: str = 'guanwang'

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or
                parsed.username or parsed.password or parsed.port not in (None, 443) or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Unsupported API origin')
        if not self.access_token or len(self.access_token) > 4096 or self.game_id <= 0:
            raise ValueError('Invalid account token or game id')

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding='utf-8'))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class ApiClient:
    def __init__(self, account: Account):
        self.account = account
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def call(self, path, values, *, headers=None):
        if path not in ALLOWED_PATHS:
            raise ValueError('Unsupported accelerator API operation')
        body = dict(values)
        if path not in ('/client/auth/login', '/client/auth/login_token'):
            body['access_token'] = self.account.access_token
        request_headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                           'User-Agent': 'xianniu-linux-experimental/0.1'}
        request_headers.update(headers or {})
        request = Request(self.account.base_url.rstrip('/') + path, data=encode_body(body),
                          headers=request_headers, method='POST')
        try:
            with self.opener.open(request, timeout=15) as response:
                decoded = decode_body(response.read(MAX_RESPONSE + 1))
        except HTTPError as error:
            raise ApiError(f'API HTTP status {error.code}') from None
        except (URLError, TimeoutError, OSError):
            raise ApiError('Could not reach the authenticated API') from None
        if decoded.get('code') != 0:
            # Return only the numeric status; server messages may echo account data.
            code = decoded.get('code')
            if not isinstance(code, (int, str)) or len(str(code)) > 12:
                code = 'unknown'
            raise ApiError(f'API rejected the operation (code={code})', code=code,
                           server_message=decoded.get('msg'))
        return decoded.get('data')

    def server_datetime(self):
        request = Request(self.account.base_url.rstrip('/') + '/open/tools/datetime?time=' +
                          str(int(time.monotonic() * 1000)), headers={'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=15) as response:
                body = json.loads(response.read(65536).decode('utf-8'))
            value = body['data']['datetime']
            if body.get('code') != 0 or not isinstance(value, str) or len(value) > 64:
                raise ValueError('Invalid time response')
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise ApiError('Could not get the server time required for login') from None

    def login_digest(self, mobile, digest, mobile_code=86):
        """Internal login primitive; callers should use getpass and discard the digest."""
        if not mobile.isdigit() or not 6 <= len(mobile) <= 20:
            raise ValueError('Invalid phone number')
        if len(digest) != 32 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid password digest')
        if not self.account.hardware:
            raise ValueError('A stable device identifier is required')
        timestamp = self.server_datetime()
        user_agent = ('Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 RequestTime/' + timestamp)
        return self.call('/client/auth/login', {
            'mobile': mobile, 'mobile_code': mobile_code, 'password': digest,
            'hardware': self.account.hardware, 'client_type': 0, 'os_type': 0,
            'client_version': '5.2.8.2', 'channel': self.account.channel,
            'os_info': 'PC_' + platform.node(), 'os_version': platform.release(),
            'datetime': timestamp}, headers={'User-Agent': user_agent, 'Accept': '*/*'})

    def login_password(self, mobile, password, mobile_code=86):
        # The desktop hashes the UTF-8 password into a lower-case MD5 digest.
        return self.login_digest(mobile, hashlib.md5(password.encode('utf-8')).hexdigest(), mobile_code)
