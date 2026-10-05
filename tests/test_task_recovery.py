import importlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gui import auth
from modules import batch_manager as batch
from modules.pipeline import Account, KeyResult
from utils.session_secret import load_session_secret


with patch.object(auth, "init_db"), patch("utils.session_secret.load_session_secret", return_value="synthetic-session-secret-for-tests-123456"), patch("threading.Thread.start"):
    gui = importlib.import_module("gui.app")


class SessionSecretTests(unittest.TestCase):
    def test_persistent_random_key_is_shared_across_simultaneous_starts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AZURE_KEY_SESSION_SECRET": ""}):
            path = Path(directory) / "session_secret"
            with ThreadPoolExecutor(max_workers=3) as executor:
                keys = list(executor.map(lambda _: load_session_secret(path), range(3)))
            self.assertEqual(len(set(keys)), 1)
            self.assertGreaterEqual(len(keys[0]), 32)
            self.assertEqual(load_session_secret(path), keys[0])

    def test_configured_key_must_be_long_enough(self):
        with patch.dict(os.environ, {"AZURE_KEY_SESSION_SECRET": "short"}):
            with self.assertRaises(ValueError):
                load_session_secret()


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(batch, "_QUEUE_FILE", str(Path(self.temp.name) / "queue.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        gui._sessions.clear()
        gui.app.testing = True
        self.first = Account("first@example.invalid", "synthetic")
        self.second = Account("second@example.invalid", "synthetic")
        self.result = KeyResult(self.first, True, keys={"Synthetic": "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"})

    def init(self):
        batch.BatchManager.init_task("user_999", 999, [self.first, self.second])

    def test_resume_excludes_completed_accounts(self):
        self.init()
        batch.BatchManager.record_account_result(self.result)
        accounts, *_ = batch.BatchManager.get_current_batch()
        self.assertEqual([a.email for a in accounts], [self.second.email])

    def test_repeated_result_does_not_duplicate_or_erase_keys(self):
        self.init()
        batch.BatchManager.record_account_result(self.result)
        batch.BatchManager.record_account_result(KeyResult(self.first, False))
        results = batch.BatchManager.get_all_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].keys, self.result.keys)
        self.assertTrue(results[0].success)

    def test_running_task_is_restarted_after_service_restart(self):
        self.init()
        batch.BatchManager.record_account_result(self.result)
        thread = MagicMock()
        with patch.object(gui, "threading", SimpleNamespace(Thread=thread)), patch("time.sleep"):
            gui._startup_check_batch()
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertTrue(gui._get_sess("user_999").running)
        self.assertEqual(len(gui._get_sess("user_999").results), 1)

    def test_all_completed_before_crash_finishes_without_reprocessing(self):
        self.init()
        batch.BatchManager.record_account_result(self.result)
        batch.BatchManager.record_account_result(KeyResult(self.second, False))
        with patch.object(gui, "run_pipeline", AsyncMock(return_value=[])) as pipeline, \
             patch.object(gui, "export_to_excel", return_value="synthetic.xlsx"), \
             patch.object(gui.BrowserProcessManager, "cleanup_all", return_value=0), \
             patch.object(gui, "release_system_memory"), \
             patch.object(gui.config, "AUTO_RESTART_ON_COMPLETE", False):
            gui._run_batch_worker("user_999")
        self.assertEqual(pipeline.call_args.args[0], [])
        self.assertFalse(gui._get_sess("user_999").running)
        self.assertFalse(batch.BatchManager.is_running())

    def test_simultaneous_start_requests_only_create_one_task(self):
        def start(user):
            client = gui.app.test_client()
            with client.session_transaction() as session:
                session.update(user_id=user, sid=f"user_{user}")
            return client.post("/api/start", json={"accounts": "synthetic@example.invalid dummy"}).status_code
        thread = MagicMock()
        with patch.object(gui, "threading", SimpleNamespace(Thread=thread)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(start, (991, 992)))
        self.assertEqual(sorted(statuses), [200, 400])
        thread.assert_called_once()

    def test_failed_queue_write_does_not_leave_task_running(self):
        client = gui.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=999, sid="user_999")
        with patch.object(batch.BatchManager, "_save_task_data", side_effect=OSError("synthetic disk error")):
            response = client.post("/api/start", json={"accounts": "synthetic@example.invalid dummy"})
        self.assertEqual(response.status_code, 500)
        self.assertFalse(gui._get_sess("user_999").running)
        self.assertFalse(batch.BatchManager.is_running())


if __name__ == "__main__":
    unittest.main()
