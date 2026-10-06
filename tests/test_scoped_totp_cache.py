import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import totp_cache as cache


EMAIL = "same@example.invalid"
FIRST = "JBSWY3DPEHPK3PXP"
SECOND = "GEZDGNBVGY3TQOJQ"


class ScopedTotpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in (("_DATA_DIR", self.temp.name), ("_CACHE_FILE", str(Path(self.temp.name, "totp_cache.json"))), ("_USER_CACHES", {}), ("_MEMORY_CACHE", {EMAIL: FIRST}), ("_INITIALIZED", True)):
            p = patch.object(cache, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_same_email_different_users_and_legacy_cache_are_separate(self):
        with cache.totp_cache_scope(1):
            self.assertEqual(cache.get_cached_totp(EMAIL), "")
            self.assertTrue(cache.save_totp_cache(EMAIL, SECOND))
        with cache.totp_cache_scope(2):
            self.assertEqual(cache.get_cached_totp(EMAIL), "")
            self.assertTrue(cache.save_totp_cache(EMAIL, FIRST))
        with cache.totp_cache_scope(1):
            self.assertEqual(cache.get_cached_totp(EMAIL), SECOND)
        self.assertEqual(cache.get_cached_totp(EMAIL), FIRST)
        cache._USER_CACHES.clear()
        with cache.totp_cache_scope(1):
            self.assertEqual(cache.get_cached_totp(EMAIL), SECOND)
        with cache.totp_cache_scope(2):
            self.assertEqual(cache.get_cached_totp(EMAIL), FIRST)

    def test_concurrent_async_tasks_keep_their_scope(self):
        async def execute(user, secret):
            with cache.totp_cache_scope(user):
                cache.save_totp_cache(EMAIL, secret)
                await asyncio.sleep(0)
                return cache.get_cached_totp(EMAIL)
        async def main():
            return await asyncio.gather(execute(1, FIRST), execute(2, SECOND))
        self.assertEqual(asyncio.run(main()), [FIRST, SECOND])

    def test_write_failure_is_retried_and_old_file_is_preserved(self):
        with cache.totp_cache_scope(1):
            cache.save_totp_cache(EMAIL, FIRST)
            with patch.object(cache.os, "replace", side_effect=OSError("synthetic write failure")):
                self.assertFalse(cache.save_totp_cache(EMAIL, SECOND))
            self.assertEqual(cache.get_cached_totp(EMAIL), FIRST)
            self.assertTrue(cache.save_totp_cache(EMAIL, SECOND))
        self.assertEqual(json.loads(Path(self.temp.name, "user_totp", "1.json").read_text())[EMAIL], SECOND)
        self.assertEqual(list(Path(self.temp.name, "user_totp").glob(".totp-*")), [])


if __name__ == "__main__":
    unittest.main()
