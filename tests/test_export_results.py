import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

from test_admin_stats import DatabaseTestCase
from test_task_recovery import gui


XLSX_CAPTURE = """
window.__exports=[];
window.XLSX={utils:{
  json_to_sheet:rows=>({rows:structuredClone(rows)}),
  aoa_to_sheet:rows=>({rows:structuredClone(rows)}),
  book_new:()=>({sheets:[]}),
  book_append_sheet:(book,sheet,name)=>book.sheets.push({name,rows:sheet.rows})
},writeFile:(book,name)=>window.__exports.push({name,...structuredClone(book)})};
"""


class ExportResultsTests(DatabaseTestCase):
    """Exercise the actual page and export inputs without network or Azure accounts."""
    def setUp(self):
        super().setUp()
        self.rows = [
            {"email": "first@example.invalid", "password": "synthetic-password", "totp_secret": "SYNTHETIC-TOTP",
             "success": True, "keys": {gui.PRODUCT_COLUMNS[0]: "SYNTHETIC-KEY"}, "msg": "ok", "ts": "2026-11-09 10:00:01"},
            {"email": "second@example.invalid", "password": "synthetic-password", "totp_secret": "",
             "success": False, "keys": {}, "msg": "failed", "ts": "2026-11-09 10:05:02"},
        ]
        self.history = [{"id": index, **row} for index, row in enumerate(self.rows, 1)]
        self.start_response = {"ok": True, "count": 1}
        self.policy = {"require_proxy": False, "max_concurrency": 20}
        self.start_requests = []
        self.history_requests = []
        self.history_error = False
        self.status = {"running": False, "results": len(self.rows), "busy": False, "can_start": True,
                       "can_stop": False, "state": "idle", "message": "就绪"}
        self.result_requests = 0
        self.html = self.client(self.alice).get("/").get_data(as_text=True)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        root = Path(__file__).resolve().parents[1]
        candidates = list((root / "ms-playwright").glob("chromium-*/chrome-win64/chrome.exe"))
        self.browser = self.pw.chromium.launch(headless=True, **({"executable_path": str(candidates[0])} if candidates else {}))
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.route)
        self.page.goto("http://export.test/")
        self.page.wait_for_function("lsGetJ(LS.CURRENT,[]).length===2")

    def route(self, route):
        url = urlsplit(route.request.url)
        if url.hostname == "unpkg.com":
            return route.fulfill(body=XLSX_CAPTURE, content_type="application/javascript")
        if url.path == "/":
            return route.fulfill(body=self.html, content_type="text/html")
        if url.path == "/static/session.js":
            source = Path(__file__).resolve().parents[1] / "gui/static/session.js"
            return route.fulfill(body=source.read_text(encoding="utf-8"), content_type="application/javascript")
        if url.path == "/stream":
            return route.abort()
        if url.path == "/api/results":
            self.result_requests += 1
        if url.path == "/api/history":
            if self.history_error:
                return route.fulfill(status=503, body=json.dumps({"ok": False, "error": "synthetic unavailable"}), content_type="application/json")
            query = parse_qs(url.query)
            self.history_requests.append(query)
            rows = self.history
            filter_type = query.get('filter', ['all'])[0]
            if filter_type in ('success', 'failed'):
                rows = [row for row in rows if row['success'] == (filter_type == 'success')]
            if query.get('all', ['0'])[0] != '1':
                rows = rows[:500]
            return route.fulfill(body=json.dumps({"ok": True, "rows": rows}), content_type="application/json")
        if url.path == "/api/history/clear":
            self.history = []
        if url.path == "/api/start":
            self.start_requests.append(route.request.post_data_json)
        data = {"/api/results": self.rows,
                "/api/settings": {"ok": True, "settings": {}, "policy": self.policy},
                "/api/policy": {"ok": True, "policy": self.policy},
                "/api/status": self.status,
                "/api/start": self.start_response}.get(url.path, {"ok": True})
        return route.fulfill(body=json.dumps(data), content_type="application/json")

    def current(self):
        return self.page.evaluate("lsGetJ(LS.CURRENT,[])")

    def export(self, name):
        count = self.page.evaluate("window.__exports.length")
        self.page.get_by_role("button", name=name, exact=True).click()
        self.page.wait_for_function("count=>window.__exports.length>count", arg=count)
        return self.page.evaluate("window.__exports.at(-1)")

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def test_completion_does_not_append_the_whole_batch_twice(self):
        self.page.evaluate("onTaskDone()")
        self.assertEqual(len(self.current()), 2)
        success = self.export("导出成功")
        failure = self.export("导出失败")
        self.assertEqual(len(success["sheets"][0]["rows"]), 1)
        self.assertEqual(len(failure["sheets"][0]["rows"]), 1)
        self.assertEqual(success["sheets"][0]["rows"][0]["时间"], self.rows[0]["ts"])

    def test_exports_preserve_each_server_result_timestamp(self):
        self.assertEqual([row["ts"] for row in self.current()], [row["ts"] for row in self.rows])

    def test_replayed_completion_notifications_do_not_duplicate_export_rows(self):
        self.page.evaluate("Promise.all([onTaskDone(),onTaskDone(),onTaskDone()])")
        self.page.evaluate("onTaskDone()")
        self.assertEqual(len(self.current()), 2)
        book = self.export("导出成功")
        summary = dict(row for row in book["sheets"][1]["rows"] if len(row) == 2)
        self.assertEqual(summary["总账号数"], 1)
        self.assertEqual(summary["成功账号数"], 1)

    def test_next_task_completed_on_another_device_replaces_previous_snapshot(self):
        self.page.evaluate("onTaskDone()")
        self.rows = [{**self.rows[0], "email": "next-task@example.invalid", "ts": "2026-11-09 11:00:00"}]
        self.page.evaluate("onTaskDone()")
        self.assertEqual([row["email"] for row in self.current()], ["next-task@example.invalid"])

    def test_polling_shares_one_request_and_updates_changed_rows(self):
        count = self.result_requests
        self.rows[0]["keys"][gui.PRODUCT_COLUMNS[1]] = "ADDITIONAL-KEY"
        self.page.evaluate("Promise.all([syncCurrentResults(),syncCurrentResults(),syncCurrentResults()])")
        self.assertEqual(self.result_requests - count, 1)
        self.assertEqual(len(self.current()), 2)
        self.assertEqual(self.current()[0]["keys"][gui.PRODUCT_COLUMNS[1]], "ADDITIONAL-KEY")
        self.rows.append({**self.rows[0], "email": "third@example.invalid", "ts": "2026-11-09 10:10:03"})
        self.page.evaluate("syncCurrentResults()")
        self.assertEqual(len(self.current()), 3)

    def test_completion_waits_for_pending_poll_then_fetches_final_snapshot(self):
        final = self.rows + [{**self.rows[0], "email": "third@example.invalid", "ts": "2026-11-09 10:10:03"}]
        self.history = [{"id": index, **row} for index, row in enumerate(final, 1)]
        observed = self.page.evaluate("""async ({previous,final})=>{
          const original=window.fetch;let calls=0,release;
          window.fetch=(url,options)=>{
            if(String(url).startsWith('/api/results')){
              calls++;
              if(calls===1)return new Promise(resolve=>{release=()=>resolve(new Response(JSON.stringify(previous),{status:200}));});
              return Promise.resolve(new Response(JSON.stringify(final),{status:200}));
            }
            return original(url,options);
          };
          const poll=syncCurrentResults(),done=onTaskDone(),replay=onTaskDone();
          release();await Promise.all([poll,done,replay]);return {calls,rows:lsGetJ(LS.CURRENT,[])};
        }""", {"previous": self.rows, "final": final})
        self.assertEqual(observed["calls"], 2)
        self.assertEqual([row["email"] for row in observed["rows"]], [row["email"] for row in final])
        self.assertEqual(len(self.export("导出成功")["sheets"][0]["rows"]), 2)

    def test_old_response_cannot_restore_previous_batch_after_new_start(self):
        previous = self.rows
        self.rows = []
        self.page.locator('#accounts').fill('new@example.invalid synthetic-password')
        observed = self.page.evaluate("""async previous=>{
          const original=window.fetch;let release,first=true;
          window.fetch=(url,options)=>{
            if(String(url).startsWith('/api/results')&&first){
              first=false;return new Promise(resolve=>{release=()=>resolve(new Response(JSON.stringify(previous),{status:200}));});
            }
            return original(url,options);
          };
          const poll=syncCurrentResults(),done=onTaskDone();await startTask();
          release();await Promise.all([poll,done]);return lsGetJ(LS.CURRENT,[]);
        }""", previous)
        self.assertEqual(observed, [])
        self.assertNotEqual(self.page.locator('#mode-badge').inner_text(), '已完成')
        self.rows = [{**previous[0], "email": "new@example.invalid"}]
        self.page.evaluate("syncCurrentResults()")
        self.assertEqual([row["email"] for row in self.current()], ['new@example.invalid'])

    def test_failed_new_start_retains_previous_exportable_results(self):
        self.start_response = {"ok": False, "error": "synthetic busy error"}
        self.page.locator('#accounts').fill('new@example.invalid synthetic-password')
        self.page.evaluate("startTask()")
        self.assertEqual(len(self.current()), 2)
        self.assertEqual(len(self.export("导出成功")["sheets"][0]["rows"]), 1)

    def test_empty_or_failed_response_after_restart_retains_final_snapshot(self):
        self.rows = []
        self.page.evaluate("syncCurrentResults()")
        self.assertEqual(len(self.current()), 2)
        self.page.evaluate("""async()=>{
          const original=window.fetch;
          window.fetch=(url,options)=>String(url).startsWith('/api/results')?Promise.reject(new Error('synthetic offline')):original(url,options);
          await syncCurrentResults();await onTaskDone();
        }""")
        self.assertEqual(len(self.current()), 2)
        self.assertEqual(len(self.export("导出失败")["sheets"][0]["rows"]), 1)

    def test_legitimate_separate_history_attempts_are_preserved(self):
        self.history.append({**self.history[0], "id": 3, "ts": "2026-11-10 12:00:00"})
        self.page.evaluate("loadServerHistory()")
        book = self.export("导出成功")
        rows = book["sheets"][0]["rows"]
        self.assertEqual(len(rows), 2)
        repeated = [row for row in rows if row["账号 (EDU 邮箱)"] == "first@example.invalid"]
        self.assertEqual({row["时间"] for row in repeated}, {"2026-11-09 10:00:01", "2026-11-10 12:00:00"})

    def test_invalidated_login_cannot_restore_sensitive_export_cache(self):
        current = self.page.evaluate("""async previous=>{
          const original=window.fetch;let release;
          window.fetch=(url,options)=>String(url).startsWith('/api/results')
            ?new Promise(resolve=>{release=()=>resolve(new Response(JSON.stringify(previous),{status:200}));})
            :original(url,options);
          const pending=syncCurrentResults();
          window.dispatchEvent(new Event('session-invalidated'));
          release();await pending;return lsGetJ(LS.CURRENT,[]);
        }""", self.rows)
        self.assertEqual(current, [])

    def test_only_three_actions_and_no_selection_controls(self):
        self.assertEqual(self.page.locator('.tbl-actions button').all_text_contents(), ['导出成功', '导出失败', '🗑 清除历史'])
        self.assertEqual(self.page.locator('#results-section input[type=checkbox]').count(), 0)

    def test_concurrency_is_corrected_after_editing_without_blocking_empty_input(self):
        self.policy['max_concurrency'] = 4
        self.page.evaluate('policy=>applyPolicy(policy)', self.policy)
        field = self.page.locator('#concurrency-input')
        for typed, expected in [('10','4'), ('0','1'), ('-2','1'), ('2.8','2'), ('','1')]:
            field.fill(typed)
            self.assertEqual(field.input_value(), typed)
            field.blur()
            self.assertEqual(field.input_value(), expected)

    def test_start_uses_latest_policy_instead_of_stale_page_limit(self):
        self.policy['max_concurrency'] = 4
        self.page.locator('#accounts').fill('new@example.invalid synthetic-password')
        self.page.locator('#concurrency-input').fill('10')
        self.page.evaluate('startTask()')
        self.assertEqual(self.start_requests[-1]['concurrency'], 4)
        self.assertEqual(self.page.locator('#concurrency-input').input_value(), '4')
        self.assertIn('4 并发', self.page.locator('#toast-container').inner_text())

    def test_start_displays_server_effective_concurrency_if_limit_changes_again(self):
        self.policy['max_concurrency'] = 4
        self.start_response = {'ok': True, 'count': 1, 'concurrency': 2, 'policy': {'require_proxy': False, 'max_concurrency': 2}}
        self.page.locator('#accounts').fill('new@example.invalid synthetic-password')
        self.page.locator('#concurrency-input').fill('10')
        self.page.evaluate('startTask()')
        self.assertEqual(self.start_requests[-1]['concurrency'], 4)
        self.assertEqual(self.page.locator('#concurrency-input').input_value(), '2')
        self.assertIn('2 并发', self.page.locator('#toast-container').inner_text())

    def test_start_reads_latest_proxy_requirement_too(self):
        self.policy['require_proxy'] = True
        self.page.locator('#accounts').fill('new@example.invalid synthetic-password')
        self.page.evaluate('startTask()')
        self.assertEqual(self.start_requests, [])
        self.assertIn('必须填写代理池', self.page.locator('#toast-container').inner_text())

    def test_success_and_failure_exports_include_all_saved_batches_beyond_500(self):
        self.history = [
            {'id': index+1, **self.rows[0], 'email': f'success-{index}@example.invalid'} for index in range(620)
        ] + [
            {'id': index+621, **self.rows[1], 'email': f'failed-{index}@example.invalid'} for index in range(510)
        ]
        self.page.evaluate('loadServerHistory()')
        self.assertEqual(self.page.locator('#results-body tr').count(), 500)
        success = self.export('导出成功')['sheets'][0]['rows']
        failed = self.export('导出失败')['sheets'][0]['rows']
        self.assertEqual(len(success), 620)
        self.assertEqual(len(failed), 510)
        self.assertEqual(len({row['账号 (EDU 邮箱)'] for row in success}), 620)
        self.assertEqual(self.history_requests[-2:], [{'all': ['1'], 'filter': ['success']}, {'all': ['1'], 'filter': ['failed']}])

    def test_export_does_not_fall_back_to_cached_rows_on_history_fetch_failure(self):
        self.history_error = True
        self.page.get_by_role('button', name='导出成功', exact=True).click()
        self.page.wait_for_function("document.getElementById('toast-container').textContent.includes('synthetic unavailable')")
        self.assertEqual(self.page.evaluate('window.__exports.length'), 0)

    def test_cleared_history_does_not_export_from_current_task_cache(self):
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.get_by_role('button', name='🗑 清除历史', exact=True).click()
        self.page.wait_for_function('_serverHistory.length===0')
        self.page.evaluate('syncCurrentResults()')
        self.assertEqual(len(self.current()), 2)
        self.page.evaluate("exportExcel('success',document.querySelector('.tbl-actions button'))")
        self.assertEqual(self.page.evaluate('window.__exports.length'), 0)

    def test_completion_log_does_not_show_ready_before_server_cleanup_finishes(self):
        self.status = {'running':True,'busy':True,'can_start':False,'can_stop':False,'state':'cleaning','message':'任务正在收尾清理，完成后可启动'}
        self.page.evaluate('onTaskDone()')
        self.assertEqual(self.page.locator('#mode-badge').inner_text(),'清理中')
        self.assertTrue(self.page.locator('#btn-start').is_disabled())
        self.assertTrue(self.page.locator('#btn-stop').is_disabled())
        self.assertFalse(self.page.locator('#progress-section').is_visible())
        self.status = {**self.status,'running':False,'busy':False,'can_start':True,'state':'idle','message':'就绪'}
        self.page.evaluate('syncStatus()')
        self.assertTrue(self.page.locator('#btn-start').is_enabled())

    def test_recovery_state_offers_stop_and_shows_actual_busy_reason(self):
        self.status = {'running':True,'busy':True,'can_start':False,'can_stop':True,'state':'recovery_required','message':'发现未恢复的任务记录，可停止旧任务后重新启动'}
        self.page.evaluate('syncStatus()')
        self.assertEqual(self.page.locator('#mode-badge').inner_text(),'任务待恢复')
        self.assertTrue(self.page.locator('#btn-start').is_disabled())
        self.assertTrue(self.page.locator('#btn-stop').is_enabled())

    def test_late_idle_response_cannot_reenable_start_for_new_running_task(self):
        self.page.locator('#accounts').fill('new@example.invalid synthetic-password')
        self.page.evaluate("""async()=>{
          const original=window.fetch;let release;
          window.fetch=(url,options)=>String(url).startsWith('/api/status')
            ?new Promise(resolve=>{release=()=>resolve(new Response(JSON.stringify({running:false,busy:false,can_start:true,state:'idle'}),{status:200}));})
            :original(url,options);
          const check=syncStatus();await startTask();release();await check;
        }""")
        self.assertTrue(self.page.locator('#btn-start').is_disabled())
        self.assertEqual(self.page.locator('#mode-badge').inner_text(),'运行中')

    def test_unreachable_status_disables_start_until_reconnected(self):
        self.page.evaluate("""async()=>{
          const original=window.fetch;
          window.fetch=(url,options)=>String(url).startsWith('/api/status')?Promise.reject(new Error('offline')):original(url,options);
          await syncStatus();window.fetch=original;
        }""")
        self.assertTrue(self.page.locator('#btn-start').is_disabled())
        self.assertEqual(self.page.locator('#mode-badge').inner_text(),'连接中…')
        self.page.evaluate('syncStatus()')
        self.assertTrue(self.page.locator('#btn-start').is_enabled())
