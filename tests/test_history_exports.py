import json

from test_admin_stats import DatabaseTestCase
from gui import auth


class SavedHistoryExportTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        conn = auth._get_db()
        conn.executemany("""INSERT INTO task_history
            (user_id,email,password,success,keys,created_at) VALUES (?,?,'synthetic-password',?,?,'2026-11-09 12:00:00')""",
            [(self.alice, f"success-{i}@example.invalid", 1, json.dumps({"Visio": f"SYNTHETIC-{i}"})) for i in range(620)] +
            [(self.alice, f"failed-{i}@example.invalid", 0, '{}') for i in range(510)] +
            [(self.bob, "BOB-PRIVATE@example.invalid", 1, '{}')])
        conn.commit()

    def export(self, filter_type="success", client=None):
        return (client or self.client(self.alice)).get(f"/api/history?all=1&filter={filter_type}")

    def test_exports_include_all_saved_rows_beyond_the_display_limit(self):
        self.assertEqual(len(self.client(self.alice).get('/api/history').get_json()['rows']), 500)
        successes = self.export().get_json()['rows']
        failures = self.export('failed').get_json()['rows']
        self.assertEqual(len(successes), 620)
        self.assertEqual(len(failures), 510)
        self.assertTrue(all(row['success'] for row in successes))
        self.assertTrue(all(not row['success'] for row in failures))
        self.assertIn('success-0@example.invalid', {row['email'] for row in successes})
        self.assertIn('failed-0@example.invalid', {row['email'] for row in failures})
        self.assertEqual(len(self.export('all').get_json()['rows']), 1130)

    def test_all_rows_and_status_filters_remain_user_scoped(self):
        alice = self.client(self.alice)
        response = alice.get(f'/api/history?all=1&filter=success&user_id={self.bob}')
        self.assertEqual(len(response.get_json()['rows']), 620)
        self.assertNotIn('BOB-PRIVATE', response.get_data(as_text=True))
        bob = self.client(self.bob)
        rows = bob.get(f'/api/history?all=1&filter=success&user_id={self.alice}').get_json()['rows']
        self.assertEqual([row['email'] for row in rows], ['BOB-PRIVATE@example.invalid'])
        self.assertEqual(self.export('failed', bob).get_json()['rows'], [])

    def test_export_requires_active_login_and_matching_page_identity(self):
        self.assertEqual(self.export(client=self.client()).status_code, 401)
        alice = self.client(self.alice)
        self.assertEqual(alice.get('/api/history?all=1&filter=success', headers={'X-User-ID': str(self.bob)}).status_code, 409)
        auth.delete_user(self.alice)
        self.assertEqual(self.export(client=alice).status_code, 401)

    def test_cleared_history_is_not_exported_but_lifetime_stats_remain(self):
        self.save()
        before = self.stats_user(self.alice)
        alice = self.client(self.alice)
        self.assertTrue(alice.post('/api/history/clear', json={}).get_json()['ok'])
        self.assertEqual(self.export('success', alice).get_json()['rows'], [])
        self.assertEqual(self.export('failed', alice).get_json()['rows'], [])
        after = self.stats_user(self.alice)
        self.assertEqual(after['extracted_keys'], before['extracted_keys'])
        self.assertEqual(after['successful_accounts'], before['successful_accounts'])

    def test_invalid_history_filters_are_rejected(self):
        client = self.client(self.alice)
        for query in ('all=invalid', 'filter=unknown', 'all=1&filter=success;DROP+TABLE+users'):
            self.assertEqual(client.get('/api/history?' + query).status_code, 400)

    def test_older_malformed_key_data_does_not_block_entire_export(self):
        conn = auth._get_db()
        conn.execute("UPDATE task_history SET keys='not-json' WHERE email='success-0@example.invalid'")
        conn.commit()
        rows = self.export().get_json()['rows']
        self.assertEqual(len(rows), 620)
        self.assertEqual(next(row['keys'] for row in rows if row['email']=='success-0@example.invalid'), {})

    def test_worker_history_limit_is_preserved_and_ties_are_deterministic(self):
        rows = auth.get_history(self.alice)
        self.assertEqual(len(rows), 500)
        self.assertEqual([row['id'] for row in rows], sorted((row['id'] for row in rows), reverse=True))
