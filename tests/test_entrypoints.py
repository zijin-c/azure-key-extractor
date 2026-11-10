import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def scenario(self, directory, body):
        environment = os.environ.copy()
        chromium = next((ROOT / 'ms-playwright').glob('chromium-*/chrome-win64/chrome.exe'), None)
        if chromium:
            environment['CHROMIUM_PATH'] = str(chromium)
        prefix = '''
import importlib,runpy,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch,MagicMock
from gui import auth
with patch.object(auth,'init_db'),patch('utils.session_secret.load_session_secret',return_value='synthetic-entrypoint-test-secret-12345'),patch('threading.Thread.start'):
    canonical=importlib.import_module('gui.app')
    compatibility=importlib.import_module('app')
    launcher=importlib.import_module('run_gui')
from modules import batch_manager as batch
from utils.task_scope import task_scope
'''
        result = subprocess.run([sys.executable, '-c', prefix + body], cwd=directory, env=environment,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wsgi_entrypoints_share_the_canonical_flask_application(self):
        for directory in (ROOT, ROOT / 'vps_deploy'):
            with self.subTest(directory=directory):
                self.scenario(directory, '''
assert compatibility.app is canonical.app
assert compatibility.app.secret_key=='synthetic-entrypoint-test-secret-12345'
assert compatibility.app.view_functions['api_start'] is canonical.api_start
''')

    def test_python_app_py_delegates_to_standard_launcher(self):
        for directory in (ROOT, ROOT / 'vps_deploy'):
            with self.subTest(directory=directory):
                self.scenario(directory, '''
with patch.object(launcher,'main') as main:
    runpy.run_path('app.py',run_name='__main__')
    main.assert_called_once_with()
''')

    def test_both_wsgi_entrypoints_allow_two_users_to_start_with_their_own_limits(self):
        for directory in (ROOT, ROOT / 'vps_deploy'):
            with self.subTest(directory=directory):
                self.scenario(directory, '''
with tempfile.TemporaryDirectory() as temp,patch.object(auth,'DB_PATH',str(Path(temp,'users.db'))),patch.object(auth,'_local',threading.local()),patch.object(batch,'_QUEUE_FILE',str(Path(temp,'queue.json'))),patch.object(canonical,'_startup_recovery_pending',False),patch.object(canonical,'_sessions',{}):
    try:
        auth.init_db()
        auth.create_user('alice','synthetic-password')
        auth.create_user('bob','synthetic-password')
        auth.save_user_policy(2,True,2)
        auth.save_user_policy(3,False,4)
        clients=[]
        for user in (2,3):
            client=compatibility.app.test_client()
            with client.session_transaction() as session:
                session.update(user_id=user,username='synthetic',sid=f'user_{user}')
            clients.append(client)
        def start(item):
            user,client=item
            try:
                response=client.post('/api/start',json={'accounts':f'account-{user}@example.invalid synthetic','concurrency':10,'proxy':'proxy.example:8080' if user==2 else ''})
                assert response.status_code==200,response.get_json()
                return response.get_json()
            finally:
                auth.close_db()
        with patch.object(canonical,'threading',SimpleNamespace(Thread=MagicMock())):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(start,zip((2,3),clients)))
        assert [row['concurrency'] for row in outcomes]==[2,4]
        assert all(row['ok'] for row in outcomes)
        assert len(batch.BatchManager.list_user_tasks())==2
        with task_scope(2):assert batch.BatchManager.get_active_task()['user_id']==2
        with task_scope(3):assert batch.BatchManager.get_active_task()['user_id']==3
        response=clients[0].post('/api/start',json={'accounts':'same@example.invalid synthetic'})
        assert response.status_code==400
        assert response.get_json()['status']['can_start'] is False
    finally:
        auth.close_db()
''')

    def test_runtime_version_is_reported_even_without_login(self):
        for directory in (ROOT, ROOT / 'vps_deploy'):
            with self.subTest(directory=directory):
                self.scenario(directory, '''
client=compatibility.app.test_client()
response=client.get('/login')
assert response.status_code==200
assert response.headers['X-Azure-Key-Version']=='2026.11.10-entrypoint-fix'
assert response.headers['X-Azure-Key-Task-Isolation']=='per-user'
response=client.get('/api/status')
assert response.status_code==401
assert response.headers['X-Azure-Key-Task-Isolation']=='per-user'
''')

    def test_no_deployment_entrypoint_keeps_the_legacy_global_busy_guard(self):
        old_message = '已有任务在运行中或在批次重启过渡中'
        for directory in (ROOT, ROOT / 'vps_deploy'):
            for name in ('app.py', 'gui/app.py'):
                self.assertNotIn(old_message, (directory / name).read_text(encoding='utf-8'))
