from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

from test_admin_stats import DatabaseTestCase
from test_task_recovery import gui, batch
from gui import auth
from modules.pipeline import Account, KeyResult


class UserPolicyTests(DatabaseTestCase):
    def set_policy(self, **overrides):
        data = {"user_id": self.alice, "require_proxy": False, "max_concurrency": 2}
        data.update(overrides)
        return self.client(self.admin).post("/admin/users/policy", json=data)

    def test_policy_defaults_and_persistence_are_per_user(self):
        self.assertEqual(auth.get_user_policy(self.alice), {"require_proxy": False, "max_concurrency": 20})
        self.assertTrue(self.set_policy(require_proxy=True).get_json()["ok"])
        self.close_db()
        auth.init_db()
        self.assertEqual(auth.get_user_policy(self.alice), {"require_proxy": True, "max_concurrency": 2})
        self.assertEqual(auth.get_user_policy(self.bob), {"require_proxy": False, "max_concurrency": 20})
        row = self.stats_user(self.alice)
        self.assertEqual((row["require_proxy"], row["max_concurrency"]), (1, 2))
        self.assertEqual(self.client(self.alice).get("/api/settings").get_json()["policy"]["max_concurrency"], 2)

    def test_policy_rejects_invalid_values_and_deleted_users(self):
        for value in (0, 21, -1, True, 1.5, "2", None):
            with self.subTest(value=value):
                self.assertEqual(self.set_policy(max_concurrency=value).status_code, 400)
        for value in ("false", 0, None):
            self.assertEqual(self.set_policy(require_proxy=value).status_code, 400)
        self.assertEqual(self.set_policy(user_id="2").status_code, 400)
        auth.delete_user(self.alice)
        self.assertEqual(self.set_policy().status_code, 404)

    def test_regular_user_cannot_modify_policy_through_settings(self):
        self.set_policy(require_proxy=True)
        client = self.client(self.alice)
        self.assertEqual(client.post("/admin/users/policy", json={"user_id": self.alice, "require_proxy": False, "max_concurrency": 20}).status_code, 403)
        self.assertTrue(client.post("/api/settings", json={"require_proxy": False, "max_concurrency": 20, "user_id": self.bob}).get_json()["ok"])
        self.assertEqual(auth.get_user_policy(self.alice), {"require_proxy": True, "max_concurrency": 2})
        self.assertEqual(auth.get_user_settings(self.bob), {})

    def test_start_enforces_concurrency_even_for_direct_api_calls(self):
        self.set_policy()
        client = self.client(self.alice)
        with patch.object(gui.threading.Thread, "start") as start:
            for value in (True, 1.5, "2.0", None, "invalid"):
                with self.subTest(value=value):
                    response = client.post("/api/start", json={"accounts": "test@example.invalid password", "concurrency": value, "max_concurrency": 100})
                    self.assertEqual(response.status_code, 400)
            start.assert_not_called()
            self.assertIsNone(batch.BatchManager.get_active_task())
            for requested, expected in ((3, 2), (20, 2), (100, 2), (0, 1), (-1, 1), (2, 2)):
                response = client.post("/api/start", json={"accounts": "test@example.invalid password", "concurrency": requested, "max_concurrency": 100})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["concurrency"], expected)
                self.assertEqual(batch.BatchManager.get_active_task()["concurrency"], expected)
                batch.BatchManager.clear_task()
                gui._sessions.clear()

    def test_start_defaults_to_low_user_limit(self):
        self.set_policy(max_concurrency=1)
        with patch.object(gui.threading.Thread, "start"):
            response = self.client(self.alice).post("/api/start", json={"accounts": "test@example.invalid password"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(batch.BatchManager.get_active_task()["concurrency"], 1)

    def test_forced_proxy_rejects_empty_or_invalid_pools_before_task_creation(self):
        self.set_policy(require_proxy=True)
        client = self.client(self.alice)
        with patch.object(gui.threading.Thread, "start") as start:
            for proxy, code in ((None, 403), ("", 403), (" , \n;", 403), ("nonsense", 400),
                                ("http://", 400), ("http://host:0", 400), ("http://host:99999", 400),
                                ("file:///tmp/proxy", 400), ("http://host:80/path", 400),
                                (["http://host:80"], 400)):
                with self.subTest(proxy=proxy):
                    response = client.post("/api/start", json={"accounts": "test@example.invalid password", "proxy": proxy,
                                                               "vless_proxy": "vless://synthetic", "require_proxy": False})
                    self.assertEqual(response.status_code, code)
            start.assert_not_called()
            self.assertIsNone(batch.BatchManager.get_active_task())

    def test_valid_mixed_proxy_pool_is_normalized_and_keeps_rotation_order(self):
        self.set_policy(require_proxy=True)
        proxy = "host.example:8080, socks5://user:pass@second.example:1080\nhttps://third.example:443"
        with patch.object(gui.threading.Thread, "start"):
            response = self.client(self.alice).post("/api/start", json={"accounts": "test@example.invalid password", "proxy": proxy, "concurrency": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(batch.BatchManager.get_active_task()["proxy"].splitlines(),
                         ["http://host.example:8080", "socks5://user:pass@second.example:1080", "https://third.example:443"])

    def test_proxy_requirement_cannot_be_bypassed_with_ip_detection(self):
        self.set_policy(require_proxy=True)
        with patch.object(gui._requests, "get") as get:
            self.assertEqual(self.client(self.alice).post("/api/check_proxy", json={}).status_code, 403)
            get.assert_not_called()

    def test_proxy_detection_concurrency_obeys_user_limit(self):
        self.set_policy(max_concurrency=1)
        reply = MagicMock()
        reply.json.return_value = {"ip": "192.0.2.1"}
        with patch.object(gui._requests, "get", return_value=reply), \
             patch.object(gui.concurrent.futures, "ThreadPoolExecutor", wraps=ThreadPoolExecutor) as executor:
            response = self.client(self.alice).post("/api/check_proxy", json={"proxy": "one.example:80,two.example:80"})
        self.assertTrue(response.get_json()["ok"])
        executor.assert_called_once_with(max_workers=1)

    def test_over_limit_settings_are_clamped_and_saved(self):
        self.set_policy()
        response = self.client(self.alice).post("/api/settings", json={"concurrency": 20, "accounts": "synthetic-accounts"})
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(auth.get_user_settings(self.alice), {"concurrency": "2", "accounts": "synthetic-accounts"})

    def test_invalid_concurrency_settings_are_rejected_without_partial_save(self):
        self.set_policy()
        response = self.client(self.alice).post("/api/settings", json={"concurrency": 1.5, "accounts": "must-not-save"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(auth.get_user_settings(self.alice), {})

    def test_policy_endpoint_reads_only_current_users_latest_policy(self):
        client = self.client(self.alice)
        self.assertEqual(self.client().get("/api/policy").status_code, 401)
        self.set_policy(max_concurrency=4)
        self.assertEqual(client.get(f"/api/policy?user_id={self.bob}").get_json()["policy"]["max_concurrency"], 4)
        self.set_policy(max_concurrency=1)
        self.assertEqual(client.get("/api/policy").get_json()["policy"]["max_concurrency"], 1)

    def test_recovered_and_following_batches_use_latest_concurrency(self):
        self.set_policy()
        accounts = [Account("one@example.invalid", "password"), Account("two@example.invalid", "password")]
        batch.BatchManager.init_task(f"user_{self.alice}", self.alice, accounts, concurrency=10, batch_size=1)
        observed = []
        async def pipeline(current, **kwargs):
            observed.append(kwargs["concurrency"])
            result = KeyResult(current[0], True, keys={"Visio": "SYNTHETIC-KEY"})
            kwargs["on_result"](result)
            auth.save_user_policy(self.alice, False, 1)
            return [result]
        with patch.object(gui, "run_pipeline", side_effect=pipeline), \
             patch.object(gui.config, "AUTO_RESTART_ON_BATCH", False), \
             patch.object(gui.config, "AUTO_RESTART_ON_COMPLETE", False), \
             patch.object(gui, "export_to_excel", return_value="synthetic.xlsx"), \
             patch.object(gui.BrowserProcessManager, "cleanup_all", return_value=0), \
             patch.object(gui, "release_system_memory"):
            gui._run_batch_worker_impl(f"user_{self.alice}")
        self.assertEqual(observed, [2, 1])

    def test_recovered_direct_task_stops_when_proxy_is_now_required(self):
        self.set_policy(require_proxy=True)
        batch.BatchManager.init_task(f"user_{self.alice}", self.alice, [Account("one@example.invalid", "password")])
        with patch.object(gui, "run_pipeline", AsyncMock()) as pipeline, \
             patch.object(gui.BrowserProcessManager, "cleanup_all", return_value=0), patch.object(gui, "release_system_memory"):
            gui._run_batch_worker_impl(f"user_{self.alice}")
            pipeline.assert_not_called()
        self.assertIsNone(batch.BatchManager.get_active_task())


class ExpandedIsolationTests(DatabaseTestCase):
    def test_history_and_settings_are_isolated_even_when_foreign_ids_are_supplied(self):
        self.save(self.alice)
        auth.save_user_setting(self.alice, "accounts", "ALICE-PRIVATE-CREDENTIALS")
        bob = self.client(self.bob)
        self.assertEqual(bob.get(f"/api/history?user_id={self.alice}").get_json()["rows"], [])
        self.assertEqual(bob.get(f"/api/settings?user_id={self.alice}").get_json()["settings"], {})
        alice_id = auth.get_history(self.alice)[0]["id"]
        self.assertEqual(bob.post("/api/history/delete", json={"ids": [alice_id], "user_id": self.alice}).get_json()["deleted"], 0)
        bob.post("/api/history/clear", json={"user_id": self.alice})
        bob.post("/api/settings", json={"user_id": self.alice, "accounts": "BOB-PRIVATE"})
        self.assertEqual(len(auth.get_history(self.alice)), 1)
        self.assertEqual(auth.get_user_settings(self.alice)["accounts"], "ALICE-PRIVATE-CREDENTIALS")

    def test_stale_tab_cannot_write_or_read_using_new_login_cookie(self):
        client = self.client(self.bob)
        headers = {"X-User-ID": str(self.alice)}
        for path in ("/api/settings", "/api/history", "/api/results"):
            self.assertEqual(client.get(path, headers=headers).status_code, 409)
        self.assertEqual(client.post("/api/settings", json={"accounts": "ALICE-PRIVATE"}, headers=headers).status_code, 409)
        self.assertEqual(auth.get_user_settings(self.bob), {})

    def test_deleted_user_cookie_is_invalidated_on_all_user_endpoints(self):
        client = self.client(self.alice)
        auth.delete_user(self.alice)
        for path in ("/api/settings", "/api/history", "/api/results", "/api/status", "/stream"):
            response = client.get(path)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(client.post("/api/start", json={"accounts": "test@example.invalid password"}).status_code, 401)
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_deleted_users_queued_tasks_are_not_recovered(self):
        batch.BatchManager.init_task(f"user_{self.alice}", self.alice, [Account("one@example.invalid", "password")])
        auth.delete_user(self.alice)
        with patch.object(gui, "_run_batch_worker_impl") as worker:
            gui._run_batch_worker(f"user_{self.alice}")
            worker.assert_not_called()
        self.assertIsNone(batch.BatchManager.get_active_task())

    def test_private_pages_and_responses_cannot_be_reused_from_http_cache(self):
        client = self.client(self.alice)
        for path in ("/", "/api/settings", "/api/history", "/api/results"):
            self.assertEqual(client.get(path).headers["Cache-Control"], "no-store")

    def test_cross_site_login_is_rejected_even_without_existing_session(self):
        response = self.client().post("/auth/login", json={"username": "alice", "password": "test-password"}, headers={"Origin": "https://foreign.invalid"})
        self.assertEqual(response.status_code, 403)
