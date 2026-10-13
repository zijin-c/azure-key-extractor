import asyncio
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils import fingerprint as fp
from playwright.async_api import async_playwright
import config


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = os.path.abspath(directory.name)
        self.count = 0
        for name, value in {
            '_cache_warm_done': set(), '_cache_warm_inflight': {},
            '_cache_warm_retry_after': {},
        }.items():
            patcher = patch.object(fp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(fp.LocalHttpCache, 'CACHE_DIR', directory.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(fp, '_static_cache_count', side_effect=lambda: self.count)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_concurrent_accounts_share_one_direct_download(self):
        async def download(*args):
            await asyncio.sleep(.02)
            self.count = 12
        downloader = AsyncMock(side_effect=download)
        proxy = {'server': 'http://127.0.0.1:9999'}
        with patch.object(fp, '_download_public_cache', downloader):
            await asyncio.gather(*(fp.warmup_static_cache(object(), proxy) for _ in range(5)))
            await fp.warmup_static_cache(object(), proxy)
        self.assertEqual(downloader.await_count, 1)
        self.assertIsNone(downloader.await_args.args[1])
        self.assertIn(self.root, fp._cache_warm_done)
        self.assertFalse(fp._cache_warm_inflight)

    async def test_proxy_fallback_only_when_direct_adds_no_cache(self):
        proxy = {'server': 'http://127.0.0.1:9999'}
        async def download(pw, selected_proxy, exe):
            if selected_proxy:
                self.count = 8
        downloader = AsyncMock(side_effect=download)
        with patch.object(fp, '_download_public_cache', downloader):
            await fp.warmup_static_cache(object(), proxy)
        self.assertEqual([call.args[1] for call in downloader.await_args_list], [None, proxy])

    async def test_existing_cache_skips_warmup(self):
        self.count = 100
        with patch.object(fp, '_download_public_cache', AsyncMock()) as downloader:
            await fp.warmup_static_cache(object())
        downloader.assert_not_awaited()

    async def test_failure_does_not_fail_account_and_is_rate_limited(self):
        with patch.object(fp, '_download_public_cache', AsyncMock(side_effect=OSError('offline'))) as downloader:
            await fp.warmup_static_cache(object())
            await fp.warmup_static_cache(object())
            self.assertEqual(downloader.await_count, 1)
            self.assertNotIn(self.root, fp._cache_warm_done)
            fp._cache_warm_retry_after[self.root] = 0
            await fp.warmup_static_cache(object())
            self.assertEqual(downloader.await_count, 2)

    async def test_cancelled_waiter_does_not_cancel_shared_download(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def download(*args):
            started.set()
            await release.wait()
            self.count = 3
        with patch.object(fp, '_download_public_cache', side_effect=download):
            owner = asyncio.create_task(fp.warmup_static_cache(object()))
            await started.wait()
            waiter = asyncio.create_task(fp.warmup_static_cache(object()))
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            await owner
        self.assertIn(self.root, fp._cache_warm_done)

    async def test_cancelled_owner_releases_waiters(self):
        started = asyncio.Event()
        async def download(*args):
            started.set()
            await asyncio.Event().wait()
        with patch.object(fp, '_download_public_cache', side_effect=download):
            owner = asyncio.create_task(fp.warmup_static_cache(object()))
            await started.wait()
            waiter = asyncio.create_task(fp.warmup_static_cache(object()))
            await asyncio.sleep(0)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner
            await asyncio.wait_for(waiter, 1)
        self.assertFalse(fp._cache_warm_inflight)

    async def test_public_loader_closes_browser_and_never_submits_credentials(self):
        page = SimpleNamespace(goto=AsyncMock(), wait_for_load_state=AsyncMock())
        ctx = SimpleNamespace(new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=ctx), close=AsyncMock())
        launch = AsyncMock(return_value=browser)
        pw = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        with patch.object(fp, 'setup_save_data_route', AsyncMock()):
            await fp._download_public_cache(pw, None, None)
        self.assertEqual(page.goto.await_count, 2)
        self.assertIsNone(launch.await_args.kwargs['proxy'])
        self.assertNotIn('ignore_https_errors', browser.new_context.await_args.kwargs)
        browser.close.assert_awaited_once()


class SlowCacheSharingTest(unittest.IsolatedAsyncioTestCase):
    async def test_nine_second_download_is_shared_by_three_browsers(self):
        downloaded = []
        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path.endswith('.js'):
                    downloaded.append(self.path)
                    body, content_type = b'window.loaded=true;', 'application/javascript'
                    delay = 9
                else:
                    body, content_type = b'<script src="/asset.12345678.js"></script>', 'text/html'
                    delay = 0
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                time.sleep(delay)
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stats_list = []
        try:
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(fp.LocalHttpCache, 'CACHE_DIR', directory), \
                 patch.object(fp.LocalHttpCache, 'CANONICAL_DIR', directory + '/canonical'), \
                 patch.object(fp.LocalHttpCache, '_canonical_initialized', True):
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True, executable_path=config.CHROMIUM_PATH)
                    try:
                        async def load():
                            ctx = await browser.new_context()
                            try:
                                stats = fp.TrafficStats()
                                stats_list.append(stats)
                                await fp.setup_save_data_route(ctx, stats)
                                page = await ctx.new_page()
                                await page.goto(f'http://127.0.0.1:{server.server_port}', timeout=30000)
                                self.assertTrue(await page.evaluate('window.loaded'))
                            finally:
                                await ctx.close()
                        await asyncio.gather(*(load() for _ in range(3)))
                    finally:
                        await browser.close()
            self.assertEqual(len(downloaded), 1)
            self.assertEqual(sum(stats.cache_hit_count for stats in stats_list), 2)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
