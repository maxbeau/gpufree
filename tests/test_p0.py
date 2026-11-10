"""P0 regressions: all API and clock interactions are mocked."""
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from gpufree.client import GPUFeeClient, PlatformError
from gpufree.cli import main, _print_instances


ITEM = {
    'webide_instance_id': 12, 'webide_instance_uuid': 'uuid-12',
    'webide_instance_name': 'worker', 'nick_name': '',
    'product_name': 'RTX 4090 / 24GB', 'data_center_id': 1,
    'data_center_name': 'test-dc', 'status': 5, 'start_mode': 'gpu',
    'charge_type': 0, 'ssh_command': 'ssh test', 'ssh_password': 'SECRET-PASSWORD',
    'access_token': 'SECRET-TOKEN', 'local_disk_info': {'root_fs_used_rate': '42%'},
}
PRODUCTS = [
    {'product_name': 'RTX 4090 / 24GB', 'current_price': 138, 'sold_avalible': 1,
     'product_skus': [{'datacenter_id': 1, 'gpu_nodes_left': 0, 'product_sku_code': 'a'},
                      {'datacenter_id': 2, 'gpu_nodes_left': 3, 'product_sku_code': 'b'}]},
    {'product_name': 'L40 / 48G', 'current_price': 200, 'sold_avalible': 0,
     'product_skus': [{'datacenter_id': 1, 'gpu_nodes_left': 0}]},
    {'product_name': 'L40S / 48G', 'current_price': 210, 'sold_avalible': 1,
     'product_skus': [{'datacenter_id': 1, 'gpu_nodes_left': 2}]},
]


class P0Tests(unittest.TestCase):
    def setUp(self):
        # Bypass construction entirely: no credential paths can be read.
        self.client = object.__new__(GPUFeeClient)
        self.network = patch('gpufree.client.urllib.request.urlopen', side_effect=AssertionError('unmocked network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['gpufree', *argv]), patch('gpufree.cli.GPUFeeClient', wraps=GPUFeeClient, return_value=self.client), redirect_stdout(out), redirect_stderr(err):
            code = main()
        return code, out.getvalue(), err.getvalue()

    def test_control_codes(self):
        for method in (self.client.start_instance, self.client.stop_instance):
            for code in (200, '200'):
                with patch.object(self.client, '_request', return_value={'code': code}):
                    self.assertEqual(method(12, 'uuid'), {'code': code})
            for code, hint in ((53523, 'gpufree stock'), ('53524', 'gpufree instances'), (999, 'raw message'), (None, 'raw message')):
                with self.subTest(method=method, code=code), patch.object(self.client, '_request', return_value={'code': code, 'msg': 'raw message'}):
                    with self.assertRaises(PlatformError) as raised:
                        method(12, 'uuid')
                    self.assertEqual(raised.exception.code, code)
                    self.assertEqual(raised.exception.msg, 'raw message')
                    self.assertIn(hint, str(raised.exception))

    def test_other_commands_preserve_business_error_payload(self):
        from unittest.mock import MagicMock
        payload = {'code': 999, 'msg': 'original platform message'}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with patch.object(self.client, '_ensure_access_token', return_value='fixture-token'), patch('gpufree.client.urllib.request.urlopen', return_value=response):
            self.assertEqual(self.client.list_products(), payload)

    def test_stock_wait_success_uses_gpu_then_state_wait(self):
        final = dict(ITEM, status=3)
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, 'wait_for_gpu_stock', return_value=True) as stock, patch.object(self.client, 'start_instance', return_value={'code': 200}) as start, patch.object(self.client, '_wait_for_instance', return_value=final) as wait:
            code, out, err = self.cli(['start', '--when-available', '--fallback', 'nogpu', '--wait', '--wait-timeout', '600'])
            self.assertEqual((code, err), (0, ''))
            stock.assert_called_once_with(ITEM, 600)
            start.assert_called_once_with(12, 'uuid-12', start_mode='gpu')
            wait.assert_called_once_with(ITEM, 3, 600)
            self.assertNotIn('已降级', out)
            self.assertNotIn('SECRET-PASSWORD', out)
            self.assertNotIn('SECRET-TOKEN', out)

    def test_fallback_failure_is_not_retried_or_waited(self):
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, 'gpu_stock_for_instance', return_value=1), patch.object(self.client, 'start_instance', side_effect=[PlatformError(53523, 'gpu'), PlatformError(53524, 'nogpu')]) as start, patch.object(self.client, '_wait_for_instance') as wait:
            code, out, err = self.cli(['start', '--fallback', 'nogpu', '--wait'])
            self.assertEqual(code, 1)
            self.assertEqual(start.call_count, 2)
            self.assertEqual(out, '')
            self.assertIn('53524', err)
            wait.assert_not_called()

    def test_control_failure_does_not_wait_or_confirm(self):
        for action in ('start', 'stop'):
            with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, '_request', return_value={'code': 53524, 'msg': 'bad state'}), patch.object(self.client, '_wait_for_instance') as wait:
                code, out, err = self.cli([action, '--wait'])
                self.assertEqual(code, 1)
                self.assertEqual(out, '')
                self.assertIn('53524', err)
                wait.assert_not_called()

    def test_stock_matching_and_datacenter_preference(self):
        with patch.object(self.client, 'list_products', return_value={'code': 200, 'data': PRODUCTS}):
            self.assertEqual(self.client.stock_products('l40'), PRODUCTS[1:])
            self.assertEqual(self.client.stock_products('L40', available=True), PRODUCTS[2:])
            self.assertEqual(self.client.gpu_stock_for_instance(ITEM), 0)
            self.assertEqual(self.client.gpu_stock_for_instance(dict(ITEM, data_center_id=99)), 3)
            with self.assertRaisesRegex(RuntimeError, 'No stock product'):
                self.client.gpu_stock_for_instance(dict(ITEM, product_name='missing'))
            code, out, _ = self.cli(['stock', '4090'])
            self.assertEqual(code, 0)
            self.assertIn('¥1.38/h\t3\tdc2:3', out)
            self.assertNotIn('dc1:0', out)
            code, out, _ = self.cli(['stock', 'l40', '--available', '--json'])
            self.assertEqual(json.loads(out), PRODUCTS[2:])
            code, out, _ = self.cli(['stock', 'L40 /'])
            self.assertIn('¥2.00/h\t0\t0', out)

    def test_stock_polling(self):
        with patch.object(self.client, 'gpu_stock_for_instance', side_effect=[0, 1]), patch('gpufree.client.time.monotonic', return_value=0), patch('gpufree.client.time.sleep') as sleep:
            self.assertTrue(self.client.wait_for_gpu_stock(ITEM, 120))
            sleep.assert_called_once_with(30)
        with patch.object(self.client, 'gpu_stock_for_instance', return_value=0), patch('gpufree.client.time.monotonic', side_effect=[0, 120]), patch('gpufree.client.time.sleep') as sleep:
            self.assertFalse(self.client.wait_for_gpu_stock(ITEM, 120))
            sleep.assert_not_called()

    def test_fallback_cases(self):
        for flags, stock, wait_result, failure in (
            (['--fallback', 'nogpu'], 0, None, False),
            (['--fallback', 'nogpu'], 1, None, True),
            (['--when-available', '--fallback', 'nogpu'], 0, False, False),
            (['--when-available', '--fallback', 'nogpu'], 0, True, True),
        ):
            with self.subTest(flags=flags, failure=failure), patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, 'gpu_stock_for_instance', return_value=stock) as lookup, patch.object(self.client, 'wait_for_gpu_stock', return_value=wait_result) as waiting, patch.object(self.client, 'start_instance', side_effect=[PlatformError(53523, 'no stock'), {'code': 200}] if failure else [{'code': 200}]) as start:
                code, out, err = self.cli(['start', *flags])
                self.assertEqual((code, err), (0, ''))
                self.assertIn('已降级为无卡模式', out)
                self.assertEqual([call.kwargs['start_mode'] for call in start.call_args_list], ['gpu', 'nogpu'] if failure else ['nogpu'])
                if '--when-available' in flags:
                    waiting.assert_called_once_with(ITEM, 120)
                    lookup.assert_not_called()

    def test_stock_timeout_and_non_stock_errors(self):
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, 'wait_for_gpu_stock', return_value=False), patch.object(self.client, 'start_instance') as start:
            code, _, err = self.cli(['start', '--when-available'])
            self.assertEqual(code, 1)
            self.assertIn('Timed out', err)
            start.assert_not_called()
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, 'gpu_stock_for_instance', return_value=1), patch.object(self.client, 'start_instance', side_effect=PlatformError(999, 'original')) as start:
            code, _, err = self.cli(['start', '--fallback', 'nogpu'])
            self.assertEqual(code, 1)
            self.assertIn('original', err)
            self.assertEqual(start.call_count, 1)

    def test_nogpu_ignores_flags(self):
        with patch.object(self.client, 'start_instance_by_selector', return_value={'code': 200}) as start, patch.object(self.client, 'gpu_stock_for_instance') as lookup:
            code, out, _ = self.cli(['start', '--mode', 'nogpu', '--fallback', 'nogpu', '--when-available'])
            self.assertEqual(code, 0)
            self.assertIn('忽略', out)
            lookup.assert_not_called()
            self.assertEqual(start.call_args.kwargs['start_mode'], 'nogpu')

    def test_watch_changes_only_and_unlimited(self):
        states = [dict(ITEM, status=s) for s in (1, 1, 3)]
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, '_instance_items', side_effect=[[item] for item in states]), patch('gpufree.client.time.sleep') as sleep:
            code, out, err = self.cli(['watch'])
            self.assertEqual((code, err), (0, ''))
            self.assertEqual(len(out.splitlines()), 2)
            self.assertIn('排队中', out)
            self.assertIn('运行中', out)
            self.assertEqual(sleep.call_count, 2)
            sleep.assert_called_with(10)

    def test_watch_timeout_disappearance_and_validation(self):
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, '_instance_items', return_value=[ITEM]), patch('gpufree.client.time.monotonic', side_effect=[0, 10]):
            code, _, err = self.cli(['watch', '--timeout', '10'])
            self.assertEqual(code, 1)
            self.assertIn('last seen status: 5', err)
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, '_instance_items', return_value=[]):
            code, _, err = self.cli(['watch'])
            self.assertEqual(code, 1)
            self.assertIn('disappeared', err)
        with self.assertRaises(ValueError):
            self.client._wait_for_instance(ITEM, 3, 0, interval=0)

    def test_watch_stopped_and_custom_interval(self):
        with patch.object(self.client, 'resolve_instance', return_value=ITEM), patch.object(self.client, '_instance_items', side_effect=[[dict(ITEM, status=3)], [ITEM]]), patch('gpufree.client.time.sleep') as sleep:
            code, out, _ = self.cli(['watch', 'worker', '--until', 'stopped', '--interval', '2'])
            self.assertEqual(code, 0)
            self.assertIn('已关机', out)
            sleep.assert_called_once_with(2)

    def test_instance_human_allowlist_and_raw_json(self):
        item = dict(ITEM, release_time=(datetime.now(timezone.utc) + timedelta(days=6, hours=4)).isoformat())
        with patch.object(self.client, 'list_instances', return_value={'code': 200, 'data': {'dataList': [item]}}):
            code, out, _ = self.cli(['instances'])
            self.assertEqual(code, 0)
            for value in ('worker', 'uuid-12', 'RTX 4090', 'test-dc', '已关机', 'gpu', '按量付费', 'ssh test', '42%', '剩 6d', item['release_time']):
                self.assertIn(value, out)
            for secret in ('SECRET-PASSWORD', 'SECRET-TOKEN', 'ssh_password', 'access_token'):
                self.assertNotIn(secret, out)
            _, raw, _ = self.cli(['instances', '--json'])
            self.assertEqual(json.loads(raw)['data']['dataList'], [item])
        out = io.StringIO()
        with redirect_stdout(out):
            _print_instances([dict(ITEM, status=99, charge_type=8, local_disk_info=None, release_time='2000-01-01T00:00:00Z')])
        self.assertIn('已过期', out.getvalue())
        self.assertIn('状态: 99', out.getvalue())
        self.assertIn('计费: 8', out.getvalue())
        self.assertNotIn('根盘使用率', out.getvalue())

    def test_summary_fields(self):
        item = dict(ITEM, release_time='2030-01-01T00:00:00Z')
        with patch.object(self.client, '_ensure_access_token'), patch.object(self.client, 'get_userinfo', return_value={}), patch.object(self.client, 'get_balance_overview', return_value={}), patch.object(self.client, 'list_instances', return_value={'data': {'dataList': [item, ITEM]}}), patch.object(self.client, 'get_dashboard_instances', return_value={}), patch.object(self.client, 'get_unread_count', return_value={}):
            summary = self.client.get_status()['instances_summary']
        self.assertEqual(summary[0]['status'], 5)
        self.assertEqual(summary[0]['status_text'], '已关机')
        self.assertEqual(summary[0]['start_mode'], 'gpu')
        self.assertEqual(summary[0]['release_time'], item['release_time'])
        self.assertNotIn('release_time', summary[1])


if __name__ == '__main__':
    unittest.main()
