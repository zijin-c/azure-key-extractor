import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from modules import azure_login as login
from modules import key_extractor as extractor


SECRET = "JBSWY3DPEHPK3PXP"


class PortalPageDetection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        candidates = list((Path(__file__).resolve().parents[1] / "ms-playwright").glob("chromium-*/chrome-win64/chrome.exe"))
        self.browser = await self.pw.chromium.launch(
            headless=True, **({"executable_path": str(candidates[0])} if candidates else {})
        )
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def show(self, url, html):
        await self.page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
        await self.page.goto(url)

    async def test_proofup_in_return_url_is_not_registration(self):
        await self.show(
            "https://login.microsoftonline.com/common/login?returnUrl=https%3A%2F%2Fmysignins.microsoft.com%2Fproofup",
            "<h1>Sign in</h1><input name='loginfmt'>",
        )
        self.assertFalse(await extractor._is_mfa_setup_page(self.page))

    async def test_mysignins_code_challenge_is_not_registration(self):
        await self.show("https://mysignins.microsoft.com/proofup", "<h1>Enter code</h1><input name='otc'>")
        self.assertFalse(await extractor._is_mfa_setup_page(self.page))
        self.assertTrue(await extractor._is_totp_input_page(self.page))

    async def test_mysignins_loading_is_not_registration(self):
        await self.show("https://mysignins.microsoft.com/security-info", "<p>Loading...</p>")
        self.assertFalse(await extractor._is_mfa_setup_page(self.page))

    async def test_real_setup_link_uses_valid_dom_selectors(self):
        await self.show("https://mysignins.microsoft.com/security-info", "<a>I want to use a different authenticator app</a>")
        self.assertTrue(await extractor._is_mfa_setup_page(self.page))

    async def test_hidden_challenge_does_not_hide_real_registration(self):
        await self.show("https://mysignins.microsoft.com/security-info", "<input name='otc' hidden><h1>Scan the QR code</h1>")
        self.assertTrue(await extractor._is_mfa_setup_page(self.page))

    async def test_push_prompt_is_not_a_code_input(self):
        await self.show("https://login.microsoftonline.com/common/login", "<h1>Approve a request</h1><a>Sign in another way</a>")
        self.assertFalse(await extractor._is_totp_input_page(self.page))
        self.assertTrue(await extractor._is_mfa_login_prompt(self.page))

    async def test_fill_only_does_not_submit_form(self):
        await self.show("https://login.microsoftonline.com/common/login", """
            <form onsubmit="event.preventDefault(); window.submits++">
                <input name='otc'><button type='submit'>Verify</button>
            </form><script>window.submits = 0</script>
        """)
        self.assertTrue(await login._fill_totp_code(self.page, "123456", submit=False))
        self.assertEqual(await self.page.locator("input").input_value(), "123456")
        self.assertEqual(await self.page.evaluate("window.submits"), 0)


class PortalLoginFlow(unittest.IsolatedAsyncioTestCase):
    def page(self):
        return SimpleNamespace(
            url="https://login.microsoftonline.com/common/login", context=None,
            evaluate=AsyncMock(return_value="loading..."), keyboard=SimpleNamespace(press=AsyncMock()),
            goto=AsyncMock(),
        )

    async def test_slow_login_wait_does_not_start_registration(self):
        page = self.page()
        with patch.object(login, "_azure_destination_ready", AsyncMock(side_effect=[False] * 21 + [True])), \
             patch.object(login, "_handle_mfa_setup", AsyncMock()) as setup, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=False)), \
             patch.object(extractor, "_is_mfa_setup_page", AsyncMock(return_value=False)), \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await extractor._handle_portal_login(page, SECRET, "", None), SECRET)
            setup.assert_not_awaited()

    async def test_unknown_login_raises_instead_of_registering(self):
        page = self.page()
        with patch.object(login, "_azure_destination_ready", AsyncMock(return_value=False)), \
             patch.object(login, "_handle_mfa_setup", AsyncMock()) as setup, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=False)), \
             patch.object(extractor, "_is_mfa_setup_page", AsyncMock(return_value=False)), \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(extractor.PortalLoadError) as error:
                await extractor._handle_portal_login(page, SECRET, "", None)
            self.assertEqual(error.exception.totp, SECRET)
            setup.assert_not_awaited()

    async def test_real_registration_still_uses_state_machine(self):
        page = self.page()
        with patch.object(login, "_azure_destination_ready", AsyncMock(return_value=False)), \
             patch.object(login, "_handle_mfa_setup", AsyncMock(return_value=SECRET)) as setup, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=False)), \
             patch.object(extractor, "_is_mfa_setup_page", AsyncMock(side_effect=[True, False])), \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await extractor._handle_portal_login(page, SECRET, "", None), SECRET)
            setup.assert_awaited_once()

    async def test_first_setup_at_key_extraction_waits_for_loading_page(self):
        page = self.page()
        page.context = SimpleNamespace(_captured_totp_secret="")
        with patch.object(login, "_azure_destination_ready", AsyncMock(return_value=False)), \
             patch.object(login, "_handle_mfa_setup", AsyncMock(return_value=SECRET)) as setup, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=False)), \
             patch.object(extractor, "_is_mfa_setup_page", AsyncMock(side_effect=[False] * 5 + [True, False])), \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await extractor._handle_portal_login(page, "", "", None), SECRET)
            setup.assert_awaited_once_with(page, None, existing_secret="", ms_email="")

    async def test_existing_secret_from_initial_login_is_reused_for_key_extraction(self):
        page = self.page()
        page.context = SimpleNamespace(_captured_totp_secret=SECRET)
        with patch.object(login, "_azure_destination_ready", AsyncMock(side_effect=[False, False, True])), \
             patch.object(login, "_handle_mfa_setup", AsyncMock()) as setup, \
             patch.object(login, "_fill_totp_code", AsyncMock(return_value=True)) as fill, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=True)), \
             patch.object(extractor, "_is_mfa_setup_page", AsyncMock(return_value=False)), \
             patch.object(extractor, "_click_first_visible", AsyncMock(return_value=True)), \
             patch.object(extractor.pyotp, "TOTP", return_value=SimpleNamespace(now=lambda: "123456")) as totp, \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await extractor._handle_portal_login(page, "", "", None), SECRET)
            totp.assert_called_once_with(SECRET)
            fill.assert_awaited_once_with(page, "123456", None, submit=False)
            setup.assert_not_awaited()

    async def test_challenge_without_original_secret_does_not_start_registration(self):
        page = self.page()
        with patch.object(login, "_azure_destination_ready", AsyncMock(return_value=False)), \
             patch.object(login, "_handle_mfa_setup", AsyncMock()) as setup, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=True)), \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(extractor.PortalLoadError, "请提供原 2FA 密钥"):
                await extractor._handle_portal_login(page, "", "", None)
            setup.assert_not_awaited()

    async def test_incomplete_setup_is_not_treated_as_success(self):
        page = self.page()
        with patch.object(login, "_azure_destination_ready", AsyncMock(return_value=False)), \
             patch.object(login, "_handle_mfa_setup", AsyncMock(return_value=SECRET)) as setup, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=False)), \
             patch.object(extractor, "_is_mfa_setup_page", AsyncMock(return_value=True)), \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(extractor.PortalLoadError):
                await extractor._handle_portal_login(page, SECRET, "", None)
            setup.assert_awaited_once()
            page.goto.assert_not_awaited()

    async def test_code_is_submitted_once_while_redirect_is_pending(self):
        page = self.page()
        with patch.object(login, "_azure_destination_ready", AsyncMock(side_effect=[False] * 20 + [True])), \
             patch.object(login, "_fill_totp_code", AsyncMock(return_value=True)) as fill, \
             patch.object(extractor, "_is_totp_input_page", AsyncMock(return_value=True)), \
             patch.object(extractor, "_is_mfa_setup_page", AsyncMock(return_value=False)), \
             patch.object(extractor, "_click_first_visible", AsyncMock(return_value=True)) as click, \
             patch.object(extractor.pyotp, "TOTP", return_value=SimpleNamespace(now=lambda: "123456")), \
             patch.object(extractor.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await extractor._handle_portal_login(page, SECRET, "", None), SECRET)
            fill.assert_awaited_once_with(page, "123456", None, submit=False)
            click.assert_awaited_once()
            page.keyboard.press.assert_not_awaited()

    async def test_login_redirects_do_not_extend_total_timeout(self):
        page = self.page()
        clock = [0.0]

        async def sleep(seconds):
            clock[0] += seconds

        fake_loop = SimpleNamespace(time=lambda: clock[0])
        with patch.object(extractor.asyncio, "get_event_loop", return_value=fake_loop), \
             patch.object(extractor.asyncio, "get_running_loop", return_value=fake_loop), \
             patch.object(extractor.asyncio, "sleep", side_effect=sleep), \
             patch.object(extractor, "_handle_portal_login", AsyncMock(return_value=SECRET)) as handle:
            with self.assertRaises(extractor.PortalLoadError):
                await extractor._wait_for_portal_ready(page, None, timeout=3, totp_secret=SECRET)
            self.assertEqual(handle.await_count, 3)

    async def test_stuck_nested_login_is_cancelled_at_deadline(self):
        page = self.page()
        cancelled = []

        async def stuck(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        with patch.object(extractor, "_handle_portal_login", side_effect=stuck):
            with self.assertRaises(extractor.PortalLoadError) as error:
                await extractor._wait_for_portal_ready(page, None, timeout=0.02, totp_secret=SECRET)
            self.assertEqual(error.exception.totp, SECRET)
            self.assertEqual(cancelled, [True])


if __name__ == "__main__":
    unittest.main()
