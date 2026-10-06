import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from utils.fingerprint import new_fingerprint_context


ROOT = Path(__file__).resolve().parents[1]
SECRET = "JBSWY3DPEHPK3PXP"


class NativeBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(dict(self.headers)).encode() if self.path == "/headers" else b"<html></html>"
                if "initializemobileapp" in self.path:
                    body = json.dumps({"SecretKey": SECRET}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json" if self.path != "/" else "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.pw = await async_playwright().start()
        self.browsers = []
        candidates = list((ROOT / "ms-playwright").glob("chromium-*/chrome-win64/chrome.exe"))
        self.exe = str(candidates[0]) if candidates else None

    async def asyncTearDown(self):
        for browser in self.browsers:
            await browser.close()
        await self.pw.stop()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()

    async def launch(self):
        with patch("requests.get", side_effect=AssertionError("unexpected external GeoIP request")):
            result = await new_fingerprint_context(self.pw, True, None, self.exe, slow_mo=0, enable_save_data=False)
        self.browsers.append(result[0])
        return result

    async def test_native_page_worker_headers_and_canvas(self):
        browser, ctx, fp, stats = await self.launch()
        self.assertEqual(ctx.pages, [])
        page = await ctx.new_page()
        await page.goto(self.url)
        native = await page.evaluate("""async () => {
            const canvas = document.createElement('canvas');
            const c = canvas.getContext('2d');
            c.fillStyle = '#123456'; c.fillRect(0, 0, 5, 5);
            const first = canvas.toDataURL(), second = canvas.toDataURL();
            const worker = new Worker(URL.createObjectURL(new Blob([
                'postMessage({ua:navigator.userAgent,platform:navigator.platform,languages:navigator.languages})'
            ], {type:'text/javascript'})));
            const workerData = await new Promise(resolve => {worker.onmessage = event => resolve(event.data)});
            worker.terminate();
            const headers = await (await fetch('/headers')).json();
            return {ua:navigator.userAgent, platform:navigator.platform, languages:navigator.languages,
                timezone:Intl.DateTimeFormat().resolvedOptions().timeZone, workerData, headers,
                stableCanvas:first===second, pixel:Array.from(c.getImageData(0,0,1,1).data),
                canvasMethod:CanvasRenderingContext2D.prototype.getImageData.toString()};
        }""")
        self.assertEqual(native["ua"], fp["user_agent"])
        self.assertEqual(native["headers"]["User-Agent"], native["ua"])
        self.assertEqual(native["workerData"]["ua"], native["ua"])
        self.assertEqual(native["workerData"]["platform"], native["platform"])
        self.assertEqual(native["workerData"]["languages"], native["languages"])
        self.assertEqual(native["timezone"], fp["timezone"])
        self.assertEqual(fp["chrome_ver"], browser.version)
        self.assertIn(browser.version.split(".")[0], native["ua"])
        self.assertTrue(native["stableCanvas"])
        self.assertEqual(native["pixel"], [18, 52, 86, 255])
        self.assertIn("[native code]", native["canvasMethod"])
        self.assertGreater(stats.get_transfer_bytes(), 0)

    async def test_storage_and_captured_secret_are_isolated(self):
        _, ctx1, _, _ = await self.launch()
        _, ctx2, _, _ = await self.launch()
        page1, page2 = await ctx1.new_page(), await ctx2.new_page()
        await page1.goto(self.url)
        await page1.evaluate("document.cookie='account=first;path=/';localStorage.setItem('account','first');sessionStorage.setItem('account','first')")
        await page2.goto(self.url)
        state = await page2.evaluate("({cookie:document.cookie,local:localStorage.getItem('account'),session:sessionStorage.getItem('account')})")
        self.assertEqual(state, {"cookie": "", "local": None, "session": None})
        await page1.evaluate("fetch('/api/authenticationmethods/initializemobileapp').then(r=>r.json())")
        for _ in range(20):
            if ctx1._captured_totp_secret:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(ctx1._captured_totp_secret, SECRET)
        self.assertEqual(ctx2._captured_totp_secret, "")


class StartupFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_failure_closes_browser(self):
        browser = SimpleNamespace(new_context=AsyncMock(side_effect=RuntimeError("synthetic context failure")), close=AsyncMock())
        pw = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        with self.assertRaisesRegex(RuntimeError, "synthetic context failure"):
            await new_fingerprint_context(pw, True, None, __file__, slow_mo=0)
        browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
