import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_task_recovery import gui, batch
from modules.pipeline import Account, KeyResult
from utils import totp_cache


class UserIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, name, value in (
            (batch, "_QUEUE_FILE", str(Path(self.temp.name, "queue.json"))),
            (gui, "_sessions", {}),
            (gui, "_maintenance_pending", False),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        gui.app.testing = True
        self.result = KeyResult(Account("owner@example.invalid", "owner-password"), True,
                                totp_secret="JBSWY3DPEHPK3PXP", keys={"Synthetic": "OWNER-KEY"})
        batch.BatchManager.init_task("user_1", 1, [self.result.account])
        batch.BatchManager.record_account_result(self.result)

    def client(self, user):
        client = gui.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=user, sid=f"user_{user}")
        return client

    def test_foreign_sid_rejected_on_all_task_endpoints(self):
        client = self.client(2)
        for endpoint in ("/api/results", "/api/status", "/stream"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(client.get(endpoint + "?sid=user_1").status_code, 403)
        for endpoint in ("/api/start", "/api/export", "/api/stop", "/api/restart", "/api/cleanup"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(client.post(endpoint, json={"sid": "user_1"}).status_code, 403)
        self.assertEqual(client.get("/api/results?sid=user_2&sid=user_1").status_code, 403)
        self.assertEqual(batch.BatchManager.get_active_task()["status"], "running")

    def test_empty_session_does_not_inherit_global_results_or_status(self):
        client = self.client(2)
        self.assertEqual(client.get("/api/results").get_json(), [])
        self.assertEqual(client.get("/api/status").get_json(), {"running": False, "results": 0})
        with patch.object(gui, "export_to_excel") as export:
            self.assertEqual(client.post("/api/export", json={}).status_code, 400)
            export.assert_not_called()
        self.assertEqual(self.client(1).get("/api/results").get_json()[0]["email"], self.result.account.email)

    def test_foreign_queue_cannot_be_stopped_even_without_sid(self):
        with patch.object(gui.BrowserProcessManager, "cleanup_all") as cleanup:
            self.assertEqual(self.client(2).post("/api/stop", json={}).status_code, 403)
            cleanup.assert_not_called()
        self.assertEqual(batch.BatchManager.get_active_task()["status"], "running")

    def test_admin_maintenance_cannot_kill_foreign_active_task(self):
        client = self.client(2)
        with patch.object(gui, "is_user_admin", return_value=True), patch.object(gui.BrowserProcessManager, "cleanup_all") as cleanup, patch.object(gui.threading.Thread, "start") as start:
            for endpoint in ("/api/cleanup", "/api/restart"):
                self.assertEqual(client.post(endpoint, json={}).status_code, 403)
            cleanup.assert_not_called()
            start.assert_not_called()

    def test_regular_user_cannot_run_global_maintenance_when_idle(self):
        batch.BatchManager.stop_task()
        with patch.object(gui, "is_user_admin", return_value=False), patch.object(gui.BrowserProcessManager, "cleanup_all") as cleanup:
            for endpoint in ("/api/cleanup", "/api/restart"):
                self.assertEqual(self.client(1).post(endpoint, json={}).status_code, 403)
            cleanup.assert_not_called()

    def test_owner_stop_blocks_new_task_until_worker_finishes_cleanup(self):
        s = gui._get_sess("user_1")
        s.running = s.worker_active = True
        with patch.object(gui.BrowserProcessManager, "cleanup_all", return_value=0), patch.object(gui, "release_system_memory"):
            self.assertEqual(self.client(1).post("/api/stop", json={}).status_code, 200)
        self.assertIsNone(batch.BatchManager.get_active_task())
        thread = MagicMock()
        with patch.object(gui.threading, "Thread", thread):
            request = {"accounts": "second@example.invalid password"}
            self.assertEqual(self.client(2).post("/api/start", json=request).status_code, 400)
            thread.assert_not_called()
            s.worker_active = False
            self.assertEqual(self.client(2).post("/api/start", json=request).status_code, 200)
            thread.assert_called_once()

    def test_stream_only_replays_own_logs(self):
        gui._push_log("user_1", "OWNER-PRIVATE-LOG")
        gui._push_log("user_2", "OWN-LOG")
        response = self.client(2).get("/stream", buffered=False)
        try:
            chunk = next(response.response).decode()
            self.assertIn("OWN-LOG", chunk)
            self.assertNotIn("OWNER-PRIVATE-LOG", chunk)
        finally:
            response.close()

    def test_export_names_are_unique_and_content_is_user_scoped(self):
        gui._get_sess("user_2").results = [KeyResult(Account("second@example.invalid", "second-password"), False)]
        seen = []
        def export(results, filename):
            seen.append((results[0].account.email, filename))
            path = Path(self.temp.name, filename)
            path.write_bytes(b"synthetic export")
            return str(path)
        with patch.object(gui, "export_to_excel", side_effect=export):
            for user in (1, 2, 1):
                response = self.client(user).post("/api/export", json={})
                self.assertEqual(response.status_code, 200)
                response.close()
        self.assertEqual([row[0] for row in seen], ["owner@example.invalid", "second@example.invalid", "owner@example.invalid"])
        self.assertEqual(len({row[1] for row in seen}), 3)

    def test_sid_in_cookie_is_rederived_from_authenticated_user(self):
        client = self.client(2)
        with client.session_transaction() as session:
            session["sid"] = "user_1"
        self.assertEqual(client.get("/api/results").get_json(), [])

    def test_global_restart_blocks_new_start_until_restart_finishes(self):
        batch.BatchManager.stop_task()
        client = self.client(1)
        with patch.object(gui, "is_user_admin", return_value=True), patch.object(gui.threading.Thread, "start") as start:
            self.assertEqual(client.post("/api/restart", json={}).status_code, 200)
            start.assert_called_once()
            self.assertEqual(self.client(2).post("/api/start", json={"accounts": "second@example.invalid password"}).status_code, 400)

    def test_idle_stop_does_not_clean_up_other_users_finished_task(self):
        batch.BatchManager.stop_task()
        with patch.object(gui.BrowserProcessManager, "cleanup_all") as cleanup:
            self.assertEqual(self.client(2).post("/api/stop", json={}).status_code, 200)
            cleanup.assert_not_called()

    def test_completed_task_without_restart_allows_next_user(self):
        batch.BatchManager.mark_all_done(restarting=False)
        self.assertEqual(self.client(2).get("/api/results").get_json(), [])
        with patch.object(gui.threading.Thread, "start"):
            response = self.client(2).post("/api/start", json={"accounts": "second@example.invalid password"})
            self.assertEqual(response.status_code, 200)

    def test_worker_only_seeds_current_owners_history_and_resets_scope(self):
        history = [{"email": "same@example.invalid", "totp_secret": "JBSWY3DPEHPK3PXP"}]
        with patch.object(totp_cache, "_DATA_DIR", self.temp.name), patch.object(totp_cache, "_USER_CACHES", {}), patch.object(gui, "get_history", return_value=history) as reader, patch.object(gui, "_run_batch_worker_impl", side_effect=lambda sid: totp_cache.get_cached_totp("same@example.invalid")) as worker:
            self.assertEqual(gui._run_batch_worker("user_1"), history[0]["totp_secret"])
            reader.assert_called_once_with(1)
            worker.assert_called_once_with("user_1")
            self.assertFalse(gui._get_sess("user_1").worker_active)
            with totp_cache.totp_cache_scope(2):
                self.assertEqual(totp_cache.get_cached_totp("same@example.invalid"), "")

    def test_worker_cannot_attach_foreign_queue(self):
        with patch.object(gui, "get_history") as history, patch.object(gui, "_run_batch_worker_impl") as worker:
            gui._run_batch_worker("user_2")
            history.assert_not_called()
            worker.assert_not_called()
            self.assertFalse(gui._get_sess("user_2").worker_active)

    def test_cross_site_cookie_authenticated_control_is_rejected(self):
        client = self.client(1)
        with patch.object(gui.BrowserProcessManager, "cleanup_all") as cleanup:
            response = client.post("/api/stop", data='{}', content_type="text/plain", headers={"Origin": "https://foreign.invalid"})
            self.assertEqual(response.status_code, 403)
            response = client.post("/api/stop", json={}, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(response.status_code, 403)
            cleanup.assert_not_called()
        self.assertEqual(batch.BatchManager.get_active_task()["status"], "running")


if __name__ == "__main__":
    unittest.main()
