import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules import azure_signup as signup
from utils.http_cache import LocalHttpCache as Cache


class FormLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_first_match_does_not_hide_visible_form(self):
        hidden = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        visible = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        matches = SimpleNamespace(count=AsyncMock(return_value=2), nth=lambda i: [hidden, visible][i])
        main = SimpleNamespace(locator=lambda query: matches)
        page = SimpleNamespace(frames=[main], main_frame=main)
        target, locator = await signup._find_visible_locator(page, ['input'])
        self.assertIs(target, main)
        self.assertIs(locator, visible)

    async def test_failure_diagnostics_strip_query_and_remove_listeners(self):
        page = MagicMock()
        listeners = {}
        page.on.side_effect = lambda event, callback: listeners.update({event: callback})
        request = SimpleNamespace(resource_type='script', url='https://assets.test/app.js?token=private', failure='net::ERR_CONNECTION_RESET')

        async def failed_form(page, email, cb, diagnostics):
            listeners['requestfailed'](request)
            self.assertEqual(diagnostics, ['requestfailed net::ERR_CONNECTION_RESET assets.test/app.js'])
            raise signup.SheerIDVerificationError('missing form')

        with patch.object(signup, '_fill_azure_profile_form', side_effect=failed_form):
            with self.assertRaises(signup.SheerIDVerificationError):
                await signup.fill_azure_profile_form(page, '', None)
        self.assertEqual(page.remove_listener.call_count, 3)

    async def test_cache_waiter_timeout_preserves_owner_and_cleans_registry(self):
        url = 'https://assets.test/app.js'
        async with Cache.download_lock(url) as owner:
            self.assertTrue(owner)
            async with Cache.download_lock(url, timeout=0.01) as waiter:
                self.assertFalse(waiter)
            self.assertTrue(Cache._fills)
        self.assertFalse(Cache._fills)
        async with Cache.download_lock(url, timeout=0.01) as acquired:
            self.assertTrue(acquired)


class CacheResponseTests(unittest.TestCase):
    def test_html_is_not_saved_as_script_and_old_html_cache_is_not_replayed(self):
        url = 'https://assets.test/app.js'
        with tempfile.TemporaryDirectory() as directory, patch.object(Cache, 'CACHE_DIR', directory):
            Cache.put(url, b'<html>blocked</html>', {'content-type': 'text/html'})
            self.assertIsNone(Cache.get(url))
            Cache.put(url, b'window.loaded=true;', {'content-type': 'application/javascript'})
            self.assertIsNotNone(Cache.get(url))
            metadata = Path(directory, Cache._url_to_key(url) + '.meta')
            data = json.loads(metadata.read_text())
            data['headers']['content-type'] = 'text/html'
            metadata.write_text(json.dumps(data))
            self.assertIsNone(Cache.get(url))


if __name__ == '__main__':
    unittest.main()
