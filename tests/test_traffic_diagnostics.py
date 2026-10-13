import asyncio
import gzip
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

import config
from modules.pipeline import DynamicProxyBridge
from utils.fingerprint import TrafficStats, setup_save_data_route
from utils.http_cache import LocalHttpCache
from utils.traffic_diagnostics import proxy_report_lines, safe_traffic_url


def response_pair(headers=None, sizes=None):
    request = SimpleNamespace(
        url='https://example.test/api/data?token=SECRET#secret', headers={},
        post_data_buffer=None, method='GET', resource_type='fetch', failure='net::ERR_ABORTED',
        sizes=AsyncMock(return_value=sizes or {
            'requestBodySize': 0, 'requestHeadersSize': 300,
            'responseBodySize': 1048576, 'responseHeadersSize': 150,
        }),
    )
    response = SimpleNamespace(request=request, url=request.url, headers=headers or {}, status=200)
    return request, response


class SizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_response_is_reconciled_once(self):
        stats = TrafficStats()
        request, response = response_pair()
        stats.record_response(response)
        stats.record_response(response)
        self.assertEqual(len(stats.network_requests), 1)
        self.assertEqual(stats.transfer_size_unknown_responses, 1)
        stats.diagnostics._finished(request)
        await stats.diagnostics.drain()
        self.assertEqual(stats.net_transfer_bytes, 1049026)
        self.assertEqual(stats.transfer_size_unknown_responses, 0)
        self.assertEqual(stats.diagnostics.recovered_count, 1)
        stats.diagnostics._finished(request)
        self.assertEqual(len(stats.network_requests), 1)
        self.assertNotIn('SECRET', repr(stats.network_requests))

    async def test_synthetic_response_does_not_measure_replayed_cache(self):
        stats = TrafficStats()
        request, response = response_pair({'content-length': '120'})
        stats.record_response(response, native=False)
        before = stats.net_transfer_bytes
        stats.diagnostics.mark_synthetic(request)
        stats.diagnostics._finished(request)
        await stats.diagnostics.drain()
        request.sizes.assert_not_awaited()
        self.assertEqual(stats.net_transfer_bytes, before)
        self.assertFalse(stats.diagnostics.pending)
        request2, _ = response_pair()
        stats.diagnostics.mark_synthetic(request2)
        stats.diagnostics._finished(request2)
        self.assertEqual(len(stats.network_requests), 1)

    async def test_measurement_failure_keeps_unknown_and_estimate(self):
        stats = TrafficStats()
        request, response = response_pair()
        request.sizes.side_effect = RuntimeError('closed')
        stats.record_response(response)
        before = stats.net_transfer_bytes
        stats.diagnostics._finished(request)
        await stats.diagnostics.drain()
        self.assertEqual(stats.net_transfer_bytes, before)
        self.assertEqual(stats.transfer_size_unknown_responses, 1)
        self.assertEqual(stats.diagnostics.measurement_errors, 1)
        self.assertNotIn('SECRET', '\n'.join(stats.diagnostics.report_lines()))

    async def test_failed_requests_and_local_blocks_are_distinguished(self):
        stats = TrafficStats()
        local, _ = response_pair()
        stats.diagnostics.mark_synthetic(local)
        stats.diagnostics._failed(local)
        native, _ = response_pair()
        stats.diagnostics._failed(native)
        stats.diagnostics.fetch_failed(native, TimeoutError())
        self.assertEqual(len(stats.diagnostics.failures), 1)
        self.assertEqual(len(stats.diagnostics.fetch_failures), 1)
        self.assertNotIn('SECRET', repr(stats.diagnostics.failures))

    async def test_top_five_remainder_and_repeated_paths(self):
        stats = TrafficStats()
        for i in range(7):
            stats.record_transfer(1048576, f'https://example.test/api/data?token={i}', 'fetch')
        report = '\n'.join(stats.diagnostics.report_lines())
        self.assertIn('Top 5 之外 2.00 MB', report)
        self.assertIn('7 次 https://example.test/api/data', report)
        self.assertNotIn('token=', report)


class BridgeTests(unittest.TestCase):
    def test_domain_counters_and_snapshots_are_consistent(self):
        bridge = DynamicProxyBridge(None)
        bridge.record_connection('a.test')
        bridge.record_bytes(20, 'upload', 'a.test')
        bridge.record_bytes(100, 'download', 'a.test')
        bridge.record_connection('a.test', failed=True)
        bridge.record_bytes(40, 'download', 'b.test')
        rows = bridge.get_domain_stats()
        self.assertEqual(sum(r['upload_bytes'] + r['download_bytes'] for r in rows), bridge.get_total_bytes())
        rows[0]['upload_bytes'] = 999
        self.assertEqual(bridge.get_domain_stats()[0]['upload_bytes'], 20)
        self.assertEqual(bridge.get_domain_stats()[0]['failed_connections'], 1)
        self.assertIn('不是纯协议开销', '\n'.join(proxy_report_lines(bridge, 100)))

    def test_connection_failure_is_reported_without_exposing_proxy_credentials(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.close()
        bridge = DynamicProxyBridge(f'http://username:SECRET@127.0.0.1:{port}')
        try:
            bridge.start()
            with socket.create_connection(('127.0.0.1', bridge.local_port), timeout=3) as client:
                client.sendall(b'CONNECT example.test:443 HTTP/1.1\r\nHost: example.test\r\n\r\n')
                self.assertIn(b'502', client.recv(4096))
            row = bridge.get_domain_stats()[0]
            self.assertEqual(row['host'], 'example.test')
            self.assertEqual(row['connections'], 1)
            self.assertEqual(row['failed_connections'], 1)
            self.assertNotIn('SECRET', '\n'.join(proxy_report_lines(bridge, 0)))
        finally:
            bridge.stop()

    def test_url_redaction(self):
        self.assertEqual(safe_traffic_url('https://user:SECRET@example.test/a?token=SECRET#SECRET'),
                         'https://example.test/a')
        self.assertNotIn('name@example.com', safe_traffic_url('https://example.test/name@example.com'))


class BrowserSizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_compressed_and_cached_responses_with_and_without_proxy(self):
        payload = b'x' * 1048576
        compressed = gzip.compress(payload)
        script = b'window.cachedAsset=true;'
        class Origin(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ('/api/data', '/api/gzip'):
                    body = payload if path == '/api/data' else compressed
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Transfer-Encoding', 'chunked')
                    if path == '/api/gzip':
                        self.send_header('Content-Encoding', 'gzip')
                    self.end_headers()
                    self.wfile.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n\r\n')
                else:
                    body = script if path.endswith('.js') else b'<html></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/javascript' if path.endswith('.js') else 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                self.wfile.flush()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            async with async_playwright() as pw:
                totals = []
                for use_proxy in (False, True):
                    with self.subTest(proxy=use_proxy), tempfile.TemporaryDirectory() as root, \
                         patch.object(LocalHttpCache, 'CACHE_DIR', root), \
                         patch.object(LocalHttpCache, 'CANONICAL_DIR', root + '/canonical'), \
                         patch.object(LocalHttpCache, '_canonical_initialized', True):
                        bridge = DynamicProxyBridge(None) if use_proxy else None
                        if bridge:
                            bridge.start()
                        browser = await pw.chromium.launch(
                            headless=True, executable_path=config.get_chromium_path(),
                            args=['--disable-background-networking'],
                            proxy={'server': f'http://127.0.0.1:{bridge.local_port}', 'bypass': '<-loopback>'} if bridge else None,
                        )
                        try:
                            ctx = await browser.new_context()
                            stats = TrafficStats(bridge)
                            await setup_save_data_route(ctx, stats)
                            page = await ctx.new_page()
                            await page.goto(f'http://127.0.0.1:{server.server_port}/')
                            for path in ('/api/data', '/api/gzip'):
                                length = await page.evaluate('async path => (await (await fetch(path)).arrayBuffer()).byteLength', path)
                                self.assertEqual(length, len(payload))
                            await page.evaluate("async () => { for(let i=0;i<2;i++) await (await fetch('/asset.12345678.js')).text(); }")
                            await page.wait_for_timeout(100)
                            await stats.diagnostics.drain()
                            data = [row[0] for row in stats.network_requests if row[1].endswith('/api/data')]
                            zipped = [row[0] for row in stats.network_requests if row[1].endswith('/api/gzip')]
                            cached = [row for row in stats.network_requests if row[1].endswith('.js')]
                            self.assertGreaterEqual(data[0], len(payload))
                            self.assertLess(zipped[0], 5000)  # 压缩传输，而不是 1 MB 解压体积。
                            self.assertEqual(stats.transfer_size_unknown_responses, 0)
                            self.assertEqual(stats.diagnostics.recovered_count, 2)
                            self.assertEqual(len(cached), 1)
                            self.assertEqual(stats.cache_hit_count, 1)
                            totals.append(stats.net_transfer_bytes)
                            if bridge:
                                local_row = next(row for row in bridge.get_domain_stats() if row['host'] == '127.0.0.1')
                                self.assertGreater(local_row['download_bytes'], len(payload))
                                self.assertGreater(local_row['upload_bytes'], 0)
                                self.assertIn('Top 5 之外', '\n'.join(stats.diagnostics.report_lines()))
                                self.assertIn('127.0.0.1', '\n'.join(proxy_report_lines(bridge, stats.net_transfer_bytes)))
                        finally:
                            await browser.close()
                            if bridge:
                                bridge.stop()
                self.assertLess(abs(totals[0] - totals[1]), 10240)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
