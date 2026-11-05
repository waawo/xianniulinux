import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib
import io

from xianniu_linux.api import API_KEY, Account, ApiClient, ApiError, decode_body, encode_body
from xianniu_linux.control import Acceleration
from xianniu_linux.protocol import Session, rc4
from xianniu_linux.netns import select_subnet, hev_config


class EnvelopeTests(unittest.TestCase):
    def test_login_has_server_time_in_body_and_required_request_header(self):
        account = Account('https://api1.xianniu.com', 'stale-token', 159, hardware='test-device')
        client = ApiClient(account)
        requests = []
        timestamp = '2026-11-05 23:25:58'
        replies = [json.dumps({'code': 0, 'data': {'datetime': timestamp}}).encode(),
                   encode_body({'code': 0, 'data': {'access_token': 'new-token'}})]
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return io.BytesIO(replies.pop(0))
        client.opener = Opener()
        self.assertEqual(client.login_password('13800000000', 'password')['access_token'], 'new-token')
        body = decode_body(requests[1].data)
        self.assertEqual(body['password'], '5f4dcc3b5aa765d61d8327deb882cf99')
        self.assertEqual(body['datetime'], timestamp)
        self.assertNotIn('access_token', body)
        self.assertTrue(requests[1].get_header('User-agent').endswith('RequestTime/' + timestamp))

    def test_api_decodes_an_independently_assembled_response(self):
        plain = b'{"code":0,"data":{"line_user":"synthetic","line_token":"synthetic-key"}}'
        wire = base64.b64encode(rc4(zlib.compress(plain, 1), API_KEY))
        self.assertEqual(decode_body(wire)['data']['line_user'], 'synthetic')

    def test_codec_preserves_utf8_and_numeric_types(self):
        value = {'code': 0, 'data': {'line_title': '\u9999\u6e2f', 'port': 9000}}
        self.assertEqual(decode_body(encode_body(value)), value)

    def test_compression_bomb_is_rejected_before_json_parse(self):
        wire = base64.b64encode(rc4(zlib.compress(b'a' * (3 * 1024 * 1024)), API_KEY))
        with self.assertRaises(ApiError):
            decode_body(wire)

    def test_garbage_envelope_does_not_expose_response(self):
        with self.assertRaises(ApiError) as error:
            decode_body(b'not-an-api-token')
        self.assertNotIn('not-an-api-token', str(error.exception))

    def test_account_origin_rejects_cleartext_redirects_and_lookalikes(self):
        for url in ('http://api1.xianniu.com', 'https://api1.xianniu.com.attacker.test',
                    'https://api1.xianniu.com:8443', 'https://api1.xianniu.com/?token=secret'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    Account(url, 'test-token', 159)

    def test_account_repr_redacts_credentials_and_device(self):
        account = Account('https://api1.xianniu.com', 'unique-test-token', 159, hardware='unique-hardware')
        self.assertNotIn('unique-test-token', repr(account))
        self.assertNotIn('unique-hardware', repr(account))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'session.json'
        self.account = Account('https://api1.xianniu.com', 'test-token', 159,
                               line_id=123, area_id=0, hardware='test-device')
        self.patcher = patch('xianniu_linux.control.ApiClient')
        factory = self.patcher.start()
        self.api = factory.return_value
        self.manager = Acceleration(self.account, self.path)

    def tearDown(self):
        self.patcher.stop()
        self.temporary.cleanup()

    def start_response(self):
        return {'speed_ip': '127.0.0.1', 'pc_port': 9000,
                'line_user': 'initial-user', 'line_token': 'initial-domain-key'}

    def test_start_refresh_stop_records_real_usage(self):
        self.api.call.side_effect = [{'pause_state': 0}, {'pause_state': 1}, self.start_response(),
                                    {'line_user': 'renewed-user', 'line_token': 'renewed-key'}, {}, {'pause_state': 0}]
        self.manager.start()
        self.assertTrue(self.manager.active)
        self.assertEqual(Session.load(self.path).username, 'initial-user')
        stats = {'tcp_upload_bytes': 1024, 'udp_upload_bytes': 128,
                 'tcp_download_bytes': 2048, 'last_activity_tick': 12345}
        self.manager.refresh(stats)
        self.assertEqual(Session.load(self.path).username, 'renewed-user')
        refresh = self.api.call.call_args_list[3].args[1]
        self.assertEqual(refresh['send_bytes'], 1152)
        self.assertEqual(refresh['recv_bytes'], 2048)
        self.assertEqual(refresh['last_access_tick'], 12345)
        self.manager.stop(stats)
        self.assertFalse(self.manager.active)
        self.assertEqual(self.api.call.call_args_list[4].args[1]['tran_kbytes'], 3)
        self.assertFalse(self.manager.clock_owned)

    def test_invalid_start_response_triggers_server_stop(self):
        bad = self.start_response()
        bad['pc_port'] = 0
        self.api.call.side_effect = [{'pause_state': 0}, {'pause_state': 1}, bad, {}, {'pause_state': 0}]
        with self.assertRaises(ValueError):
            self.manager.start()
        self.assertEqual([call.args[0] for call in self.api.call.call_args_list],
                         ['/client/member/details', '/client/member/recovery', '/client/accelerate/start',
                          '/client/accelerate/stop', '/client/member/suspend'])
        self.assertFalse(self.manager.active)

    def test_missing_authorization_profile_does_not_start_billing(self):
        self.manager.account = replace(self.account, hardware=None)
        with self.assertRaises(ValueError):
            self.manager.start()
        self.api.call.assert_not_called()

    def test_failed_refresh_does_not_advance_reported_usage(self):
        self.api.call.side_effect = [{'pause_state': 1}, self.start_response(), ApiError('offline')]
        self.manager.start()
        with self.assertRaises(ApiError):
            self.manager.refresh({'tcp_upload_bytes': 42})
        self.assertEqual(self.manager.reported_upload, 0)

    def test_failed_start_still_pauses_billing(self):
        self.api.call.side_effect = [{'pause_state': 0}, {'pause_state': 1}, ApiError('start failed'),
                                    {}, {'pause_state': 0}]
        with self.assertRaises(ApiError):
            self.manager.start()
        self.assertFalse(self.manager.active)
        self.assertFalse(self.manager.clock_owned)
        self.assertEqual(self.api.call.call_args_list[-1].args[0], '/client/member/suspend')

    def test_failed_stop_still_attempts_billing_pause(self):
        self.manager.active = self.manager.clock_owned = True
        self.api.call.side_effect = [ApiError('stop failed'), {'pause_state': 0}]
        with self.assertRaises(ApiError):
            self.manager.stop({})
        self.assertFalse(self.manager.clock_owned)
        self.assertTrue(self.manager.active)

    def test_uncertain_recovery_is_followed_by_pause(self):
        self.api.call.side_effect = [{'pause_state': 0}, ApiError('timeout after recovery'), {'pause_state': 0}]
        with self.assertRaises(ApiError):
            self.manager.start()
        self.assertEqual(self.api.call.call_args_list[-1].args[0], '/client/member/suspend')
        self.assertFalse(self.manager.clock_owned)


class LauncherPlanningTests(unittest.TestCase):
    def test_link_subnet_avoids_existing_specific_routes(self):
        subnet = select_subnet([{'dst': 'default'}, {'dst': '169.254.16.0/24'}, {'dst': '10.0.0.0/8'}])
        self.assertFalse(subnet.overlaps(__import__('ipaddress').ip_network('169.254.16.0/24')))
        self.assertEqual(subnet.prefixlen, 30)

    def test_all_link_addresses_occupied_does_not_override_host_routes(self):
        with self.assertRaises(ValueError):
            select_subnet([{'dst': '169.254.0.0/16'}])

    def test_tunnel_configuration_uses_udp_and_ipv4(self):
        configuration = hev_config('169.254.20.1', 1080)
        self.assertIn("udp: 'udp'", configuration)
        self.assertIn('mtu: 1400', configuration)
        self.assertNotIn('ipv6:', configuration)
