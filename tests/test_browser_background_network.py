"""后台下载限制的启动参数回归；不使用账号或付费代理。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import async_playwright

import config
from utils import fingerprint as fp


EXPECTED_FLAGS = {
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-component-extensions-with-background-pages',
    '--disable-default-apps',
}


class LaunchOptionsTests(unittest.IsolatedAsyncioTestCase):
    def fake_runtime(self, failures=0):
        context = SimpleNamespace(add_init_script=AsyncMock(), on=MagicMock())
        browser = SimpleNamespace(version='130.0.6723.131',
                                  new_context=AsyncMock(return_value=context))
        launch = AsyncMock(side_effect=[RuntimeError('test launch failure')] * failures + [browser])
        return SimpleNamespace(chromium=SimpleNamespace(launch=launch)), launch

    async def run_context(self, pw, proxy=None, save_data=True):
        with patch.object(fp, '_get_proxy_geo', return_value={}), \
             patch.object(fp, 'build_init_script', return_value=''), \
             patch.object(fp, 'setup_save_data_route', AsyncMock()) as routing, \
             patch.object(fp.os.path, 'exists', return_value=True):
            await fp.new_fingerprint_context(pw, headless=True, proxy_config=proxy,
                                             exe_path='test-browser', enable_save_data=save_data)
        self.assertEqual(routing.await_count, int(save_data))

    def assert_restrictions(self, options):
        self.assertTrue(EXPECTED_FLAGS.issubset(options['args']))
        self.assertFalse(EXPECTED_FLAGS.intersection(options.get('ignore_default_args', [])))

    async def test_direct_and_proxy_preserve_restrictions_in_both_resource_modes(self):
        for proxy in (None, {'server': 'http://127.0.0.1:9'}):
            for save_data in (True, False):
                with self.subTest(proxy=proxy, save_data=save_data):
                    pw, launch = self.fake_runtime()
                    await self.run_context(pw, proxy, save_data)
                    self.assert_restrictions(launch.await_args.kwargs)
                    self.assertEqual(launch.await_args.kwargs['proxy'], proxy)

    async def test_default_browser_fallback_keeps_background_restrictions(self):
        pw, launch = self.fake_runtime(failures=1)
        await self.run_context(pw)
        self.assertEqual(launch.await_count, 2)
        self.assertIn('executable_path', launch.await_args_list[0].kwargs)
        self.assertNotIn('executable_path', launch.await_args_list[1].kwargs)
        for call in launch.await_args_list:
            self.assert_restrictions(call.kwargs)

    async def test_edge_and_chrome_fallbacks_keep_background_restrictions(self):
        for failures, final_channel in ((2, 'msedge'), (3, 'chrome')):
            with self.subTest(channel=final_channel), patch.object(fp.sys, 'platform', 'win32'):
                pw, launch = self.fake_runtime(failures=failures)
                await self.run_context(pw)
                self.assertEqual(launch.await_args.kwargs['channel'], final_channel)
                for call in launch.await_args_list:
                    self.assert_restrictions(call.kwargs)

    async def test_warmup_uses_same_flags_without_identity_submission(self):
        page = SimpleNamespace(goto=AsyncMock(), wait_for_load_state=AsyncMock())
        ctx = SimpleNamespace(new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=ctx), close=AsyncMock())
        launch = AsyncMock(return_value=browser)
        with patch.object(fp, 'setup_save_data_route', AsyncMock()):
            await fp._download_public_cache(SimpleNamespace(chromium=SimpleNamespace(launch=launch)), None, None)
        self.assert_restrictions(launch.await_args.kwargs)
        self.assertEqual(page.goto.await_count, 2)
        browser.close.assert_awaited_once()

    async def test_real_browser_command_line_contains_component_update_restrictions(self):
        # 只打开空浏览器。测试专用 enable-automation 用于读取命令行。
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=config.get_chromium_path(),
                                               args=[*fp.BROWSER_BACKGROUND_NETWORK_ARGS, '--enable-automation'])
            try:
                session = await browser.new_browser_cdp_session()
                result = await session.send('Browser.getBrowserCommandLine')
                self.assertTrue(EXPECTED_FLAGS.issubset(result['arguments']))
                await session.detach()
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
