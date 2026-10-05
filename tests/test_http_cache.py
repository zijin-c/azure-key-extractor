import asyncio
import gzip
import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from utils.http_cache import LocalHttpCache as Cache
from utils.fingerprint import TrafficStats, setup_save_data_route

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = b"window.loadedVersion=1; /*" + b"X" * 200000 + b"*/"
COMPRESSED = gzip.compress(SCRIPT)
MANIFEST = json.dumps({"manifest": {"Microsoft_Azure_Education": {"assetTypes": {"version": 1}}}}).encode()


class CacheFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in {
            "CACHE_DIR": self.temp.name,
            "CANONICAL_DIR": str(Path(self.temp.name) / "canonical"),
            "HASH_MAP_FILE": str(Path(self.temp.name) / "canonical" / "hashes.json"),
            "_canonical_initialized": True,
            "_manifest_hash_to_type": {},
        }.items():
            patcher = patch.object(Cache, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class CacheTests(CacheFixture, unittest.TestCase):
    def test_versions_do_not_collide(self):
        Cache.put("https://static.test/main.js?v=1", SCRIPT, {"content-type": "application/javascript"})
        self.assertIsNone(Cache.get("https://static.test/main.js?v=2"))
        self.assertEqual(Cache.get("https://static.test/main.js?v=1")[0], SCRIPT)

    def test_legacy_query_cache_only_matches_its_original_url(self):
        import hashlib
        key = hashlib.sha256(b"https://static.test/main.js").hexdigest()
        Path(self.temp.name, key + ".body").write_bytes(SCRIPT)
        Path(self.temp.name, key + ".meta").write_text(json.dumps({"url": "https://static.test/main.js?v=1", "headers": {"content-type": "application/javascript"}}))
        self.assertEqual(Cache.get("https://static.test/main.js?v=1")[0], SCRIPT)
        self.assertIsNone(Cache.get("https://static.test/main.js?v=2"))
        self.assertIsNone(Cache.get_wire_size("https://static.test/main.js?v=1"))

    def test_compressed_size_is_saved_separately(self):
        url = "https://static.test/main.12345678.js"
        Cache.put(url, SCRIPT, {"content-encoding": "gzip", "content-length": str(len(COMPRESSED))})
        body, headers, _ = Cache.get(url)
        self.assertEqual(body, SCRIPT)
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(Cache.get_wire_size(url), len(COMPRESSED))

    def test_private_response_and_auth_api_are_not_cached(self):
        self.assertFalse(Cache.is_cacheable("https://mysignins.microsoft.com/api/account.json"))
        Cache.put("https://static.test/app.js", SCRIPT, {"cache-control": "no-store"})
        self.assertIsNone(Cache.get("https://static.test/app.js"))
        Cache.put("https://static.test/app.js", SCRIPT, {"set-cookie": "synthetic=1"})
        self.assertIsNone(Cache.get("https://static.test/app.js"))

    def test_manifest_cannot_be_replayed_for_new_hash_without_validation(self):
        old = "https://portal.test/ExtensionManifest/old.json?m_type=assetTypes"
        new = "https://portal.test/ExtensionManifest/new.json?m_type=assetTypes"
        Cache.save_canonical_manifest(old, MANIFEST, {"etag": '"same"'})
        self.assertEqual(Cache.get_canonical_manifest(old)[0], MANIFEST)
        self.assertIsNone(Cache.get_canonical_manifest(new))
        self.assertIsNotNone(Cache.manifest_candidate(new))
        self.assertIsNone(Cache.manifest_candidate(new.replace("portal.test", "other.test")))

    def test_unversioned_resource_expires_but_hashed_resource_survives(self):
        for url in ("https://static.test/app.js", "https://static.test/app.abcdef1234.js"):
            Cache.put(url, SCRIPT, {})
        with patch("utils.http_cache.time.time", return_value=time.time() + 90000):
            self.assertIsNone(Cache.get("https://static.test/app.js"))
            self.assertIsNotNone(Cache.get("https://static.test/app.abcdef1234.js"))

    def test_unknown_wire_size_is_not_reported_as_saved_bytes(self):
        stats = TrafficStats()
        stats.record_cache_hit(len(SCRIPT))
        stats.record_blocked(5000000)
        self.assertEqual(stats.cache_saved_bytes, 0)
        self.assertEqual(stats.cache_size_unknown_hits, 1)

    def test_xray_without_byte_counter_uses_http_estimate(self):
        stats = TrafficStats(SimpleNamespace())
        self.assertEqual(stats.transfer_source(), "HTTP 传输估算")

    def test_old_manifest_unknown_size_remains_unknown_after_alias(self):
        url = "https://portal.test/ExtensionManifest/old.json?m_type=assetTypes"
        Cache.save_canonical_manifest(url, MANIFEST, {"etag": '"same"'}, wire_body_bytes=None)
        self.assertIsNone(Cache.get_wire_size(url))


class RoutingTests(CacheFixture, unittest.IsolatedAsyncioTestCase):
    async def handler(self):
        class Context:
            async def route(self, pattern, handler):
                self.handler = handler
            def on(self, event, handler):
                pass
        ctx = Context()
        self.stats = TrafficStats()
        await setup_save_data_route(ctx, self.stats)
        return ctx.handler

    def request(self, url, method="GET"):
        return SimpleNamespace(url=url, resource_type="script", method=method, headers={}, post_data_buffer=None, is_navigation_request=lambda: False)

    def route(self, url, status=200, body=SCRIPT, headers=None):
        response = SimpleNamespace(url=url, status=status, headers=headers or {"content-length": str(len(body))}, body=AsyncMock(return_value=body))
        return SimpleNamespace(continue_=AsyncMock(), fetch=AsyncMock(return_value=response), fulfill=AsyncMock())

    async def test_mysignins_public_script_uses_cache(self):
        url = "https://mysignins.microsoft.com/bundle/main.12345678.js"
        Cache.put(url, SCRIPT, {})
        handler, route = await self.handler(), self.route(url)
        await handler(route, self.request(url))
        route.fulfill.assert_awaited_once()
        route.continue_.assert_not_awaited()
        route.fetch.assert_not_awaited()

    async def test_mfa_post_bypasses_cache(self):
        url = "https://mysignins.microsoft.com/api/authenticationmethods/initializemobileapp"
        handler, route = await self.handler(), self.route(url)
        await handler(route, self.request(url, "POST"))
        route.continue_.assert_awaited_once()
        route.fetch.assert_not_awaited()

    async def test_etag_304_reuses_manifest_and_then_needs_no_request(self):
        old = "https://portal.test/ExtensionManifest/old.json?m_type=assetTypes"
        new = "https://portal.test/ExtensionManifest/new.json?m_type=assetTypes"
        Cache.save_canonical_manifest(old, MANIFEST, {"etag": '"same"', "content-length": "50", "content-encoding": "gzip"})
        handler, route = await self.handler(), self.route(new, 304, b"", {"etag": '"same"'})
        await handler(route, self.request(new))
        self.assertEqual(route.fetch.call_args.kwargs["headers"]["If-None-Match"], '"same"')
        self.assertEqual(route.fulfill.call_args.kwargs["body"], MANIFEST)
        self.assertEqual(Cache.get_canonical_manifest(new)[0], MANIFEST)
        self.assertEqual(Cache.get_wire_size(new), 50)
        self.assertGreater(self.stats.get_transfer_bytes(), 0)
        subsequent = self.route(new)
        await handler(subsequent, self.request(new))
        subsequent.fetch.assert_not_awaited()

    async def test_etag_change_downloads_new_manifest(self):
        old = "https://portal.test/ExtensionManifest/old.json?m_type=assetTypes"
        new = "https://portal.test/ExtensionManifest/new.json?m_type=assetTypes"
        fresh = MANIFEST.replace(b'"version": 1', b'"version": 2')
        Cache.save_canonical_manifest(old, MANIFEST, {"etag": '"old"'})
        handler, route = await self.handler(), self.route(new, 200, fresh, {"etag": '"new"', "content-length": str(len(fresh))})
        await handler(route, self.request(new))
        self.assertEqual(route.fulfill.call_args.kwargs["body"], fresh)
        self.assertEqual(Cache.get(new)[0], fresh)

    async def test_disk_write_error_does_not_download_again(self):
        url = "https://static.test/new.js"
        handler, route = await self.handler(), self.route(url)
        with patch.object(Cache, "put", side_effect=OSError("synthetic disk error")):
            await handler(route, self.request(url))
        route.fetch.assert_awaited_once()
        route.fulfill.assert_awaited_once()
        route.continue_.assert_not_awaited()

    async def test_cancelled_waiter_releases_download_registry(self):
        async with Cache.download_lock("https://static.test/a.js"):
            async def wait():
                async with Cache.download_lock("https://static.test/a.js"):
                    self.fail("cancelled waiter acquired lock")
            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(Cache._fills)


class BrowserCacheTests(CacheFixture, unittest.IsolatedAsyncioTestCase):
    async def test_compressed_cold_parallel_and_warm_cache(self):
        counts = Counter()
        guard = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with guard:
                    counts[self.path] += 1
                if self.path.startswith("/asset.js"):
                    time.sleep(0.15)
                    body, kind = COMPRESSED, "application/javascript"
                else:
                    body, kind = b'<script src="/asset.js?v=1"></script>', "text/html"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                if kind == "application/javascript":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            async with async_playwright() as pw:
                candidates = list((ROOT / "ms-playwright").glob("chromium-*/chrome-win64/chrome.exe"))
                browser = await pw.chromium.launch(headless=True, **({"executable_path": str(candidates[0])} if candidates else {}))
                try:
                    async def load():
                        ctx = await browser.new_context()
                        stats = TrafficStats()
                        await setup_save_data_route(ctx, stats)
                        page = await ctx.new_page()
                        await page.goto(f"http://127.0.0.1:{server.server_port}")
                        self.assertEqual(await page.evaluate("window.loadedVersion"), 1)
                        await ctx.close()
                        return stats
                    first, second = await asyncio.gather(load(), load())
                    self.assertEqual(counts["/asset.js?v=1"], 1)
                    self.assertLess(first.get_transfer_bytes() + second.get_transfer_bytes(), 10000)
                    warm = await load()
                    self.assertEqual(counts["/asset.js?v=1"], 1)
                    self.assertEqual(warm.cache_hit_count, 1)
                    self.assertEqual(warm.cache_saved_bytes, len(COMPRESSED))
                finally:
                    await browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
