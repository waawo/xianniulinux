"""Manage one authenticated acceleration session, including normal accounting."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading
import time

from .api import Account, ApiClient, ApiError
from .protocol import Session


def write_private_json(path: Path, values):
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + os.urandom(4).hex() + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(values, stream, ensure_ascii=True, indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Acceleration:
    def __init__(self, account: Account, session_path: Path):
        self.account = account
        self.api = ApiClient(account)
        self.path = session_path
        self.session = None
        self.active = False
        self.clock_owned = False
        self.started = time.monotonic()
        self.reported_upload = self.reported_download = 0
        self.last_report = time.monotonic()
        self.lock = threading.Lock()

    def start(self):
        if self.account.line_id is None or self.account.area_id is None or not self.account.hardware:
            raise ValueError('Account profile needs the observed line_id, area_id and hardware fields')
        with self.lock:
            try:
                member = self.api.call('/client/member/details', {})
                if not isinstance(member, dict) or member.get('pause_state') not in (0, 1):
                    raise ApiError('Unknown account billing pause state')
                self.clock_owned = True
                # Observed API convention: 0 = paused, 1 = time running.
                if member['pause_state'] == 0:
                    resumed = self.api.call('/client/member/recovery', {'sender': 1})
                    if not isinstance(resumed, dict) or resumed.get('pause_state') != 1:
                        raise ApiError('Server did not confirm billing resumed')
                self.active = True
                data = self.api.call('/client/accelerate/start', {
                    'line_id': self.account.line_id, 'area_id': self.account.area_id,
                    'game_id': self.account.game_id, 'client_type': 0,
                    'isp_type': self.account.isp_type, 'hardware': self.account.hardware})
                if not isinstance(data, dict):
                    raise ApiError('Unexpected acceleration start response')
                self.session = Session(server=data['speed_ip'], port=int(data['pc_port']),
                                       username=data['line_user'], password=data['line_token'])
                write_private_json(self.path, asdict(self.session))
                self.started = self.last_report = time.monotonic()
                return {'game_id': self.account.game_id, 'line_id': self.account.line_id,
                        'area_id': self.account.area_id, 'mode': 'classic-0x69'}
            except (KeyError, TypeError, ValueError, OSError, ApiError):
                # Starting a server-side session can enable billing. If local
                # initialization fails, immediately request its normal stop.
                self._stop({})
                raise

    def refresh(self, stats):
        with self.lock:
            upload = stats.get('tcp_upload_bytes', 0) + stats.get('udp_upload_bytes', 0)
            download = stats.get('tcp_download_bytes', 0) + stats.get('udp_download_bytes', 0)
            now = time.monotonic()
            elapsed = max(now - self.last_report, 0.001)
            sent = max(0, upload - self.reported_upload)
            received = max(0, download - self.reported_download)
            data = self.api.call('/client/accelerate/token', {
                'game_id': self.account.game_id, 'send_bytes': sent, 'recv_bytes': received,
                'cdn_send_bytes': 0, 'cdn_recv_bytes': 0, 'cdn_speed': 0,
                'speed': int((sent + received) / elapsed),
                'last_access_tick': stats.get('last_activity_tick', 0)})
            if not isinstance(data, dict) or not {'line_user', 'line_token'} <= data.keys():
                raise ApiError('Unexpected acceleration refresh response')
            if self.session is None:
                self.session = Session.load(self.path)
            self.session = replace(self.session, username=data['line_user'], password=data['line_token'])
            write_private_json(self.path, asdict(self.session))
            self.reported_upload, self.reported_download = upload, download
            self.last_report = now

    def _stop(self, stats):
        total = sum(stats.get(key, 0) for key in
                    ('tcp_upload_bytes', 'tcp_download_bytes', 'udp_upload_bytes', 'udp_download_bytes'))
        failure = None
        try:
            if self.active:
                self.api.call('/client/accelerate/stop', {
                    'game_id': self.account.game_id, 'tran_kbytes': total // 1024,
                    'tran_cdn_kbytes': 0, 'avg_delay': 0, 'min_delay': 0, 'max_delay': 0,
                    'packet_loss_rate': 0})
                self.active = False
        except ApiError as error:
            failure = error
        finally:
            if self.clock_owned:
                try:
                    paused = self.api.call('/client/member/suspend', {'sender': 1})
                except ApiError:
                    # A prior stop or a completed request whose reply was lost
                    # can leave the account already paused. Confirm via read.
                    paused = self.api.call('/client/member/details', {})
                if not isinstance(paused, dict) or paused.get('pause_state') != 0:
                    raise ApiError('Server did not confirm billing paused')
                self.clock_owned = False
        if failure:
            raise failure

    def stop(self, stats, force=False):
        with self.lock:
            if force:
                self.active = self.clock_owned = True
            if self.active or self.clock_owned:
                self._stop(stats)
