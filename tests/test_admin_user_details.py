import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from test_admin_stats import DatabaseTestCase
from gui import auth


class AdminUserDetailsTests(DatabaseTestCase):
    def details(self, **kwargs):
        return auth.get_admin_user_details(self.alice, **kwargs)

    def seed(self):
        self.save(email="one@example.invalid", ts="2026-11-01 09:15:00",
                  keys={"Visio": "KEY-ONE", "Project": "KEY-TWO"})
        self.save(email="ONE@example.invalid", ts="2026-11-09 10:30:00",
                  keys={"Visio": "KEY-ONE"})
        self.save(email="two@example.invalid", ts="2026-11-02T23:59:59",
                  keys={"Visio": "KEY-THREE"})
        self.save(email="failed@example.invalid", success=False, ts="2026-11-09 00:00:00",
                  keys={"Project": "PARTIAL-KEY"})
        self.save(self.bob, email="foreign@example.invalid", ts="2026-11-02 12:00:00",
                  keys={"Visio": "FOREIGN-KEY"})

    def test_period_and_type_counts_use_first_extraction_and_user_scope(self):
        self.seed()
        data = self.details()
        self.assertEqual(data["summary"], {"new_keys": 4, "new_successful_accounts": 2})
        self.assertEqual(data["lifetime"], data["summary"])
        self.assertEqual([p["period"] for p in data["periods"]], ["2026-11-09", "2026-11-02", "2026-11-01"])
        self.assertEqual([p["new_keys"] for p in data["periods"]], [1, 1, 2])
        self.assertEqual([p["new_successful_accounts"] for p in data["periods"]], [0, 1, 1])
        self.assertEqual(data["products"], [{"product": "Project", "count": 2}, {"product": "Visio", "count": 2}])
        self.assertEqual(sum(p["count"] for p in data["periods"][-1]["products"]), 2)

    def test_inclusive_dates_and_lifetime_counts(self):
        self.seed()
        data = self.details(start="2026-11-02", end="2026-11-02")
        self.assertEqual(data["summary"], {"new_keys": 1, "new_successful_accounts": 1})
        self.assertEqual(data["lifetime"]["new_keys"], 4)
        self.assertEqual(data["products"], [{"product": "Visio", "count": 1}])
        self.assertEqual(self.details(start="2026-11-10")["periods"], [])
        self.assertEqual(self.details(end="2026-11-01")["summary"]["new_keys"], 2)

    def test_hour_week_and_month_grouping(self):
        self.seed()
        self.assertEqual([p["period"] for p in self.details(group="hour")["periods"]],
                         ["2026-11-09 00:00:00", "2026-11-02 23:00:00", "2026-11-01 09:00:00"])
        self.assertEqual([p["period"] for p in self.details(group="week")["periods"]],
                         ["2026-11-09", "2026-11-02", "2026-10-26"])
        month = self.details(group="month")["periods"]
        self.assertEqual(len(month), 1)
        self.assertEqual((month[0]["period"], month[0]["new_keys"]), ("2026-11", 4))

    def test_out_of_order_results_keep_earliest_date_and_latest_activity(self):
        self.save(ts="2026-11-09 10:00:00")
        self.save(ts="2026-11-01 10:00:00")
        self.save(ts="2026-11-20 10:00:00")
        self.assertEqual([p["period"] for p in self.details()["periods"]], ["2026-11-01"])
        self.assertEqual(self.stats_user(self.alice)["last_extracted_at"], "2026-11-20 10:00:00")

    def test_deleted_history_and_user_still_have_details_after_restart(self):
        self.seed()
        expected = self.details()["periods"]
        auth.clear_history(self.alice)
        auth.delete_user(self.alice)
        self.close_db()
        auth.init_db()
        self.assertEqual(self.details()["periods"], expected)
        self.assertTrue(self.details()["user"]["deleted"])
        self.assertEqual(self.client(self.admin).get(f"/admin/users/{self.alice}").status_code, 200)

    def test_access_requires_admin_and_unknown_user_is_404(self):
        page = f"/admin/users/{self.alice}"
        api = f"/api/admin/users/{self.alice}/details"
        self.assertEqual(self.client().get(page).status_code, 302)
        self.assertEqual(self.client().get(api).status_code, 401)
        for path in (page, api):
            self.assertEqual(self.client(self.bob).get(path).status_code, 403)
            self.assertEqual(self.client(self.admin).get(path).status_code, 200)
        self.assertEqual(self.client(self.admin).get("/api/admin/users/99999/details").status_code, 404)
        self.assertEqual(self.client(self.admin).get("/admin/users/99999").status_code, 404)
        self.assertEqual(self.client(self.admin).get(api, headers={"X-User-ID": str(self.bob)}).status_code, 409)

    def test_invalid_filters_are_rejected(self):
        client = self.client(self.admin)
        for query in ("start=2026-02-30", "start=20261109", "end=invalid", "group=year",
                      "start=2026-11-10&end=2026-11-01", "group=day;DROP+TABLE+users"):
            with self.subTest(query=query):
                self.assertEqual(client.get(f"/api/admin/users/{self.alice}/details?{query}").status_code, 400)

    def test_details_do_not_expose_credentials_or_fingerprints(self):
        self.save()
        response = self.client(self.admin).get(f"/api/admin/users/{self.alice}/details")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for secret in ("PRIVATE-PASSWORD", "PRIVATE-TOTP", "SYNTHETIC-KEY-A", "synthetic@example.invalid", "key_hash", "email_hash"):
            self.assertNotIn(secret, response.get_data(as_text=True))

    def test_success_only_and_empty_users_have_correct_empty_states(self):
        self.assertEqual(self.details()["summary"]["new_keys"], 0)
        self.assertEqual(self.details()["products"], [])
        self.save(keys={}, ts="2026-11-09 12:00:00")
        data = self.details()
        self.assertEqual(data["periods"][0]["new_keys"], 0)
        self.assertEqual(data["periods"][0]["new_successful_accounts"], 1)

    def test_timeline_limit_does_not_truncate_summary_or_products(self):
        start = datetime(2026, 1, 1)
        for index in range(505):
            self.save(keys={"Visio": f"KEY-{index}"}, email=f"account-{index}@example.invalid",
                      ts=(start+timedelta(hours=index)).isoformat(sep=" "))
        data = self.details(group="hour")
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["periods"]), 500)
        self.assertEqual(data["summary"]["new_keys"], 505)
        self.assertEqual(data["products"], [{"product": "Visio", "count": 505}])

    def test_legacy_dates_are_inferred_or_unknown_without_inventing_dates(self):
        self.save(email="known@example.invalid", keys={"Visio": "KNOWN-KEY"}, ts="2026-11-01 10:00:00")
        self.save(email="known@example.invalid", keys={"Visio": "KNOWN-KEY"}, ts="2026-11-09 10:00:00")
        self.save(email="unknown@example.invalid", keys={"Project": "UNKNOWN-KEY"}, ts="2026-11-02 10:00:00")
        unknown = next(h["id"] for h in auth.get_history(self.alice) if h["email"] == "unknown@example.invalid")
        auth.delete_history_items(self.alice, [unknown])
        conn = auth._get_db()
        keys = conn.execute("SELECT user_id,username,product,key_hash,extracted_at FROM key_extractions").fetchall()
        accounts = conn.execute("SELECT user_id,username,email_hash,succeeded_at FROM successful_accounts").fetchall()
        conn.executescript("""
            DROP TABLE key_extractions;
            DROP TABLE successful_accounts;
            CREATE TABLE key_extractions (user_id INTEGER, username TEXT, product TEXT,
                key_hash TEXT, extracted_at TEXT, PRIMARY KEY(user_id,product,key_hash));
            CREATE TABLE successful_accounts (user_id INTEGER,username TEXT,email_hash TEXT,
                succeeded_at TEXT,PRIMARY KEY(user_id,email_hash));
            DELETE FROM settings WHERE key='first_extraction_dates_v1';
        """)
        conn.executemany("INSERT INTO key_extractions VALUES (?,?,?,?,?)", keys)
        conn.executemany("INSERT INTO successful_accounts VALUES (?,?,?,?)", accounts)
        conn.commit()
        auth.init_db()
        data = self.details()
        self.assertEqual([p["period"] for p in data["periods"]], ["2026-11-01", None])
        self.assertEqual(data["timing"]["new_keys"], {"inferred": 2, "unknown": 1})
        self.assertEqual(data["timing"]["new_successful_accounts"], {"inferred": 2, "unknown": 1})
        self.save(email="unknown@example.invalid", keys={"Project": "UNKNOWN-KEY"}, ts="2026-11-20 10:00:00")
        self.assertEqual(self.details(start="2026-11-20")["summary"]["new_keys"], 0)
        auth.clear_history(self.alice)
        self.close_db()
        auth.init_db()
        self.assertEqual(self.details()["periods"], data["periods"])

    def test_rollback_does_not_leave_a_phantom_first_date(self):
        conn = auth._get_db()
        conn.execute("""CREATE TRIGGER reject_result BEFORE INSERT ON task_history
                        BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.save(ts="2026-11-01 10:00:00")
        self.assertEqual(self.details()["periods"], [])

    def test_live_writer_cannot_mix_different_snapshots_in_one_report(self):
        conn = auth._get_db()
        inserted = []
        def concurrent_save(sql):
            if "COUNT(*) AS total" in sql and not inserted:
                inserted.append(True)
                with closing(sqlite3.connect(auth.DB_PATH)) as writer:
                    writer.execute("""INSERT INTO key_extractions
                        (user_id,username,product,key_hash,extracted_at,first_extracted_at,time_inferred)
                        VALUES (?,'alice','Visio','synthetic-fingerprint','2026-11-09 12:00:00','2026-11-09 12:00:00',0)
                    """, (self.alice,))
                    writer.commit()
        conn.set_trace_callback(concurrent_save)
        try:
            report = self.details()
        finally:
            conn.set_trace_callback(None)
        self.assertTrue(inserted)
        self.assertEqual(report["lifetime"]["new_keys"], report["summary"]["new_keys"])
        self.assertEqual(report["summary"]["new_keys"], 0)
        self.assertEqual(self.details()["summary"]["new_keys"], 1)
