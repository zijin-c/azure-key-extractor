import os
import unittest
from unittest.mock import MagicMock, patch

from modules.pipeline import DynamicProxyBridge, _emit_traffic_summary
from utils.fingerprint import TrafficStats


class TrafficLogSummaryTests(unittest.TestCase):
    def setUp(self):
        self.bridge = DynamicProxyBridge(None)
        self.bridge.record_bytes(int(6.89 * 1048576), 'download', 'example.test')
        self.stats = TrafficStats(self.bridge)
        self.stats.record_transfer(int(4.61 * 1048576), 'https://example.test/api/data', 'fetch')
        self.stats.cache_hit_count = 425
        self.stats.cache_saved_bytes = int(13.9 * 1048576)
        self.stats.blocked_count = 18
        self.stats.telemetry_blocked_count = 41
        self.stats.cache_size_unknown_hits = 46
        self.stats.transfer_size_unknown_responses = 20
        self.stats.mfa_diag = [{'size': 1024, 'raw_len': 1024, 'enc': 'none', 'status': 200,
                               'post': '{"securityInfoType":3}', 'fields': 'SYNTHETIC_DETAIL'}]

    def test_default_prints_only_one_line_to_callback_and_logger(self):
        messages = []
        with patch.dict(os.environ):
            os.environ.pop('TRAFFIC_DIAGNOSTICS', None)
            with self.assertLogs('modules.pipeline', level='INFO') as logs:
                _emit_traffic_summary(self.stats, self.bridge, messages.append)
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('6.89 MB', messages[0])
        self.assertIn('425 次', messages[0])
        self.assertIn('13.90 MB', messages[0])
        self.assertIn('18 次', messages[0])
        self.assertIn('41 次', messages[0])
        for detail in ('Top', '[流量诊断]', '[2FA 诊断]', '请求体', '响应字段', 'SYNTHETIC_DETAIL'):
            self.assertNotIn(detail, messages[0])

    def test_disabled_does_not_build_diagnostic_reports_or_change_counters(self):
        before = (self.stats.net_transfer_bytes, self.stats.cache_saved_bytes,
                  self.stats.cache_hit_count, self.stats.transfer_size_unknown_responses,
                  self.bridge.get_total_bytes())
        self.stats.diagnostics.report_lines = MagicMock(side_effect=AssertionError('unexpected diagnostic'))
        with patch.dict(os.environ, {'TRAFFIC_DIAGNOSTICS': 'false'}), \
             patch('modules.pipeline.proxy_report_lines') as report:
            _emit_traffic_summary(self.stats, self.bridge, None)
        self.stats.diagnostics.report_lines.assert_not_called()
        report.assert_not_called()
        self.assertEqual(before, (self.stats.net_transfer_bytes, self.stats.cache_saved_bytes,
                                 self.stats.cache_hit_count, self.stats.transfer_size_unknown_responses,
                                 self.bridge.get_total_bytes()))

    def test_diagnostic_flag_accepts_common_values(self):
        for value in ('true', 'TRUE', '1', ' yes ', 'on'):
            with self.subTest(value=value), patch.dict(os.environ, {'TRAFFIC_DIAGNOSTICS': value}):
                messages = []
                _emit_traffic_summary(self.stats, self.bridge, messages.append)
                joined = '\n'.join(messages)
                self.assertIn('[流量诊断]', joined)
                self.assertIn('单次请求 Top 20', joined)
                self.assertIn('代理目标域名', joined)
                self.assertIn('[2FA 诊断]', joined)
        for value in ('false', '0', 'off', '', 'unexpected'):
            with self.subTest(value=value), patch.dict(os.environ, {'TRAFFIC_DIAGNOSTICS': value}):
                messages = []
                _emit_traffic_summary(self.stats, self.bridge, messages.append)
                self.assertEqual(len(messages), 1)

    def test_direct_mode_keeps_http_estimate_label(self):
        messages = []
        self.stats.set_proxy_bridge(None)
        with patch.dict(os.environ, {'TRAFFIC_DIAGNOSTICS': 'false'}):
            _emit_traffic_summary(self.stats, None, messages.append)
        self.assertEqual(len(messages), 1)
        self.assertIn('HTTP 传输估算约 4.61 MB', messages[0])

    def test_diagnostic_failure_does_not_remove_summary(self):
        messages = []
        self.stats.diagnostics.report_lines = MagicMock(side_effect=RuntimeError('synthetic failure'))
        with patch.dict(os.environ, {'TRAFFIC_DIAGNOSTICS': 'true'}), \
             self.assertLogs('modules.pipeline', level='WARNING') as logs:
            _emit_traffic_summary(self.stats, self.bridge, messages.append)
        self.assertIn('6.89 MB', messages[0])
        self.assertTrue(any('不影响业务' in message for message in logs.output))


if __name__ == '__main__':
    unittest.main()
