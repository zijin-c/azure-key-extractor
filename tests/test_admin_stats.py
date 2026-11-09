import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_task_recovery import gui, batch
from gui import auth


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in (("DB_PATH", str(Path(self.temp.name, "users.db"))),
                            ("_local", threading.local())):
            patcher = patch.object(auth, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.close_db)
        for target, name, value in ((gui, "_sessions", {}), (gui, "_maintenance_pending", False),
                                    (gui, "_startup_recovery_pending", False),
                                    (batch, "_QUEUE_FILE", str(Path(self.temp.name, "queue.json")))):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        auth.init_db()
        auth.create_user("alice", "test-password")
        auth.create_user("bob", "test-password")
        self.admin = 1
        self.alice = 2
        self.bob = 3
        gui.app.testing = True

    def close_db(self):
        conn = getattr(auth._local, "conn", None)
        if conn is not None:
            conn.close()
            auth._local.conn = None

    def client(self, user_id=None):
        client = gui.app.test_client()
        if user_id is not None:
            with client.session_transaction() as session:
                session.update(user_id=user_id, username="synthetic-user", is_admin=True)
        return client

    def save(self, user_id=None, **overrides):
        result = {"email": "synthetic@example.invalid", "password": "PRIVATE-PASSWORD",
                  "totp_secret": "PRIVATE-TOTP", "success": True,
                  "keys": {"Visio": "SYNTHETIC-KEY-A", "Project": "SYNTHETIC-KEY-B"}}
        result.update(overrides)
        auth.save_result(user_id or self.alice, result)

    def stats_user(self, user_id):
        return next(u for u in auth.get_admin_stats()["users"] if u["id"] == user_id)


class AdminStatsTests(DatabaseTestCase):
    def test_all_admin_pages_and_apis_are_protected(self):
        gets = ("/admin", "/admin/", "/api/admin/stats", "/admin/users")
        posts = ("/admin/users/create", "/admin/users/delete", "/admin/users/password", "/admin/users/policy", "/auth/register")
        for user_id in (None, self.alice):
            client = self.client(user_id)
            for path in gets:
                with self.subTest(user=user_id, path=path):
                    expected = 403 if user_id else (302 if path in ("/admin", "/admin/") else 401)
                    self.assertEqual(client.get(path).status_code, expected)
            for path in posts:
                with self.subTest(user=user_id, path=path):
                    self.assertEqual(client.post(path, json={"self": True}).status_code, 403 if user_id else 401)
        self.assertEqual(self.client(self.admin).get("/admin").status_code, 200)

    def test_role_is_rechecked_against_database(self):
        client = self.client(self.admin)
        self.assertEqual(client.get("/api/admin/stats").status_code, 200)
        conn = auth._get_db()
        conn.execute("UPDATE users SET is_admin=0 WHERE id=?", (self.admin,))
        conn.commit()
        self.assertEqual(client.get("/api/admin/stats").status_code, 403)
        self.assertEqual(client.get("/admin").status_code, 403)

    def test_only_admin_has_homepage_entry(self):
        self.assertIn('href="/admin/"', self.client(self.admin).get("/").get_data(as_text=True))
        self.assertNotIn('href="/admin/"', self.client(self.alice).get("/").get_data(as_text=True))

    def test_stats_survive_selective_delete_clear_and_restart(self):
        self.save()
        item_id = auth.get_history(self.alice)[0]["id"]
        client = self.client(self.alice)
        self.assertEqual(client.post("/api/history/delete", json={"ids": [item_id]}).get_json()["deleted"], 1)
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 2)
        self.save(keys={"Visio": "SYNTHETIC-KEY-C"})
        self.assertEqual(client.post("/api/history/clear", json={}).get_json()["deleted"], 1)
        self.close_db()
        auth.init_db()
        row = self.stats_user(self.alice)
        self.assertEqual(row["extracted_keys"], 3)
        self.assertEqual(row["history_count"], 0)

    def test_deduplication_is_per_user_and_product(self):
        self.save()
        self.save(keys={"Visio": " synthetic-key-a ", "Project": "SYNTHETIC-KEY-B"})
        self.save(keys={"Windows": "SYNTHETIC-KEY-A"})
        self.save(self.bob, keys={"Visio": "SYNTHETIC-KEY-A"})
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 3)
        self.assertEqual(self.stats_user(self.bob)["extracted_keys"], 1)
        self.assertEqual(auth.get_admin_stats()["summary"]["extracted_keys"], 4)

    def test_empty_and_failed_results_do_not_inflate_counts(self):
        self.save(success=False, keys={})
        self.save(success=False, keys={"Visio": "", "Project": None, "Windows": "   ", "Invalid": []})
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 0)
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 0)
        self.save(success=False, keys={"Visio": "SYNTHETIC-PARTIAL-KEY"})
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 1)

    def test_repeated_keys_update_last_extracted_time_without_increasing_count(self):
        self.save(ts="2026-11-06 12:00:00")
        self.save(ts="2026-11-07 12:00:00")
        self.save(ts="2026-11-05 12:00:00")
        row = self.stats_user(self.alice)
        self.assertEqual(row["extracted_keys"], 2)
        self.assertEqual(row["last_extracted_at"], "2026-11-07 12:00:00")

    def test_deleted_users_keep_stats_and_recreated_names_are_separate(self):
        self.save()
        response = self.client(self.admin).post("/admin/users/delete", json={"user_id": self.alice})
        self.assertTrue(response.get_json()["ok"])
        deleted = self.stats_user(self.alice)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["username"], "alice")
        self.assertEqual(deleted["extracted_keys"], 2)
        auth.create_user("alice", "new-password")
        users = [u for u in auth.get_admin_stats()["users"] if u["username"] == "alice"]
        self.assertEqual(len(users), 2)
        self.assertEqual(sorted(u["extracted_keys"] for u in users), [0, 2])

    def test_admin_cannot_be_deleted_or_lose_history(self):
        self.save(self.admin)
        self.assertFalse(auth.delete_user(self.admin))
        self.assertEqual(len(auth.get_history(self.admin)), 1)

    def test_stats_response_contains_counts_without_secrets(self):
        self.save()
        response = self.client(self.admin).get("/api/admin/stats")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        stats = response.get_json()
        self.assertEqual(stats["summary"], {"active_users": 3, "users_with_keys": 1, "extracted_keys": 2, "successful_accounts": 1})
        self.assertEqual(sum(p["extracted_keys"] for p in stats["products"]), 2)
        for secret in ("PRIVATE-PASSWORD", "PRIVATE-TOTP", "SYNTHETIC-KEY-A", "key_hash"):
            self.assertNotIn(secret, response.get_data(as_text=True))
        ledger = dict(auth._get_db().execute("SELECT * FROM key_extractions LIMIT 1").fetchone())
        self.assertEqual(len(ledger["key_hash"]), 64)
        self.assertNotIn("SYNTHETIC-KEY-A", json.dumps(ledger))

    def test_history_and_statistics_write_roll_back_together(self):
        conn = auth._get_db()
        conn.execute("""CREATE TRIGGER reject_history BEFORE INSERT ON task_history
                        BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.save()
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 0)
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 0)

    def test_existing_history_backfill_includes_all_rows_and_runs_once(self):
        conn = auth._get_db()
        conn.execute("DELETE FROM settings WHERE key='key_stats_backfilled_v1'")
        rows = [(self.alice, json.dumps({"Visio": f"SYNTHETIC-{i}"})) for i in range(520)]
        rows += [(self.alice, value) for value in ("invalid json", "null", "[]", '{"Visio": ""}')]
        conn.executemany("""INSERT INTO task_history
            (user_id,email,password,success,keys,created_at) VALUES (?,'synthetic','',1,?,'2026-11-07 12:00:00')""", rows)
        conn.commit()
        auth.init_db()
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 520)
        auth.clear_history(self.alice)
        auth.init_db()
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 520)
        self.assertEqual(self.stats_user(self.alice)["history_count"], 0)

    def test_concurrent_result_saves_do_not_duplicate_or_lose_statistics(self):
        def save_and_close(index):
            try:
                self.save(keys={"Visio": "SHARED-KEY", "Project": f"UNIQUE-KEY-{index}"})
            finally:
                self.close_db()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(save_and_close, range(12)))
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 13)
        self.assertEqual(self.stats_user(self.alice)["history_count"], 12)
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 1)

    def test_successful_accounts_are_deduplicated_and_survive_deletion(self):
        self.save(email=" First@Example.invalid ")
        self.save(email="first@example.invalid", password="changed-password")
        self.save(email="failed@example.invalid", success=False)
        self.save(email="second@example.invalid")
        self.save(self.bob, email="first@example.invalid")
        self.save(email="")
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 2)
        self.assertEqual(self.stats_user(self.bob)["successful_accounts"], 1)
        auth.clear_history(self.alice)
        auth.delete_user(self.alice)
        self.close_db()
        auth.init_db()
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 2)
        self.assertTrue(self.stats_user(self.alice)["deleted"])
        ledger = dict(auth._get_db().execute("SELECT * FROM successful_accounts LIMIT 1").fetchone())
        self.assertEqual(len(ledger["email_hash"]), 64)
        self.assertNotIn("@", json.dumps(ledger))

    def test_success_without_keys_is_counted_by_result_status(self):
        self.save(email="success@example.invalid", success=True, keys={})
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 1)
        self.assertEqual(self.stats_user(self.alice)["extracted_keys"], 0)
        auth.delete_user(self.alice)
        self.assertTrue(self.stats_user(self.alice)["deleted"])

    def test_success_backfill_uses_all_existing_history_once(self):
        conn = auth._get_db()
        conn.execute("DELETE FROM settings WHERE key='account_stats_backfilled_v1'")
        conn.executemany("""INSERT INTO task_history
            (user_id,email,password,success,created_at) VALUES (?,?,'',?,'2026-11-09 12:00:00')""",
            [(self.alice, f"account-{i}@example.invalid", 1) for i in range(510)] +
            [(self.alice, "ACCOUNT-0@example.invalid", 1), (self.alice, "failure@example.invalid", 0)])
        conn.commit()
        auth.init_db()
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 510)
        auth.clear_history(self.alice)
        auth.init_db()
        self.assertEqual(self.stats_user(self.alice)["successful_accounts"], 510)

    def test_regular_user_can_still_change_own_password(self):
        response = self.client(self.alice).post("/auth/password", json={"password": "updated-password", "user_id": self.bob})
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(auth.verify_user("alice", "updated-password")[0])
        self.assertTrue(auth.verify_user("bob", "test-password")[0])

    def test_admin_user_management_remains_available(self):
        client = self.client(self.admin)
        self.assertTrue(client.post("/admin/users/create", json={"username": "carol", "password": "initial-password"}).get_json()["ok"])
        user = next(u for u in client.get("/admin/users").get_json()["users"] if u["username"] == "carol")
        self.assertTrue(client.post("/admin/users/password", json={"user_id": user["id"], "password": "reset-password"}).get_json()["ok"])
        self.assertTrue(auth.verify_user("carol", "reset-password")[0])


if __name__ == "__main__":
    unittest.main()
