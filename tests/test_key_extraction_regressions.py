import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import async_playwright

from modules import key_extractor as extractor
from modules import pipeline


KEY = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"
PRODUCTS = ["Visio Professional 2021", "Project Professional 2021 - DVD"]
ROOT = Path(__file__).resolve().parents[1]
HTML = """
<input id="global" placeholder="Search resources, services, and docs" value="global">
<div class="fxs-blade" id="education">
  <button class="fxs-blade-close" onclick="this.parentElement.remove()">Close Education</button>
  Education
</div>
<div class="fxs-blade" id="software">
  <button class="fxs-blade-close" onclick="this.parentElement.remove()">Close Software</button>
  <input id="search" placeholder="Search software" value="Visio">
  <span>Software</span>
  <button data-icon-name="Cancel" onclick="this.parentElement.remove()">Cancel</button>
</div>
"""
DETAIL = """<div class="fxs-blade ms-Panel" role="dialog" id="detail">
  View Key
  <button class="fxs-blade-close" onclick="this.parentElement.remove()">Close</button>
</div>"""


class BrowserRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        candidates = list((ROOT / "ms-playwright").glob("chromium-*/chrome-win64/chrome.exe"))
        try:
            self.browser = await self.pw.chromium.launch(
                headless=True, **({"executable_path": str(candidates[0])} if candidates else {})
            )
        except Exception:
            await self.pw.stop()
            raise
        self.ctx = await self.browser.new_context()
        self.page = await self.ctx.new_page()
        await self.page.route("**/*", lambda route: route.fulfill(body=HTML, content_type="text/html"))
        await self.page.goto("http://software.test/#EducationMenuBlade/~/software")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_close_only_product_detail_and_keep_software(self):
        await self.page.evaluate("html => document.body.insertAdjacentHTML('beforeend', html)", DETAIL)
        await extractor._close_panel(self.page)
        self.assertEqual(await self.page.locator("#detail").count(), 0)
        self.assertEqual(await self.page.locator("#software").count(), 1)

    async def test_failed_product_without_view_key_can_be_closed(self):
        detail = DETAIL.replace("View Key", "<h2>Visio Professional 2021</h2>")
        await self.page.evaluate("html => document.body.insertAdjacentHTML('beforeend', html)", detail)
        await extractor._close_panel(self.page)
        self.assertEqual(await self.page.locator("#detail").count(), 0)
        self.assertEqual(await self.page.locator("#software").count(), 1)
        await extractor._close_panel(self.page)
        self.assertEqual(await self.page.locator("#education").count(), 1)
        self.assertEqual(await self.page.locator("#software").count(), 1)

    async def test_clear_and_search_keep_global_and_cancel_untouched(self):
        await extractor._clear_search(self.page)
        self.assertEqual(await self.page.locator("#search").input_value(), "")
        self.assertEqual(await self.page.locator("#global").input_value(), "global")
        self.assertTrue(await extractor._ensure_software_blade_active(self.page))
        self.assertTrue(await extractor._search_product(self.page, "Project", None))
        self.assertEqual(await self.page.locator("#search").input_value(), "Project")
        self.assertEqual(await self.page.locator("#software").count(), 1)

    async def test_iframe_search_near_top_is_supported(self):
        await self.page.set_content('<input id="global" placeholder="搜索资源" value="unchanged"><iframe></iframe>')
        frame = self.page.frames[1]
        await frame.set_content('<input id="local" placeholder="筛选软件" value="Visio">')
        self.assertTrue(await extractor._has_software_search(self.page))
        await extractor._clear_search(self.page)
        self.assertEqual(await frame.locator("#local").input_value(), "")
        self.assertTrue(await extractor._search_product(self.page, "Project", None))
        self.assertEqual(await frame.locator("#local").input_value(), "Project")
        self.assertEqual(await self.page.locator("#global").input_value(), "unchanged")

    async def test_missing_software_search_recovers_by_navigation(self):
        await self.page.set_content('<input placeholder="Search resources, services, and docs">')
        self.assertFalse(await extractor._has_software_search(self.page))
        self.assertTrue(await extractor._search_product(self.page, "Project", None))
        self.assertEqual(await self.page.locator("#search").input_value(), "Project")

    async def _extract(self, scan, shared, checkpoints):
        async def click(page, product, cb):
            expected = "Visio" if product == PRODUCTS[0] else "Project"
            self.assertEqual(await page.locator("#search").input_value(), expected)
            await page.evaluate("html => document.body.insertAdjacentHTML('beforeend', html)", DETAIL)
            return True

        with patch.object(extractor.config, "PRODUCTS_TO_EXTRACT", PRODUCTS), \
             patch.object(extractor, "_wait_for_portal_ready", AsyncMock(return_value=(True, ""))), \
             patch.object(extractor, "_handle_terms_flow", AsyncMock()), \
             patch.object(extractor, "_is_mfa_login_prompt", AsyncMock(return_value=False)), \
             patch.object(extractor, "_click_product_row", side_effect=click), \
             patch.object(extractor, "_extract_key_from_panel", side_effect=scan), \
             patch.object(extractor, "get_cached_totp", return_value=""):
            return await extractor.extract_all_keys(
                self.page, self.ctx, "", "test@example.invalid", keys_out=shared,
                on_key=lambda keys, secret: checkpoints.append(keys),
            )

    async def test_two_products_and_immediate_checkpoints(self):
        shared, checkpoints = {}, []
        keys, _ = await self._extract(AsyncMock(side_effect=[KEY, KEY.replace("AAAAA", "FFFFF")]), shared, checkpoints)
        self.assertEqual(len([v for v in keys.values() if v]), 2)
        self.assertEqual(checkpoints[0], {PRODUCTS[0]: KEY})
        self.assertEqual(len(checkpoints[1]), 2)
        self.assertEqual(await self.page.locator("#software").count(), 1)

    async def test_partial_keys_survive_exception_before_return(self):
        shared, checkpoints = {}, []
        with self.assertRaises(extractor.PortalLoadError):
            await self._extract(AsyncMock(side_effect=[KEY, extractor.PortalLoadError("offline")]), shared, checkpoints)
        self.assertEqual(shared[PRODUCTS[0]], KEY)
        self.assertEqual(checkpoints[0][PRODUCTS[0]], KEY)

    async def test_one_key_survives_other_product_and_retry_failure(self):
        shared, checkpoints = {}, []
        keys, _ = await self._extract(AsyncMock(side_effect=[KEY, "", ""]), shared, checkpoints)
        self.assertEqual(keys, {PRODUCTS[0]: KEY, PRODUCTS[1]: ""})
        self.assertEqual(checkpoints, [{PRODUCTS[0]: KEY}])


class PersistenceRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_key_is_on_disk_before_extraction_raises(self):
        account = pipeline.Account("test@example.invalid", "dummy")
        page = MagicMock()
        ctx = SimpleNamespace(new_page=AsyncMock(return_value=page))
        pw = SimpleNamespace(start=AsyncMock(return_value=MagicMock()))
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            async def extract(*args, keys_out, on_key, **kwargs):
                keys_out[PRODUCTS[0]] = KEY
                on_key(dict(keys_out), "")
                files = list(Path(directory).glob("*.jsonl"))
                self.assertEqual(len(files), 1)
                observed.append(json.loads(files[0].read_text(encoding="utf-8"))["keys"][PRODUCTS[0]])
                raise extractor.PortalLoadError("offline after first key")

            with patch.object(pipeline, "_RESULTS_DIR", directory), \
                 patch.object(pipeline, "async_playwright", return_value=pw), \
                 patch.object(pipeline, "new_fingerprint_context", AsyncMock(return_value=(MagicMock(), ctx, {}, None))), \
                 patch.object(pipeline, "BrowserProcessManager"), \
                 patch.object(pipeline, "get_playwright_driver_pid", return_value=None), \
                 patch.object(pipeline, "get_cached_totp", return_value=""), \
                 patch.object(pipeline, "do_azure_login", AsyncMock(return_value="")), \
                 patch.object(pipeline, "extract_all_keys", side_effect=extract), \
                 patch.object(pipeline, "safe_close_playwright", AsyncMock()):
                result = await pipeline.process_account(account, mode="direct_ms")
            self.assertTrue(result.success)
            self.assertEqual(observed, [KEY])
            self.assertEqual(result.keys[PRODUCTS[0]], KEY)
            entries = [json.loads(line) for line in list(Path(directory).glob("*.jsonl"))[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(entries[-1]["keys"][PRODUCTS[0]], KEY)

    async def test_timeout_keeps_partial_result_successful(self):
        async def process(*args, keys_out, **kwargs):
            keys_out[PRODUCTS[0]] = KEY
            await asyncio.sleep(10)

        with patch.object(pipeline, "process_account", side_effect=process), \
             patch.object(pipeline, "_persist_result"):
            results = await pipeline.run_pipeline(
                [pipeline.Account("test@example.invalid", "dummy")], account_timeout=0.05,
            )
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].keys[PRODUCTS[0]], KEY)


if __name__ == "__main__":
    unittest.main()
