import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config
from gui import auth
from modules import pipeline
from modules.pipeline import Account, KeyResult
from playwright.async_api import async_playwright
from test_admin_stats import DatabaseTestCase
from test_task_recovery import gui, batch
from utils.process_manager import BrowserProcessManager as Processes, get_playwright_driver_pid, safe_close_playwright
from utils.task_scope import current_task_user, task_scope, scoped_data_path


class MultiUserTaskTests(DatabaseTestCase):
    def init_task(self, user, **kwargs):
        with task_scope(user):
            return batch.BatchManager.init_task(f"user_{user}", user,
                                               [Account(f"account-{user}@example.invalid", "synthetic")], **kwargs)

    def task(self, user):
        with task_scope(user):
            return batch.BatchManager.get_active_task()

    def test_two_users_can_start_independent_tasks_and_only_own_slot_is_busy(self):
        auth.save_user_policy(self.alice, True, 2)
        auth.save_user_policy(self.bob, False, 4)
        def start(user):
            try:
                return self.client(user).post('/api/start', json={
                    'accounts':f'account-{user}@example.invalid synthetic', 'concurrency':10,
                    'proxy':'proxy.example:8080' if user==self.alice else '',
                    'products':['Visio'] if user==self.alice else ['Project']}).get_json()
            finally:
                auth.close_db()
        with patch.object(gui, 'threading', SimpleNamespace(Thread=MagicMock())):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(start,(self.alice,self.bob)))
        self.assertTrue(all(row['ok'] for row in outcomes))
        self.assertEqual([row['concurrency'] for row in outcomes],[2,4])
        self.assertEqual(self.task(self.alice)['selected_products'],['Visio'])
        self.assertEqual(self.task(self.bob)['selected_products'],['Project'])
        self.assertEqual(len(batch.BatchManager.list_user_tasks()),2)
        self.assertEqual(self.client(self.alice).post('/api/start',json={'accounts':'repeat@example.invalid password'}).status_code,400)
        self.assertIsNone(current_task_user())

    def test_stop_does_not_touch_other_users_queue_or_results(self):
        for user in (self.alice,self.bob):
            self.init_task(user)
            with task_scope(user):
                batch.BatchManager.record_account_result(KeyResult(Account(f'{user}@example.invalid','synthetic'),True,keys={'Visio':f'KEY-{user}'}))
        with patch.object(Processes,'cleanup_all',return_value=0) as cleanup:
            self.assertTrue(self.client(self.alice).post('/api/stop',json={}).get_json()['ok'])
            cleanup.assert_called_once()
        self.assertIsNone(self.task(self.alice))
        self.assertEqual(self.task(self.bob)['status'],'running')
        self.assertEqual(self.client(self.bob).get('/api/results').get_json()[0]['email'],f'{self.bob}@example.invalid')
        self.assertEqual(self.client(self.alice).get('/api/results').get_json(),[])

    def test_foreign_active_user_does_not_block_idle_users_status(self):
        self.init_task(self.bob)
        gui._get_sess(f'user_{self.bob}').running=True
        gui._get_sess(f'user_{self.bob}').worker_active=True
        status=self.client(self.alice).get('/api/status').get_json()
        self.assertEqual((status['state'],status['busy'],status['can_start']),('idle',False,True))
        self.assertNotIn(f'user_{self.bob}',json.dumps(status))

    def test_final_cleanup_stays_busy_only_for_owner(self):
        self.init_task(self.alice)
        with task_scope(self.alice):
            batch.BatchManager.mark_all_done(restarting=False)
        state=gui._get_sess(f'user_{self.alice}')
        state.running=state.worker_active=True
        owner=self.client(self.alice).get('/api/status').get_json()
        other=self.client(self.bob).get('/api/status').get_json()
        self.assertEqual(owner['state'],'cleaning')
        self.assertFalse(owner['can_start'])
        self.assertTrue(other['can_start'])
        state.running=state.worker_active=False
        self.assertTrue(self.client(self.alice).get('/api/status').get_json()['can_start'])

    def test_orphan_task_can_be_stopped_without_restarting_service(self):
        self.init_task(self.alice)
        status=self.client(self.alice).get('/api/status').get_json()
        self.assertEqual(status['state'],'recovery_required')
        self.assertTrue(status['can_stop'])
        with patch.object(Processes,'cleanup_all',return_value=0):
            self.client(self.alice).post('/api/stop',json={})
        self.assertTrue(self.client(self.alice).get('/api/status').get_json()['can_start'])

    def test_initialization_failure_releases_only_failed_task_and_preserves_results(self):
        self.init_task(self.alice)
        self.init_task(self.bob)
        with task_scope(self.alice):
            batch.BatchManager.record_account_result(KeyResult(Account('partial@example.invalid','synthetic'),True,keys={'Visio':'SYNTHETIC'}))
        with patch.object(gui,'_run_batch_worker_impl',side_effect=RuntimeError('synthetic initialization failure')), \
             patch.object(Processes,'cleanup_all',return_value=0), self.assertLogs(gui.app.logger,level='ERROR'):
            gui._run_batch_worker(f'user_{self.alice}')
        self.assertEqual(self.task(self.alice)['status'],'failed')
        self.assertEqual(len(self.task(self.alice)['completed_results']),1)
        self.assertEqual(self.task(self.bob)['status'],'running')
        self.assertTrue(self.client(self.alice).get('/api/status').get_json()['can_start'])

    def test_global_maintenance_rejected_while_any_users_queue_or_cleanup_is_active(self):
        self.init_task(self.bob)
        admin=self.client(self.admin)
        with patch.object(Processes,'cleanup_all') as cleanup, patch.object(gui.threading.Thread,'start') as start:
            for path in ('/api/cleanup','/api/restart'):
                self.assertEqual(admin.post(path,json={}).status_code,403)
            cleanup.assert_not_called();start.assert_not_called()
        with task_scope(self.bob):batch.BatchManager.clear_task()
        gui._get_sess(f'user_{self.bob}').worker_active=True
        self.assertEqual(admin.post('/api/cleanup',json={}).status_code,403)

    def test_proxy_pool_check_threads_inherit_the_user_identity(self):
        seen=[]
        def check_request(*args,**kwargs):
            seen.append(current_task_user())
            reply=MagicMock();reply.json.return_value={'ip':'192.0.2.1'}
            return reply
        with patch.object(gui._requests,'get',side_effect=check_request):
            result=self.client(self.alice).post('/api/check_proxy',json={'proxy':'one.example:8080,two.example:8080'})
        self.assertTrue(result.get_json()['ok'])
        self.assertEqual(seen,[self.alice,self.alice])

    def test_startup_restores_all_users_and_migrates_legacy_queue_without_overwrite(self):
        legacy=batch.BatchManager.init_task(f'user_{self.alice}',self.alice,[Account('legacy@example.invalid','synthetic')])
        self.init_task(self.bob,selected_products=['Project'])
        with patch.object(gui.threading.Thread,'start') as start,patch('time.sleep'):
            gui._startup_check_batch()
        self.assertEqual(start.call_count,2)
        self.assertEqual(self.task(self.alice)['all_accounts'],legacy['all_accounts'])
        self.assertTrue(Path(batch._QUEUE_FILE+'.legacy').is_file())
        self.assertFalse(gui._startup_recovery_pending)
        # Old legacy file reappears: never overwrite the newer per-user queue.
        Path(batch._QUEUE_FILE).write_text(json.dumps({**legacy,'all_accounts':[]}),encoding='utf-8')
        batch.BatchManager.migrate_legacy_task()
        self.assertEqual(self.task(self.alice)['all_accounts'],legacy['all_accounts'])

    def test_restart_failure_for_one_user_does_not_prevent_recovering_another(self):
        self.init_task(self.alice);self.init_task(self.bob)
        with patch.object(gui.threading.Thread,'start',side_effect=[RuntimeError('synthetic'),None]),patch('time.sleep'),self.assertLogs(gui.app.logger,level='ERROR'):
            gui._startup_check_batch()
        statuses={self.task(user)['status'] for user in (self.alice,self.bob)}
        self.assertEqual(statuses,{'failed','running'})

    def test_products_and_result_files_are_scoped_across_concurrent_async_tasks(self):
        async def task(user,products):
            with task_scope(user,products):
                await asyncio.sleep(0)
                return current_task_user(),config.get_products_to_extract(),scoped_data_path('results')
        async def execute():
            return await asyncio.gather(task(self.alice,['Visio']),task(self.bob,['Project']))
        results=asyncio.run(execute())
        self.assertEqual(results[0][1],['Visio']);self.assertEqual(results[1][1],['Project'])
        self.assertNotEqual(results[0][2],results[1][2])
        with patch.object(pipeline,'_RESULTS_DIR',self.temp.name):
            for user in (self.alice,self.bob):
                with task_scope(user):
                    self.assertTrue(pipeline._persist_result(KeyResult(Account('same@example.invalid',f'password-{user}'),True)))
            files=list(Path(self.temp.name).glob('user_*/*.jsonl'))
            self.assertEqual(len(files),2)
            for path in files:
                self.assertEqual(len(path.read_text().splitlines()),1)


class ProcessScopeTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        for name,value in (('_active_driver_pids',set()),('_tracked_child_pids',{}),('_driver_owners',{})):
            p=patch.object(Processes,name,value);p.start();self.addCleanup(p.stop)

    def test_cleanup_only_kills_own_registered_driver_trees_never_scans_all_browsers(self):
        for user,pid in ((self.alice,900002),(self.bob,900003)):
            with task_scope(user):Processes.register_driver(pid)
        with patch.object(Processes,'_get_descendant_pids',return_value={900012}), \
             patch.object(Processes,'_kill_pids',return_value=2) as kill, \
             patch.object(Processes,'_find_orphan_browsers') as scan:
            with task_scope(self.alice):
                self.assertEqual(Processes.cleanup_all(),2)
            kill.assert_called_once_with({900002,900012})
            scan.assert_not_called()
        self.assertEqual(Processes._active_driver_pids,{900003})
        self.assertEqual(Processes._driver_owners,{900003:self.bob})

    def test_explicit_foreign_pid_cannot_be_killed_by_current_user(self):
        with task_scope(self.bob):Processes.register_driver(900003)
        with task_scope(self.alice),patch.object(Processes,'_kill_pids') as kill:
            self.assertEqual(Processes.kill_driver_and_descendants(900003),0)
            with self.assertRaises(RuntimeError):Processes.cleanup_all(global_scope=True)
            kill.assert_not_called()

    def test_unscoped_cleanup_cannot_scan_or_kill_live_user_browsers(self):
        with task_scope(self.bob):Processes.register_driver(900003)
        with patch.object(Processes,'_kill_pids',return_value=0) as kill,patch.object(Processes,'_find_orphan_browsers') as scan:
            Processes.cleanup_all()
            kill.assert_called_once_with(set());scan.assert_not_called()
        self.assertEqual(Processes._active_driver_pids,{900003})

    def test_real_browsers_run_concurrently_and_stopping_a_preserves_b_through_next_batch(self):
        started={user:threading.Event() for user in (self.alice,self.bob)}
        release=threading.Event();a_stopped=threading.Event();b_survived=threading.Event()
        threads=[];observed=[];browser_errors=[]
        root=Path(__file__).resolve().parents[1]
        candidates=list((root/'ms-playwright').glob('chromium-*/chrome-win64/chrome.exe'))

        async def browser_pipeline(accounts,**kwargs):
            user=current_task_user()
            pw=await async_playwright().start()
            driver=get_playwright_driver_pid(pw)
            Processes.register_driver(driver)
            browser=None
            try:
                browser=await pw.chromium.launch(headless=True,**({'executable_path':str(candidates[0])} if candidates else {}))
                page=await browser.new_page()
                await page.set_content(f'<h1>USER-{user}</h1>')
                Processes.record_descendants(driver)
                observed.append((user,kwargs['concurrency'],config.get_products_to_extract(),kwargs['proxy_str']))
                started[user].set()
                while not release.is_set():
                    self.assertEqual(await page.locator('h1').inner_text(),f'USER-{user}')
                    if user==self.bob and a_stopped.is_set():b_survived.set()
                    await asyncio.sleep(.03)
                result=KeyResult(accounts[0],True,keys={config.get_products_to_extract()[0]:f'KEY-{user}-{accounts[0].email}'})
                kwargs['on_result'](result)
                return [result]
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not (user==self.alice and not gui._get_sess(f'user_{user}').running):
                    browser_errors.append(str(error))
                raise
            finally:
                await safe_close_playwright(browser=browser,pw=pw,driver_pid=driver)

        def create_thread(*args,**kwargs):
            thread=threading.Thread(*args,**kwargs);threads.append(thread);return thread

        auth.save_user_policy(self.alice,True,2)
        auth.save_user_policy(self.bob,False,3)
        with patch.object(gui,'run_pipeline',side_effect=browser_pipeline), \
             patch.object(gui,'threading',SimpleNamespace(Thread=create_thread)), \
             patch.object(gui,'export_to_excel',return_value='synthetic.xlsx'), \
             patch.object(gui.config,'AUTO_RESTART_ON_BATCH',True), \
             patch.object(gui.config,'AUTO_RESTART_ON_COMPLETE',True), \
             patch.object(batch.BatchManager,'trigger_service_restart') as restart:
            try:
                a=self.client(self.alice).post('/api/start',json={'accounts':'a@example.invalid synthetic','products':['Visio'],'concurrency':20,'proxy':'proxy.example:8080'})
                self.assertTrue(a.get_json()['ok'])
                self.assertTrue(started[self.alice].wait(15),'A browser did not start')
                b=self.client(self.bob).post('/api/start',json={'accounts':'b1@example.invalid synthetic\nb2@example.invalid synthetic','products':['Project'],'concurrency':20,'batch_size':1})
                self.assertTrue(b.get_json()['ok'])
                self.assertTrue(started[self.bob].wait(15),'B browser did not start while A was active')
                self.assertEqual(self.client(self.alice).get('/api/status').get_json()['state'],'running')
                self.assertEqual(self.client(self.bob).get('/api/status').get_json()['state'],'running')
                self.assertTrue(self.client(self.alice).post('/api/stop',json={}).get_json()['ok'])
                a_stopped.set()
                self.assertTrue(b_survived.wait(10),'B browser was affected by A stop/cleanup')
                self.assertEqual(self.client(self.bob).get('/api/status').get_json()['state'],'running')
                release.set()
            finally:
                release.set()
                for thread in threads:thread.join(timeout=15)
            self.assertFalse(any(thread.is_alive() for thread in threads),'worker thread did not finish')
            restart.assert_not_called()
        self.assertEqual(browser_errors,[])
        a_observed=[item for item in observed if item[0]==self.alice]
        b_observed=[item for item in observed if item[0]==self.bob]
        self.assertEqual(a_observed,[(self.alice,2,['Visio'],'http://proxy.example:8080')])
        self.assertEqual(len(b_observed),2)
        self.assertTrue(all(item[1:]==(3,['Project'],None) for item in b_observed))
        with task_scope(self.bob):
            self.assertEqual(batch.BatchManager.get_active_task()['status'],'completed')
            self.assertEqual(len(batch.BatchManager.get_all_results()),2)
        self.assertTrue(self.client(self.bob).get('/api/status').get_json()['can_start'])
        self.assertEqual(Processes._active_driver_pids,set())
